# -*- coding: utf-8 -*-
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core import roxy_registration


class RoxyEmailOtpReadyTests(unittest.TestCase):
    def test_input_page_trusts_input_and_waits_without_resend(self):
        """输入框在位时信任 OTP 流已激活，直接进入取码，不补点重发、不导航强发。"""
        driver = SimpleNamespace()
        page_state = {"url": "https://auth.openai.com/email-verification", "inputs": []}

        with patch.object(roxy_registration, "_has_access_token", return_value=False), \
             patch.object(roxy_registration, "_email_otp_input_present", return_value=True), \
             patch.object(roxy_registration, "_email_otp_page_state", return_value=page_state), \
             patch.object(roxy_registration, "_click_resend_email_otp", side_effect=AssertionError("不应补点重发")) as resend, \
             patch.object(roxy_registration, "_install_email_otp_send_probe"), \
             patch.object(roxy_registration, "_log_email_otp_send_probe", return_value=[]), \
             patch.object(roxy_registration, "_request_email_otp_send_via_browser") as direct_send, \
             patch.object(roxy_registration.time, "time", return_value=1000.0), \
             patch.object(roxy_registration.time, "sleep"):
            after_ts = roxy_registration._ensure_email_otp_ready(driver, "user@example.com", timeout=35)

        self.assertEqual(after_ts, 1000.0)
        resend.assert_not_called()
        direct_send.assert_not_called()

    def test_input_page_without_observed_send_request_keeps_page_by_default(self):
        """默认关闭导航强发：未观测到发信请求时保持当前页直接取码（避免触发 Cloudflare）。"""
        driver = SimpleNamespace()
        setattr(driver, "_registration_otp_trigger_ts", 2222.0)
        page_state = {"url": "https://auth.openai.com/email-verification", "inputs": []}

        with patch.object(roxy_registration, "_has_access_token", return_value=False), \
             patch.object(roxy_registration, "_email_otp_input_present", return_value=True), \
             patch.object(roxy_registration, "_email_otp_page_state", return_value=page_state), \
             patch.object(roxy_registration, "_click_resend_email_otp", side_effect=RuntimeError("missing resend")), \
             patch.object(roxy_registration, "_install_email_otp_send_probe"), \
             patch.object(roxy_registration, "_log_email_otp_send_probe", return_value=[]), \
             patch.object(roxy_registration, "_request_email_otp_send_via_browser") as direct_send, \
             patch.object(roxy_registration.time, "time", return_value=1000.0), \
             patch.object(roxy_registration.time, "sleep"):
            after_ts = roxy_registration._ensure_email_otp_ready(driver, "user@example.com", timeout=35)

        self.assertEqual(after_ts, 2222.0)
        direct_send.assert_not_called()

    def test_input_page_without_observed_send_request_calls_send_endpoint_when_enabled(self):
        """OTP_SEND_NAV_API_WHEN_UNCONFIRMED=True 时也不再从取码就绪路径导航强发
        （输入框在位即直接取码）；该开关只影响 _click_resend_email_otp 内部兜底。"""
        driver = SimpleNamespace()
        setattr(driver, "_registration_otp_trigger_ts", 2222.0)
        page_state = {"url": "https://auth.openai.com/email-verification", "inputs": []}

        with patch.object(roxy_registration._cfg, "OTP_SEND_NAV_API_WHEN_UNCONFIRMED", True), \
             patch.object(roxy_registration, "_has_access_token", return_value=False), \
             patch.object(roxy_registration, "_email_otp_input_present", return_value=True), \
             patch.object(roxy_registration, "_email_otp_page_state", return_value=page_state), \
             patch.object(roxy_registration, "_click_resend_email_otp", side_effect=RuntimeError("missing resend")), \
             patch.object(roxy_registration, "_install_email_otp_send_probe"), \
             patch.object(roxy_registration, "_log_email_otp_send_probe", return_value=[]), \
             patch.object(roxy_registration, "_request_email_otp_send_via_browser") as direct_send, \
             patch.object(roxy_registration.time, "time", return_value=1000.0), \
             patch.object(roxy_registration.time, "sleep"):
            after_ts = roxy_registration._ensure_email_otp_ready(driver, "user@example.com", timeout=35)

        self.assertEqual(after_ts, 2222.0)
        direct_send.assert_not_called()

    def test_preferred_after_ts_is_kept_when_direct_send_is_later(self):
        driver = SimpleNamespace()
        setattr(driver, "_registration_otp_trigger_ts", 2222.0)
        page_state = {"url": "https://auth.openai.com/email-verification", "inputs": []}

        def fake_direct_send(target_driver, reason=""):
            setattr(target_driver, "_registration_otp_trigger_ts", 3333.0)
            return {"ok": True, "status": 200}

        with patch.object(roxy_registration, "_has_access_token", return_value=False), \
             patch.object(roxy_registration, "_email_otp_input_present", return_value=True), \
             patch.object(roxy_registration, "_email_otp_page_state", return_value=page_state), \
             patch.object(roxy_registration, "_click_resend_email_otp", side_effect=RuntimeError("missing resend")), \
             patch.object(roxy_registration, "_install_email_otp_send_probe"), \
             patch.object(roxy_registration, "_log_email_otp_send_probe", return_value=[]), \
             patch.object(roxy_registration, "_request_email_otp_send_via_browser", side_effect=fake_direct_send), \
             patch.object(roxy_registration.time, "time", return_value=1000.0), \
             patch.object(roxy_registration.time, "sleep"):
            after_ts = roxy_registration._ensure_email_otp_ready(
                driver,
                "user@example.com",
                timeout=35,
                prefer_after_ts=1111.0,
            )

        self.assertEqual(after_ts, 1111.0)

    def test_restart_email_otp_flow_resubmits_email_and_uses_restart_trigger_ts(self):
        driver = SimpleNamespace()
        email = "user@example.com"

        def fake_submit(target_driver, target_email, attempts=3):
            self.assertIs(target_driver, driver)
            self.assertEqual(target_email, email)
            self.assertEqual(attempts, 2)
            setattr(target_driver, "_registration_otp_trigger_ts", 1234.0)
            return "password"

        def fake_ensure(target_driver, target_email, timeout=35, prefer_after_ts=None):
            self.assertIs(target_driver, driver)
            self.assertEqual(target_email, email)
            self.assertEqual(timeout, 35)
            self.assertEqual(prefer_after_ts, 1234.0)
            return 1234.0

        with patch.object(roxy_registration, "_check_manual_stop"), \
             patch.object(roxy_registration, "_safe_get") as safe_get, \
             patch.object(roxy_registration, "human_delay"), \
             patch.object(roxy_registration, "_install_email_otp_send_probe"), \
             patch.object(roxy_registration, "_page_warmup"), \
             patch.object(roxy_registration, "_maybe_accept"), \
             patch.object(roxy_registration, "_submit_email_and_wait_next", side_effect=fake_submit), \
             patch.object(roxy_registration, "_fill_password_page_if_present", return_value="new-password"), \
             patch.object(roxy_registration, "_ensure_email_otp_ready", side_effect=fake_ensure), \
             patch.object(roxy_registration.time, "time", return_value=1000.0):
            after_ts, password = roxy_registration._restart_email_otp_flow(
                driver,
                email,
                reason="test restart",
            )

        self.assertEqual(after_ts, 1234.0)
        self.assertEqual(password, "new-password")
        safe_get.assert_called_once()

    def test_passwordless_send_otp_probe_counts_as_send_success(self):
        rows = [{"url": "https://auth.openai.com/u/signup?intent=passwordless_signup_send_otp", "status": 302}]

        self.assertTrue(roxy_registration._email_otp_send_has_success(rows))

    def test_route_error_page_after_submit_returns_route_error(self):
        """提交后出现服务端路由错误页应返回 route_error，由上层走 Try again 恢复。"""
        driver = SimpleNamespace()
        page_state = {
            "url": "https://auth.openai.com/email-verification",
            "errors": [],
            "inputs": [],
            "text": "Oops, an error occurred!\nRoute Error (400 Invalid content type: text/html; charset=UTF-8)\nTry again",
        }

        with patch.object(roxy_registration, "_is_email_verification_page", return_value=True), \
             patch.object(roxy_registration, "_email_otp_page_state", return_value=page_state):
            outcome = roxy_registration._wait_after_email_otp_submit(driver, timeout=3)

        self.assertEqual(outcome, "route_error")

    def test_retry_otp_same_code_after_route_error_resubmits(self):
        """路由错误页恢复：点 Try again → 输入框回来 → 重填同一验证码并提交。"""
        driver = SimpleNamespace()
        driver.execute_script = lambda script, *args: driver  # 首次脚本命中 Try again 按钮元素
        driver._registration_log_prefix = "[测试注册]"

        with patch.object(roxy_registration, "_email_otp_input_present", return_value=True), \
             patch.object(roxy_registration, "_is_cloudflare_challenge_page", return_value=False), \
             patch.object(roxy_registration.time, "sleep"), \
             patch.object(roxy_registration, "_human_click") as click, \
             patch.object(roxy_registration, "_clear_otp_inputs") as clear, \
             patch.object(roxy_registration, "_type_otp") as type_otp, \
             patch.object(roxy_registration, "_click_continue") as cont, \
             patch.object(roxy_registration, "human_delay"), \
             patch.object(roxy_registration, "_refresh_or_reopen", side_effect=AssertionError("有点到 Try again 就不应刷新")) as refresh:
            ok = roxy_registration._retry_otp_same_code_after_route_error(driver, "868369")

        self.assertTrue(ok)
        click.assert_called_once()
        clear.assert_called_once()
        type_otp.assert_called_once()
        cont.assert_called_once()
        refresh.assert_not_called()

    def test_retry_otp_same_code_returns_false_when_input_never_returns(self):
        driver = SimpleNamespace()
        driver.execute_script = lambda script, *args: None
        driver._registration_log_prefix = "[测试注册]"

        with patch.object(roxy_registration, "_email_otp_input_present", return_value=False), \
             patch.object(roxy_registration, "_is_cloudflare_challenge_page", return_value=False), \
             patch.object(roxy_registration.time, "sleep"), \
             patch.object(roxy_registration.time, "time", side_effect=[0.0] + [100.0] * 20), \
             patch.object(roxy_registration, "_refresh_or_reopen"):
            ok = roxy_registration._retry_otp_same_code_after_route_error(driver, "868369")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
