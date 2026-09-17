# -*- coding: utf-8 -*-
"""验证码页 Cloudflare 防护测试：检测、等待放行、禁止导航强发。"""
import time
import unittest
from unittest.mock import patch

from core import roxy_registration as rr


class _FakeDriver:
    """模拟 Roxy/Cloak 驱动的最小接口。"""

    def __init__(self, title="", body_text="", has_email_input=False):
        self._title = title
        self._body_text = body_text
        self._has_email_input = has_email_input
        self._registration_log_prefix = "[测试注册]"

    @property
    def title(self):
        if self._title is None:
            raise AttributeError("title")
        return self._title

    def execute_script(self, script, *args):
        has_input = "true" if self._has_email_input else "false"
        # 仅提供脚本用到的 DOM 状态；脚本返回布尔表达式结果。
        lowered = script.lower()
        if "queryselector" in lowered:
            return (not self._has_email_input) and any(
                mark in self._body_text.lower()
                for mark in ("performing security verification", "just a moment", "verify you are not a bot")
            )
        return None


class CloudflareChallengeDetectionTests(unittest.TestCase):
    def test_title_based_detection(self):
        self.assertTrue(rr._is_cloudflare_challenge_page(_FakeDriver(title="Just a moment...")))
        self.assertTrue(rr._is_cloudflare_challenge_page(_FakeDriver(title="请稍候…")))
        self.assertFalse(rr._is_cloudflare_challenge_page(_FakeDriver(title="ChatGPT")))

    def test_body_based_detection_without_title(self):
        driver = _FakeDriver(title="", body_text="Performing security verification\nRay ID: xxx")
        self.assertTrue(rr._is_cloudflare_challenge_page(driver))
        # 有邮箱输入框的正常验证码页即使文案命中也不算挑战页。
        driver_ok = _FakeDriver(title="", body_text="Performing security verification", has_email_input=True)
        self.assertFalse(rr._is_cloudflare_challenge_page(driver_ok))

    def test_detection_with_cloak_style_driver(self):
        class _CloakStyle:
            def execute_script(self, script, *args):
                return True

        # 没有 .title 属性（Cloak 适配层）也应通过脚本路径检测。
        self.assertTrue(rr._is_cloudflare_challenge_page(_CloakStyle()))

    def test_detection_reads_title_via_playwright_page(self):
        """Cloak 适配层：无 .title 但有 .page.title()，日文挑战标题应被识别。"""

        class _Page:
            def title(self):
                return "しばらくお待ちください..."

        class _CloakDriver:
            page = _Page()

            def execute_script(self, script, *args):
                return False

        self.assertTrue(rr._is_cloudflare_challenge_page(_CloakDriver()))


class EnsureEmailOtpReadyGuardTests(unittest.TestCase):
    def test_otp_input_present_returns_without_nav_api(self):
        """输入框已出现时直接进入取码，即使探针未观测到发信也不允许导航强发。"""
        driver = _FakeDriver(title="验证你的邮箱")
        with patch.object(rr, "_email_otp_input_present", return_value=True), \
                patch.object(rr, "_has_access_token", return_value=False), \
                patch.object(rr, "_email_otp_page_state", return_value={"url": "https://auth.openai.com/email-verification"}), \
                patch.object(rr, "_log_email_otp_send_probe", return_value=[]), \
                patch.object(rr, "_email_otp_wait_after_ts", return_value=123.0), \
                patch.object(rr, "_click_resend_email_otp", side_effect=AssertionError("不应补点重发")), \
                patch.object(rr, "_request_email_otp_send_via_browser", side_effect=AssertionError("不应导航强发")) as nav:
            result = rr._ensure_email_otp_ready_once(driver, "a@b.com", timeout=3, prefer_after_ts=100.0)
        self.assertEqual(result, 123.0)
        nav.assert_not_called()

    def test_challenge_page_pauses_clicks_until_cleared(self):
        """Cloudflare 验证页出现时不点击不强发，放行后输入框在位直接进入取码。"""
        driver = _FakeDriver(title="Just a moment...")
        order = []

        def _challenge_then_clear(driver_obj):
            order.append("cf")
            return len([x for x in order if x == "cf"]) <= 3  # 前 3 次检测为挑战页，之后放行

        with patch.object(rr, "_is_cloudflare_challenge_page", side_effect=_challenge_then_clear), \
                patch.object(rr, "_has_access_token", return_value=False), \
                patch.object(rr, "_email_otp_input_present", return_value=True), \
                patch.object(rr, "_email_otp_page_state", return_value={"url": "https://auth.openai.com/email-verification"}), \
                patch.object(rr, "_email_otp_wait_after_ts", return_value=55.0), \
                patch.object(rr.time, "sleep"), \
                patch.object(rr, "_click_resend_email_otp", side_effect=AssertionError("输入框在位时不应补点重发")) as resend, \
                patch.object(rr, "_request_email_otp_send_via_browser", side_effect=AssertionError("挑战期/未确认发信不应导航强发")) as nav:
            result = rr._ensure_email_otp_ready_once(driver, "a@b.com", timeout=8, prefer_after_ts=1.0)
        self.assertEqual(result, 55.0)
        resend.assert_not_called()
        nav.assert_not_called()
        # 挑战期间应原地等待（sleep 被调用多次），放行后才继续。
        self.assertGreaterEqual([x for x in order if x == "cf"].__len__(), 3)

    def test_ready_wrapper_waits_cf_without_refresh(self):
        """超时后若处于 Cloudflare 验证页，应等待放行而不是刷新（刷新会重置挑战）。"""
        driver = _FakeDriver(title="Just a moment...")
        with patch.object(rr, "_ensure_email_otp_ready_once",
                          side_effect=RuntimeError("等待邮箱验证码输入框/发信入口超时：email=a@b.com")), \
                patch.object(rr, "_is_cloudflare_challenge_page", return_value=True), \
                patch.object(rr, "_headless_cf_hint", return_value="CLOAK_HEADLESS"), \
                patch.object(rr, "_wait_cloudflare_challenge_clear", return_value=True) as wait_cf, \
                patch.object(rr, "_refresh_or_reopen", side_effect=AssertionError("CF 期间不应刷新")) as refresh, \
                patch.object(rr.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "等待邮箱验证码输入框/发信入口超时"):
                rr._ensure_email_otp_ready(driver, "a@b.com", timeout=2, prefer_after_ts=1.0)
        wait_cf.assert_called()
        refresh.assert_not_called()


class ReactHydrationWaitTests(unittest.TestCase):
    def test_returns_true_when_props_attached(self):
        driver = _FakeDriver(title="t")
        driver.execute_script = lambda script, *args: True
        self.assertTrue(rr._wait_react_hydrated(driver, object(), timeout=1))

    def test_returns_false_after_timeout_when_never_hydrated(self):
        driver = _FakeDriver(title="t")
        driver.execute_script = lambda script, *args: False
        with patch.object(rr.time, "time", side_effect=[0.0] + [100.0] * 40), \
                patch.object(rr.time, "sleep"):
            self.assertFalse(rr._wait_react_hydrated(driver, object(), timeout=5))

    def test_script_failure_is_tolerated(self):
        driver = _FakeDriver(title="t")

        def boom(script, *args):
            raise RuntimeError("no js")

        driver.execute_script = boom
        self.assertFalse(rr._wait_react_hydrated(driver, object(), timeout=1))


    def test_click_continue_passes_driver_to_find_any(self):
        """回归防护：_click_continue 必须把 driver 传给 _find_any。"""
        driver = _FakeDriver(title="t")
        with patch.object(rr, "_find_any") as find_any, \
                patch.object(rr, "_wait_react_hydrated"), \
                patch.object(rr, "_human_click"):
            rr._click_continue(driver)
        args, kwargs = find_any.call_args
        self.assertIs(args[0], driver)
        self.assertIsInstance(args[1], list)


if __name__ == "__main__":
    unittest.main()
