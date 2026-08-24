# -*- coding: utf-8 -*-
import inspect
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from core.chatgpt_momo import (
    checkout_session_kind,
    check_account_gcash,
    check_account_kakao,
    check_account_paypal,
    check_account_ideal,
    check_account_gopay,
    classify_checkout_error,
    is_risk_blocked_error,
)


RISK_BODY = '{"detail":"Our systems have detected unusual activity. Please try again later."}'


class _OkCheckoutResponse:
    """OpenAI 自定义结账成功响应（oaics），单请求即可完成 MoMo 探测。"""

    status_code = 200
    text = "{}"
    headers = {"content-type": "application/json"}

    def json(self):
        return {
            "checkout_session_id": "oaics_test123",
            "checkout_provider": "open_ai",
            "mode": "subscription",
            "payment_method_types": ["card", "momo"],
        }


class _FakeRiskResponse:
    status_code = 400
    text = RISK_BODY
    headers = {"content-type": "application/json"}

    def json(self):
        return {"detail": "Our systems have detected unusual activity. Please try again later."}


class _FakeRiskEnv:
    """只实现 _run_probe 用到的 BrowserSession 接口。"""

    device_id = "dev"

    def __init__(self):
        self.last_headers = None

    def navigator_language(self):
        return "en-US"

    def _get_common_headers(self):
        return {"user-agent": "test-ua"}

    def post(self, *args, **kwargs):
        self.last_headers = kwargs.get("headers")
        return _FakeRiskResponse()

    session = type("S", (), {"close": staticmethod(lambda: None), "timeout": 10})()


class RiskBlockedTests(unittest.TestCase):
    """OpenAI 风控（unusual activity）必须立即失败、不重试、保留服务端原文。"""

    def test_detects_risk_blocked_body(self):
        self.assertTrue(is_risk_blocked_error(RISK_BODY))
        self.assertEqual(classify_checkout_error(_FakeRiskResponse()), "risk_blocked")

    def test_risk_blocked_fails_immediately_without_retry(self):
        from core import chatgpt_momo as cm

        route = {
            "proxy": "",
            "proxy_mode": "pool",
            "network_route": "proxy",
            "proxy_used": "socks5h://***:***@x:1",
            "proxy_fallback_reason": None,
        }
        env = _FakeRiskEnv()
        with patch.object(cm, "resolve_plan_check_route", return_value=route), \
             patch.object(cm, "BrowserSession", return_value=env), \
             patch("core.checkout_sentinel.generate_checkout_sentinel_headers", return_value=None):
            result = check_account_gcash("fake-token", max_attempts=5, retry_delay=0)

        self.assertFalse(result["ok"])
        self.assertEqual(result["decision"], "risk_blocked")
        self.assertEqual(result["attempt_count"], 1)
        self.assertTrue(result["error"].startswith("Our systems"))
        self.assertFalse(result.get("retryable", False))


class CheckoutSentinelTests(unittest.TestCase):
    """Sentinel 令牌注入共享检测内核，全部支付检测生效。"""

    _ROUTE = {
        "proxy": "",
        "proxy_mode": "pool",
        "network_route": "proxy",
        "proxy_used": "socks5h://***:***@x:1",
        "proxy_fallback_reason": None,
    }

    def _ok_env(self):
        response = _OkCheckoutResponse()
        env = _FakeRiskEnv()
        env.post = lambda url, **kwargs: (setattr(env, "last_headers", kwargs.get("headers")), response)[1]
        return env

    def test_sentinel_headers_attached_to_checkout_request(self):
        from core import chatgpt_momo as cm
        from core.chatgpt_momo import check_account_momo

        sentinel = {"OpenAI-Sentinel-Token": "sentinel-tok", "OpenAI-Sentinel-SO-Token": "so-tok"}
        env = self._ok_env()
        with patch.object(cm, "resolve_plan_check_route", return_value=dict(self._ROUTE)), \
             patch.object(cm, "BrowserSession", return_value=env), \
             patch("core.checkout_sentinel.generate_checkout_sentinel_headers", return_value=sentinel):
            result = check_account_momo("fake-token", max_attempts=1)

        self.assertTrue(result["ok"])
        self.assertTrue(result["sentinel_attached"])
        sent = {str(k).lower(): v for k, v in env.last_headers.items()}
        self.assertEqual(sent["openai-sentinel-token"], "sentinel-tok")
        self.assertEqual(sent["openai-sentinel-so-token"], "so-tok")

    def test_sentinel_failure_degrades_to_plain_request(self):
        from core import chatgpt_momo as cm
        from core.chatgpt_momo import check_account_momo

        env = self._ok_env()
        with patch.object(cm, "resolve_plan_check_route", return_value=dict(self._ROUTE)), \
             patch.object(cm, "BrowserSession", return_value=env), \
             patch("core.checkout_sentinel.generate_checkout_sentinel_headers", side_effect=RuntimeError("node missing")):
            result = check_account_momo("fake-token", max_attempts=1)

        self.assertTrue(result["ok"])
        self.assertFalse(result["sentinel_attached"])
        sent = {str(k).lower() for k in env.last_headers}
        self.assertNotIn("openai-sentinel-token", sent)




class CheckoutSessionKindTests(unittest.TestCase):
    def test_derives_oaics_and_cs(self):
        self.assertEqual(checkout_session_kind("oaics_abc", ""), "oaics")
        self.assertEqual(checkout_session_kind("cs_abc", "stripe"), "cs")
        self.assertEqual(checkout_session_kind("anything", "open_ai"), "oaics")
        self.assertIsNone(checkout_session_kind("xyz", ""))
        self.assertIsNone(checkout_session_kind("", ""))


class PaymentCheckWrapperTests(unittest.TestCase):
    def test_kakao_paypal_call_kernel_with_expected_args(self):
        from core import chatgpt_momo as cm

        captured = {}

        def fake_kernel(token, *, payment_method, country, currency, label, **kw):
            captured.update(payment_method=payment_method, country=country,
                            currency=currency, label=label)
            return {"ok": True}

        with patch.object(cm, "_check_payment_support", side_effect=fake_kernel):
            check_account_kakao("tok")
            self.assertEqual(captured, dict(payment_method="kakao_pay", country="KR",
                                            currency="KRW", label="Kakao"))
            # PayPal 默认走 BR 地区（BR/BRL）
            result = check_account_paypal("tok")
            self.assertEqual(captured, dict(payment_method="paypal", country="BR",
                                            currency="BRL", label="PayPal"))
            self.assertEqual(result.get("region"), "br")
            # region="th" 走 TH/THB
            result = check_account_paypal("tok", region="th")
            self.assertEqual(captured, dict(payment_method="paypal", country="TH",
                                            currency="THB", label="PayPal"))
            self.assertEqual(result.get("region"), "th")
            # region="de" 走 DE/EUR
            result = check_account_paypal("tok", region="de")
            self.assertEqual(captured, dict(payment_method="paypal", country="DE",
                                            currency="EUR", label="PayPal"))
            self.assertEqual(result.get("region"), "de")
            # 未知地区回退 br
            check_account_paypal("tok", region="xx")
            self.assertEqual(captured["country"], "BR")
            self.assertEqual(captured["currency"], "BRL")

    def test_paypal_region_pick_uses_matching_pool(self):
        from config import proxy as proxy_cfg

        with patch.object(proxy_cfg, "PAYPAL_BR_PROXY_POOL", ["socks5://1.2.3.4:1080"]), \
             patch.object(proxy_cfg, "PAYPAL_TH_PROXY_POOL", ["5.6.7.8:1080"]), \
             patch.object(proxy_cfg, "PAYPAL_DE_PROXY_POOL", ["socks5h://user:pass@9.10.11.12:1080"]):
            self.assertEqual(proxy_cfg.pick_paypal_proxy("br"), "socks5h://1.2.3.4:1080")
            self.assertEqual(proxy_cfg.pick_paypal_proxy("th"), "socks5h://5.6.7.8:1080")
            self.assertEqual(proxy_cfg.pick_paypal_proxy("de"), "socks5h://user:pass@9.10.11.12:1080")
            # 未知地区回退 BR 池；池为空返回空串（直连）
            self.assertEqual(proxy_cfg.pick_paypal_proxy("xx"), "socks5h://1.2.3.4:1080")
            with patch.object(proxy_cfg, "PAYPAL_TH_PROXY_POOL", []):
                self.assertEqual(proxy_cfg.pick_paypal_proxy("th"), "")

    def test_momo_settings_read_config_with_clamp(self):
        """检测超时/重试参数从 config 读取并对所有检测生效，越界值被钳制。"""
        from core import chatgpt_momo as cm
        from config import proxy as proxy_cfg

        # 默认配置值生效
        timeout, attempts, delay = cm._momo_settings(None, None, None)
        self.assertEqual(timeout, proxy_cfg.MOMO_CHECK_TIMEOUT)
        self.assertEqual(attempts, proxy_cfg.MOMO_CHECK_MAX_ATTEMPTS)
        self.assertEqual(delay, proxy_cfg.MOMO_CHECK_RETRY_DELAY)

        # 越界配置被钳制到 1-60 / 1-6 / 0-30
        with patch.object(proxy_cfg, "MOMO_CHECK_TIMEOUT", 99.0), \
             patch.object(proxy_cfg, "MOMO_CHECK_MAX_ATTEMPTS", 9), \
             patch.object(proxy_cfg, "MOMO_CHECK_RETRY_DELAY", 60.0):
            self.assertEqual(cm._momo_settings(None, None, None), (60.0, 6, 30.0))

        # 显式传参优先于配置
        self.assertEqual(cm._momo_settings(5, 2, 0.5), (5, 2, 0.5))

    def test_gopay_calls_kernel_with_id_idr(self):
        from core import chatgpt_momo as cm

        captured = {}

        def fake_kernel(token, *, payment_method, country, currency, label, custom_method_ids, **kw):
            captured.update(payment_method=payment_method, country=country,
                            currency=currency, label=label, custom_method_ids=custom_method_ids)
            return {"ok": True}

        with patch.object(cm, "_check_payment_support", side_effect=fake_kernel):
            check_account_gopay("tok")
            self.assertEqual(captured, dict(payment_method="gopay", country="ID",
                                            currency="IDR", label="GoPay", custom_method_ids=[]))

    def test_gopay_pick_uses_gopay_pool(self):
        from config import proxy as proxy_cfg

        with patch.object(proxy_cfg, "GOPAY_PROXY_POOL", ["10.20.30.40:1080"]):
            self.assertEqual(proxy_cfg.pick_gopay_proxy(), "socks5h://10.20.30.40:1080")
        with patch.object(proxy_cfg, "GOPAY_PROXY_POOL", []):
            self.assertEqual(proxy_cfg.pick_gopay_proxy(), "")

    def test_wrappers_keep_proxy_keyword_signature(self):
        kakao_sig = inspect.signature(check_account_kakao)
        paypal_sig = inspect.signature(check_account_paypal)
        gopay_sig = inspect.signature(check_account_gopay)
        for sig in (kakao_sig, paypal_sig, gopay_sig):
            self.assertEqual(list(sig.parameters)[0], "token")
            self.assertIn("proxy", sig.parameters)

    def test_ideal_calls_kernel_with_nl_eur(self):
        from core import chatgpt_momo as cm

        captured = {}

        def fake_kernel(token, *, payment_method, country, currency, label, **kw):
            captured.update(payment_method=payment_method, country=country,
                            currency=currency, label=label)
            return {"ok": True}

        with patch.object(cm, "_check_payment_support", side_effect=fake_kernel):
            check_account_ideal("tok")
            self.assertEqual(captured, dict(payment_method="ideal", country="NL",
                                            currency="EUR", label="IDEAL"))


class KakaoPaypalDbTests(unittest.TestCase):
    def test_update_writes_target_and_session_kind(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"), \
                 patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"), \
                 patch.object(db, "_OUTLOOK_JSON", root / "outlook.json"), \
                 patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"), \
                 patch.object(db, "_TOKENS_TXT", root / "tokens.txt"), \
                 patch.object(db, "_OUTLOOK_TXT", root / "outlook.txt"), \
                 patch.object(db, "_VIEWER_HTML", root / "viewer.html"):
                db.insert_account(email="pay@test.com", access_token="tok")
                db.update_account_kakao_check(
                    email="pay@test.com",
                    result={"ok": True, "has_target": True, "session_kind": "cs",
                            "decision": "available", "decision_text": "ok",
                            "supported": True, "checked_at": "2026-01-01T00:00:00"},
                )
                db.update_account_paypal_check(
                    email="pay@test.com",
                    result={"ok": True, "has_target": False, "session_kind": "cs",
                            "decision": "not_enabled", "checked_at": "2026-01-01T00:00:00"},
                )
                row = db.get_account_by_email("pay@test.com")
                self.assertTrue(row["kakao_has_kakao"])
                self.assertEqual(row["kakao_session_kind"], "cs")
                self.assertFalse(row["paypal_has_paypal"])
                self.assertEqual(row["paypal_session_kind"], "cs")

                # 中断恢复把 queued/running 置 failed
                db.claim_account_kakao_check(email="pay@test.com", trigger="t")
                n = db.recover_interrupted_kakao_checks()
                self.assertGreaterEqual(n, 1)
                self.assertEqual(db.get_account_by_email("pay@test.com")["kakao_check_status"], "failed")

    def test_paypal_region_recorded_on_claim_and_update(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"), \
                 patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"), \
                 patch.object(db, "_OUTLOOK_JSON", root / "outlook.json"), \
                 patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"), \
                 patch.object(db, "_TOKENS_TXT", root / "tokens.txt"), \
                 patch.object(db, "_OUTLOOK_TXT", root / "outlook.txt"), \
                 patch.object(db, "_VIEWER_HTML", root / "viewer.html"):
                db.insert_account(email="region@test.com", access_token="tok")
                # 入队时记录地区，供前端展示 TH检测/BR检测
                self.assertTrue(db.claim_account_paypal_check(
                    email="region@test.com", trigger="manual", region="th"))
                self.assertEqual(db.get_account_by_email("region@test.com")["paypal_check_region"], "th")
                # 结果携带地区时覆盖
                db.update_account_paypal_check(
                    email="region@test.com",
                    result={"ok": True, "has_target": True, "checked_at": "2026-01-01T00:00:00", "region": "br"},
                )
                self.assertEqual(db.get_account_by_email("region@test.com")["paypal_check_region"], "br")
                # 轻量状态快照必须携带地区，否则前端增量轮询会沿用上一次检测的旧地区提示
                snap = db.list_account_plan_check_statuses(limit=50)
                item = next(i for i in snap["items"] if i["email"] == "region@test.com")
                self.assertEqual(item.get("paypal_check_region"), "br")

    def test_ideal_db_write_and_recover(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"), \
                 patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"), \
                 patch.object(db, "_OUTLOOK_JSON", root / "outlook.json"), \
                 patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"), \
                 patch.object(db, "_TOKENS_TXT", root / "tokens.txt"), \
                 patch.object(db, "_OUTLOOK_TXT", root / "outlook.txt"), \
                 patch.object(db, "_VIEWER_HTML", root / "viewer.html"):
                db.insert_account(email="ideal@test.com", access_token="tok")
                db.update_account_ideal_check(
                    email="ideal@test.com",
                    result={"ok": True, "has_target": True, "session_kind": "cs",
                            "checked_at": "2026-01-01T00:00:00"},
                )
                row = db.get_account_by_email("ideal@test.com")
                self.assertTrue(row["ideal_has_ideal"])
                self.assertEqual(row["ideal_session_kind"], "cs")

                db.claim_account_ideal_check(email="ideal@test.com", trigger="t")
                self.assertGreaterEqual(db.recover_interrupted_ideal_checks(), 1)
                self.assertEqual(db.get_account_by_email("ideal@test.com")["ideal_check_status"], "failed")

    def test_gopay_db_write_snapshot_and_recover(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"), \
                 patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"), \
                 patch.object(db, "_OUTLOOK_JSON", root / "outlook.json"), \
                 patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"), \
                 patch.object(db, "_TOKENS_TXT", root / "tokens.txt"), \
                 patch.object(db, "_OUTLOOK_TXT", root / "outlook.txt"), \
                 patch.object(db, "_VIEWER_HTML", root / "viewer.html"):
                db.insert_account(email="gopay@test.com", access_token="tok")
                db.update_account_gopay_check(
                    email="gopay@test.com",
                    result={"ok": True, "has_target": True, "session_kind": "oaics",
                            "checked_at": "2026-01-01T00:00:00"},
                )
                row = db.get_account_by_email("gopay@test.com")
                self.assertTrue(row["gopay_has_gopay"])
                self.assertEqual(row["gopay_session_kind"], "oaics")
                # 轻量快照携带 GoPay 字段，保证前端轮询能合并状态
                snap = db.list_account_plan_check_statuses(limit=50)
                item = next(i for i in snap["items"] if i["email"] == "gopay@test.com")
                self.assertTrue(item.get("gopay_has_gopay"))
                self.assertEqual(item.get("gopay_check_status"), "success")

                db.claim_account_gopay_check(email="gopay@test.com", trigger="t")
                self.assertGreaterEqual(db.recover_interrupted_gopay_checks(), 1)
                self.assertEqual(db.get_account_by_email("gopay@test.com")["gopay_check_status"], "failed")


class FastSaveTests(unittest.TestCase):
    """检测状态写入走快速保存：只写 JSON，不重建 TXT/静态查看页。"""

    def test_status_update_skips_txt_and_viewer(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            accounts_path = root / "accounts.json"
            accounts_txt = root / "accounts.txt"
            tokens_txt = root / "tokens.txt"
            viewer = root / "viewer.html"
            with patch.object(db, "_ACCOUNTS_JSON", accounts_path), \
                 patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"), \
                 patch.object(db, "_OUTLOOK_JSON", root / "outlook.json"), \
                 patch.object(db, "_ACCOUNTS_TXT", accounts_txt), \
                 patch.object(db, "_TOKENS_TXT", tokens_txt), \
                 patch.object(db, "_OUTLOOK_TXT", root / "outlook.txt"), \
                 patch.object(db, "_VIEWER_HTML", viewer):
                # 新账号走全量保存：JSON + TXT 立即生成，viewer 由上游防抖线程异步生成。
                with patch.object(db, "_VIEWER_DEBOUNCE_SECONDS", 0.01):
                    db.insert_account(email="fast@test.com", access_token="tok")
                    time.sleep(0.05)
                self.assertTrue(accounts_path.exists())
                self.assertTrue(accounts_txt.exists())
                self.assertTrue(viewer.exists())

                # 删掉 TXT/viewer，再触发一次检测状态写入（快速保存）。
                accounts_txt.unlink()
                viewer.unlink()
                db.update_account_momo_check(
                    email="fast@test.com",
                    result={"ok": True, "has_target": True, "session_kind": "cs",
                            "checked_at": "2026-01-01T00:00:00"},
                )
                # 快速保存不应重建 TXT 与 viewer，JSON 仍更新。
                self.assertFalse(accounts_txt.exists())
                self.assertFalse(viewer.exists())
                row = db.get_account_by_email("fast@test.com")
                self.assertEqual(row["momo_check_status"], "success")
                self.assertEqual(row["momo_session_kind"], "cs")

    def test_update_backfills_exit_from_result(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"), \
                 patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"), \
                 patch.object(db, "_OUTLOOK_JSON", root / "outlook.json"), \
                 patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"), \
                 patch.object(db, "_TOKENS_TXT", root / "tokens.txt"), \
                 patch.object(db, "_OUTLOOK_TXT", root / "outlook.txt"), \
                 patch.object(db, "_VIEWER_HTML", root / "viewer.html"):
                db.insert_account(email="exit@test.com", access_token="tok")
                db.update_account_gcash_check(
                    email="exit@test.com",
                    result={"ok": True, "has_target": False, "session_kind": "oaics",
                            "exit_ip": "1.2.3.4", "exit_country": "PH",
                            "checked_at": "2026-01-01T00:00:00"},
                )
                row = db.get_account_by_email("exit@test.com")
                self.assertEqual(row["gcash_exit_ip"], "1.2.3.4")
                self.assertEqual(row["gcash_exit_country"], "PH")
                self.assertEqual(row["gcash_session_kind"], "oaics")


if __name__ == "__main__":
    unittest.main()
