# -*- coding: utf-8 -*-
"""UPI 支付检测测试：检测参数、代理池、DB 状态流转与 WebUI 接口。"""
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from config import proxy as proxy_cfg
from core import db, upi_check_service
from core.chatgpt_momo import check_account_upi
from webui.app import create_app


class UpiCheckCoreTests(unittest.TestCase):
    def test_check_account_upi_uses_in_inr_and_pool_whitelist(self):
        captured = {}

        def fake_support(token, **kwargs):
            captured.update(kwargs)
            return {"ok": True, "has_target": True}

        with patch("core.chatgpt_momo._check_payment_support", side_effect=fake_support), patch.object(
            proxy_cfg, "UPI_CUSTOM_PAYMENT_METHOD_IDS", ["cpmt_123"], create=True
        ):
            result = check_account_upi("token")
        self.assertTrue(result["ok"])
        self.assertEqual(captured["payment_method"], "upi")
        self.assertEqual(captured["country"], "IN")
        self.assertEqual(captured["currency"], "INR")
        self.assertEqual(captured["label"], "UPI")
        self.assertEqual(captured["custom_method_ids"], ["cpmt_123"])

    def test_pick_upi_proxy_normalizes_pool_lines(self):
        with patch.object(proxy_cfg, "UPI_PROXY_POOL", ["1.2.3.4:1080:user:pass"], create=True):
            self.assertEqual(proxy_cfg.pick_upi_proxy(), "socks5h://user:pass@1.2.3.4:1080")
        with patch.object(proxy_cfg, "UPI_PROXY_POOL", [], create=True):
            self.assertEqual(proxy_cfg.pick_upi_proxy(), "")


class UpiCheckDbTests(unittest.TestCase):
    def _db_patches(self, root: Path):
        return (
            patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
            patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"),
            patch.object(db, "_OUTLOOK_JSON", root / "outlook.json"),
            patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
            patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
            patch.object(db, "_OUTLOOK_TXT", root / "outlook.txt"),
            patch.object(db, "_VIEWER_HTML", root / "viewer.html"),
        )

    def test_claim_run_update_flow_and_projection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with ExitStack() as stack:
                for item in self._db_patches(root):
                    stack.enter_context(item)
                account_id = db.insert_account(email="upi@example.com", access_token="token")

                # 未排队时不能直接标记运行
                self.assertFalse(db.mark_account_upi_check_running(account_id))

                self.assertTrue(db.claim_account_upi_check(acc_id=account_id, trigger="manual"))
                # 重复占用失败（busy）
                self.assertFalse(db.claim_account_upi_check(acc_id=account_id, trigger="manual"))
                self.assertTrue(db.mark_account_upi_check_running(account_id))
                db.update_account_check_route(account_id, "upi", "socks5h://user:pass@1.2.3.4:1080")

                self.assertTrue(db.update_account_upi_check(
                    acc_id=account_id,
                    result={
                        "ok": True,
                        "checked_at": "2026-09-16T12:00:00",
                        "decision": "available",
                        "decision_text": "支持 UPI 支付",
                        "has_target": True,
                        "supported": True,
                        "conclusive": True,
                        "methods": ["upi"],
                        "session_kind": "cs",
                        "promo_granted": False,
                    },
                ))
                account = db.get_account(account_id)
                self.assertEqual(account["upi_check_status"], "success")
                self.assertIs(account["upi_has_upi"], True)
                self.assertEqual(account["upi_decision"], "available")
                self.assertEqual(account["upi_session_kind"], "cs")

                item = db.list_account_plan_check_statuses(limit=10)["items"][0]
                self.assertEqual(item["upi_check_status"], "success")
                self.assertIs(item["upi_has_upi"], True)

    def test_failed_check_keeps_error_and_projection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with ExitStack() as stack:
                for item in self._db_patches(root):
                    stack.enter_context(item)
                account_id = db.insert_account(email="upi-fail@example.com", access_token="token")
                db.claim_account_upi_check(acc_id=account_id)
                db.mark_account_upi_check_running(account_id)
                self.assertTrue(db.update_account_upi_check(
                    acc_id=account_id,
                    result={"ok": False, "error": "checkout 创建失败", "decision": "checkout_failed"},
                ))
                item = db.list_account_plan_check_statuses(limit=10)["items"][0]
                self.assertEqual(item["upi_check_status"], "failed")
                self.assertEqual(item["upi_check_error"], "checkout 创建失败")
                self.assertNotIn("upi_has_upi", item)

    def test_recover_interrupted_upi_checks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with ExitStack() as stack:
                for item in self._db_patches(root):
                    stack.enter_context(item)
                account_id = db.insert_account(email="upi-stuck@example.com", access_token="token")
                db.claim_account_upi_check(acc_id=account_id)
                recovered = db.recover_interrupted_upi_checks()
                self.assertEqual(recovered, 1)
                account = db.get_account(account_id)
                self.assertEqual(account["upi_check_status"], "failed")
                self.assertIn("UPI 检测中断", account["upi_check_error"])
                # 再次恢复无遗留
                self.assertEqual(db.recover_interrupted_upi_checks(), 0)


class UpiCheckServiceTests(unittest.TestCase):
    def test_enqueue_rejects_missing_token_and_busy(self):
        self.assertFalse(upi_check_service.enqueue_account_upi_check(
            account_id=1, email="a@x.com", access_token="", trigger="manual",
        )["accepted"])
        with patch.object(upi_check_service.db, "claim_account_upi_check", return_value=False):
            queued = upi_check_service.enqueue_account_upi_check(
                account_id=1, email="a@x.com", access_token="tok", trigger="manual",
            )
        self.assertTrue(queued["busy"])

    @patch("core.upi_check_service._QUEUE_SLOTS")
    @patch("core.upi_check_service.db.update_account_upi_check")
    @patch("core.upi_check_service.db.mark_account_upi_check_running", return_value=True)
    @patch("core.upi_check_service.check_account_upi")
    def test_run_account_upi_check_uses_pool_proxy(self, check_upi, mark_running, update_upi, queue_slots):
        check_upi.return_value = {"ok": True, "has_target": True}
        with patch.object(upi_check_service, "_wait_for_rate_slot"), patch.object(
            proxy_cfg, "UPI_PROXY_POOL", ["1.2.3.4:1080:user:pass"], create=True
        ):
            result = upi_check_service._run_account_upi_check(
                account_id=1,
                email="a@x.com",
                access_token="tok",
                trigger="manual",
                proxy=None,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(check_upi.call_args.kwargs["proxy"], "socks5h://user:pass@1.2.3.4:1080")
        update_upi.assert_called_once()


class UpiCheckWebuiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.upi_check_service.enqueue_account_upi_check")
    @patch("webui.app.db.get_account")
    def test_single_upi_check_endpoint(self, get_account, enqueue_upi):
        get_account.return_value = {"id": 9, "email": "a@x.com", "access_token": "tok"}
        enqueue_upi.return_value = {"accepted": True, "busy": False}
        resp = self.client.post("/api/accounts/upi-check", json={"account_id": 9})
        self.assertEqual(resp.status_code, 202)
        self.assertTrue(resp.get_json()["ok"])
        self.assertEqual(enqueue_upi.call_args.kwargs["trigger"], "manual")

    @patch("webui.app.upi_check_service.enqueue_account_upi_check")
    @patch("webui.app.db.get_account")
    def test_bulk_upi_check_endpoint(self, get_account, enqueue_upi):
        # 字符串 "9" 与数字 9 去重；不存在的 10 计入 skipped。
        get_account.side_effect = lambda acc_id: (
            {"id": acc_id, "email": "a@x.com", "access_token": "tok"} if int(acc_id) == 9 else None
        )
        enqueue_upi.return_value = {"accepted": True, "busy": False}
        resp = self.client.post("/api/accounts/upi-check-bulk", json={"account_ids": [9, "9", 10]})
        self.assertEqual(resp.status_code, 202)
        data = resp.get_json()
        self.assertEqual(data["started_count"], 1)
        self.assertEqual(data["skipped_count"], 1)
        self.assertEqual(enqueue_upi.call_args.kwargs["trigger"], "manual_bulk")

    @patch("webui.app.upi_check_service.enqueue_account_upi_check")
    def test_upi_check_busy_maps_to_409(self, enqueue_upi):
        enqueue_upi.return_value = {"accepted": False, "busy": True, "error": "该账号正在检测 UPI"}
        with patch("webui.app.db.get_account", return_value={"id": 9, "email": "a@x.com", "access_token": "tok"}):
            resp = self.client.post("/api/accounts/upi-check", json={"account_id": 9})
        self.assertEqual(resp.status_code, 409)

    def test_status_snapshot_includes_upi_fields(self):
        with patch("webui.app.db.list_account_plan_check_statuses", return_value={"items": [], "revision": "r"}), \
             patch("webui.app.plan_check_service.queue_settings", return_value={}), \
             patch("webui.app.trial_check_service.queue_settings", return_value={}), \
             patch("webui.app.momo_check_service.queue_settings", return_value={}), \
             patch("webui.app.gcash_check_service.queue_settings", return_value={}), \
             patch("webui.app.kakao_check_service.queue_settings", return_value={}), \
             patch("webui.app.paypal_check_service.queue_settings", return_value={}), \
             patch("webui.app.ideal_check_service.queue_settings", return_value={}), \
             patch("webui.app.gopay_check_service.queue_settings", return_value={}), \
             patch("webui.app.upi_check_service.queue_settings", return_value={"workers": 3}):
            resp = self.client.get("/api/accounts/plan-check-status")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["upi_queue"], {"workers": 3})


if __name__ == "__main__":
    unittest.main()
