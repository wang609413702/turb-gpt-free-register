# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db, rebind_service
from webui.app import create_app


class RebindPoolDbTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._patches = [
            patch.object(db, "_REBIND_EMAIL_JSON", root / "rebind.json"),
            patch.object(db, "_REBIND_EMAIL_TXT", root / "rebind.txt"),
            patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
            patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
            patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
            patch.object(db, "_VIEWER_HTML", root / "viewer.html"),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        (root / "accounts.json").write_text(
            json.dumps([{"id": 1, "email": "acc@test.com", "access_token": "tok"}]), encoding="utf-8"
        )

    def test_import_generic_api_and_cloudmail(self):
        inserted, skipped = db.import_rebind_emails([
            {"email": "a@test.com", "code_url": "https://x/code", "kind": "generic_api"},
            {"email": "b@test.com", "kind": "cloudmail"},
            {"email": "a@test.com", "code_url": "https://x/code"},  # 重复跳过
            {"email": "c@test.com", "kind": "generic_api"},  # 缺 code_url 跳过
        ])
        self.assertEqual((inserted, skipped), (2, 2))
        rows = db.list_rebind_email_pool()
        kinds = {r["email"]: r["kind"] for r in rows}
        self.assertEqual(kinds["a@test.com"], "generic_api")
        self.assertEqual(kinds["b@test.com"], "cloudmail")
        summary = db.rebind_email_pool_summary()
        self.assertEqual(summary["available"], 2)
        self.assertEqual(summary["total"], 2)

    def test_claim_marks_used_and_release_roundtrip(self):
        db.import_rebind_emails([{"email": "a@test.com", "code_url": "https://x/code"}])
        claimed = db.claim_rebind_email()
        self.assertEqual(claimed["email"], "a@test.com")
        self.assertEqual(claimed["status"], "used")
        self.assertEqual(db.count_rebind_emails_available(), 0)
        # 指定 kind 领取不到
        self.assertIsNone(db.claim_rebind_email(kind="cloudmail"))
        # 回收后再取可用
        db.release_rebind_email("a@test.com", status="available", note="回收")
        self.assertEqual(db.count_rebind_emails_available(), 1)
        self.assertIsNone(db.get_rebind_email_by_email("a@test.com")["used_at"])

    def test_delete_email(self):
        db.import_rebind_emails([{"email": "a@test.com", "code_url": "https://x/code"}])
        self.assertTrue(db.delete_rebind_email("a@test.com"))
        self.assertFalse(db.delete_rebind_email("a@test.com"))
        self.assertEqual(db.rebind_email_pool_summary()["total"], 0)


class AccountRebindStateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._patches = [
            patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
            patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
            patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
            patch.object(db, "_VIEWER_HTML", root / "viewer.html"),
            patch.object(db, "_REBIND_EMAIL_JSON", root / "rebind.json"),
            patch.object(db, "_REBIND_EMAIL_TXT", root / "rebind.txt"),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        (root / "accounts.json").write_text(
            json.dumps([{"id": 1, "email": "old@test.com", "access_token": "old-token"}]), encoding="utf-8"
        )
        (root / "rebind.json").write_text(
            json.dumps([{"id": 1, "email": "new@test.com", "code_url": "https://x/c",
                         "kind": "generic_api", "status": "used", "used_at": None, "note": None,
                         "imported_at": "2026-08-30T00:00:00", "copy_line": "new@test.com----https://x/c"}]),
            encoding="utf-8",
        )

    def test_claim_run_update_success_changes_email(self):
        self.assertTrue(db.claim_account_rebind(1, source="pool"))
        self.assertFalse(db.claim_account_rebind(1, source="pool"))  # 已 queued
        self.assertEqual(db.get_account(1)["rebind_status"], "queued")
        self.assertTrue(db.mark_account_rebind_running(1, new_email="new@test.com"))
        result = {
            "ok": True,
            "new_email": "new@test.com",
            "access_token": "new-token",
            "new_email_line": "new@test.com----https://x/c",
            "session": {"user": {"id": "u1"}, "account": {"planType": "free"}},
            "completed_at": "2026-08-30T01:00:00",
        }
        self.assertTrue(db.update_account_rebind(1, result))
        acc = db.get_account(1)
        self.assertEqual(acc["rebind_status"], "success")
        self.assertEqual(acc["email"], "new@test.com")
        self.assertEqual(acc["rebind_old_email"], "old@test.com")
        self.assertEqual(acc["access_token"], "new-token")
        self.assertEqual(acc["original_email_line"], "new@test.com----https://x/c")

    def test_update_failure_keeps_email_unless_effective(self):
        db.claim_account_rebind(1, source="pool")
        db.mark_account_rebind_running(1, new_email="new@test.com")
        db.update_account_rebind(1, {"ok": False, "error": "取码超时", "new_email": "new@test.com"})
        acc = db.get_account(1)
        self.assertEqual(acc["rebind_status"], "failed")
        self.assertEqual(acc["email"], "old@test.com")
        self.assertIsNone(acc["rebind_old_email"])

        # 换绑已在 OpenAI 侧生效但重新登录失败：email 也切换
        db.claim_account_rebind(1, source="pool")
        db.update_account_rebind(1, {"ok": False, "rebind_effective": True, "new_email": "new@test.com"})
        acc = db.get_account(1)
        self.assertEqual(acc["rebind_status"], "failed")
        self.assertEqual(acc["email"], "new@test.com")
        self.assertEqual(acc["rebind_old_email"], "old@test.com")

    def test_recover_interrupted(self):
        db.claim_account_rebind(1, source="pool")
        recovered = db.recover_interrupted_rebinds()
        self.assertEqual(recovered, 1)
        acc = db.get_account(1)
        self.assertEqual(acc["rebind_status"], "failed")
        self.assertIn("重新换绑", acc["rebind_error"])


class RebindWebUiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        # SQLite 存储按 _ACCOUNTS_JSON 等路径判断测试库位置，需一并隔离，
        # 否则会读写项目根目录的真实 turb.sqlite3。
        self._patches = [
            patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
            patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"),
            patch.object(db, "_OUTLOOK_JSON", root / "outlook.json"),
            patch.object(db, "_GENERIC_API_EMAIL_JSON", root / "generic_api.json"),
            patch.object(db, "_REBIND_EMAIL_JSON", root / "rebind.json"),
            patch.object(db, "_REBIND_EMAIL_TXT", root / "rebind.txt"),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.rebind_service.enqueue_account_rebind")
    @patch("webui.app.db.get_account")
    def test_rebind_bulk_queues_accounts(self, get_account, enqueue):
        get_account.return_value = {"id": 7, "email": "a@test.com"}
        enqueue.return_value = {"accepted": True, "busy": False}

        response = self.client.post(
            "/api/accounts/rebind", json={"account_ids": [7], "source": "cloudmail"}
        )

        self.assertEqual(response.status_code, 202)
        payload = response.get_json()
        self.assertEqual(payload["started_count"], 1)
        self.assertEqual(enqueue.call_args.kwargs["source"], "cloudmail")

    def test_rebind_bulk_rejects_bad_source(self):
        response = self.client.post("/api/accounts/rebind", json={"account_ids": [1], "source": "bad"})
        self.assertEqual(response.status_code, 400)

    @patch("webui.app.db.get_account")
    def test_rebind_bulk_reports_precheck_failure(self, get_account):
        get_account.return_value = {"id": 7, "email": "a@test.com"}
        response = self.client.post(
            "/api/accounts/rebind", json={"account_ids": [7], "source": "pool"}
        )
        self.assertEqual(response.status_code, 202)
        payload = response.get_json()
        self.assertEqual(payload["started_count"], 0)
        self.assertEqual(payload["failed_count"], 1)
        self.assertIn("换绑邮箱池", payload["failed"][0]["error"])

    def test_rebind_pool_import_parse(self):
        response = self.client.post(
            "/api/rebind-pool/import",
            json={"text": "a@test.com----https://x/code\nb@test.com\n# 注释\nbad-line"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["parsed"], 2)
        self.assertEqual(payload["inserted"], 2)


class EmailChangeErrorClassTests(unittest.TestCase):
    """换绑接口错误分类：reauth_required 不算验证码错误。"""

    class FakeResp:
        def __init__(self, status_code, text):
            self.status_code = status_code
            self.text = text

    class FakeSession:
        device_id = "d"

        def get_chatgpt_headers(self, referer=None):
            return {}

        def post(self, url, headers=None, data=None):
            return EmailChangeErrorClassTests.FakeResp.current

    def test_reauth_required_not_classified_as_otp_invalid(self):
        from core import email_change

        EmailChangeErrorClassTests.FakeResp.current = EmailChangeErrorClassTests.FakeResp(
            401, '{"detail":{"message":"Recent login required","code":"reauth_required"}}'
        )
        with self.assertRaises(email_change.ChangeEmailReauthRequiredError):
            email_change.change_email_begin(EmailChangeErrorClassTests.FakeSession(), "tok", "new@x.com")

    def test_invalid_code_classified_as_otp_invalid(self):
        from core import email_change

        EmailChangeErrorClassTests.FakeResp.current = EmailChangeErrorClassTests.FakeResp(
            400, '{"detail":{"message":"Invalid verification code"}}'
        )
        with self.assertRaises(email_change.ChangeEmailOtpInvalidError):
            email_change.change_email_verify(EmailChangeErrorClassTests.FakeSession(), "tok", "new@x.com", "123456")

    def test_plain_error_stays_generic(self):
        from core import email_change

        EmailChangeErrorClassTests.FakeResp.current = EmailChangeErrorClassTests.FakeResp(
            500, "internal server error"
        )
        with self.assertRaises(email_change.ChangeEmailError) as ctx:
            email_change.change_email_begin(EmailChangeErrorClassTests.FakeSession(), "tok", "new@x.com")
        self.assertNotIsInstance(ctx.exception, email_change.ChangeEmailOtpInvalidError)
        self.assertNotIsInstance(ctx.exception, email_change.ChangeEmailReauthRequiredError)


class RunRebindReleaseTests(unittest.TestCase):
    """失败时换绑邮箱的回收/标失败逻辑。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._patches = [
            patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
            patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
            patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
            patch.object(db, "_VIEWER_HTML", root / "viewer.html"),
            patch.object(db, "_REBIND_EMAIL_JSON", root / "rebind.json"),
            patch.object(db, "_REBIND_EMAIL_TXT", root / "rebind.txt"),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        (root / "accounts.json").write_text(
            json.dumps([{"id": 1, "email": "old@test.com", "access_token": ""}]), encoding="utf-8"
        )
        db.import_rebind_emails([{"email": "new@test.com", "code_url": "https://x/c"}])

    def test_login_failure_releases_pool_email(self):
        with patch.object(rebind_service, "_pick_rebind_proxy", return_value=("", "test")), \
             patch.object(rebind_service, "protocol_login", side_effect=RuntimeError("otp timeout")):
            result = rebind_service.run_rebind(account_id=1, email="old@test.com", source="pool")
        self.assertFalse(result["ok"])
        row = db.get_rebind_email_by_email("new@test.com")
        self.assertEqual(row["status"], "available")

    def test_begin_rejected_marks_pool_email_failed(self):
        from core import email_change

        db.claim_account_rebind(1, source="pool")

        class FakeSession:
            device_id = "d"

            def get_chatgpt_headers(self, referer=None):
                return {}

            def post(self, url, headers=None, data=None):
                class Resp:
                    status_code = 400
                    text = '{"error":"email not allowed"}'
                return Resp()

        class FakeClient:
            def __init__(self, *a, **k):
                pass

        with patch.object(rebind_service, "_pick_rebind_proxy", return_value=("", "test")), \
             patch.object(rebind_service, "_warm_chatgpt_session", return_value=FakeSession()), \
             patch.object(rebind_service, "change_email_begin", side_effect=email_change.ChangeEmailError("换绑请求失败: status=400")), \
             patch.object(rebind_service, "_pick_new_email", return_value=({"email": "new@test.com", "kind": "generic_api", "code_url": "https://x/c"}, "new@test.com", None)):
            result = rebind_service.run_rebind(
                account_id=1, email="old@test.com", source="pool", stored_token="tok",
            )
        self.assertFalse(result["ok"])
        row = db.get_rebind_email_by_email("new@test.com")
        self.assertEqual(row["status"], "failed")


class TokenPathTests(unittest.TestCase):
    """有可用 accessToken 时跳过旧邮箱登录；token 过期/被拒时回退登录。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._patches = [
            patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
            patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
            patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
            patch.object(db, "_VIEWER_HTML", root / "viewer.html"),
            patch.object(db, "_REBIND_EMAIL_JSON", root / "rebind.json"),
            patch.object(db, "_REBIND_EMAIL_TXT", root / "rebind.txt"),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        (root / "accounts.json").write_text(
            json.dumps([{"id": 1, "email": "old@test.com", "access_token": ""}]), encoding="utf-8"
        )
        db.import_rebind_emails([{"email": "new@test.com", "code_url": "https://x/c"}])

    @staticmethod
    def _jwt(exp_offset):
        import base64
        import json as jsonlib
        import time as timemod

        def b64(obj):
            return base64.urlsafe_b64encode(jsonlib.dumps(obj).encode()).decode().rstrip("=")

        payload = {"exp": timemod.time() + exp_offset} if exp_offset is not None else {}
        return f"{b64({})}.{b64(payload)}.sig"

    def test_token_expired_detection(self):
        self.assertTrue(rebind_service._token_expired(self._jwt(-10)))
        self.assertFalse(rebind_service._token_expired(self._jwt(3600)))
        self.assertFalse(rebind_service._token_expired("not-a-jwt"))

    def _run(self, stored_token, begin=None):
        """公共打桩：begin/verify 成功、取码固定、重登录打桩，统计登录调用次数。"""
        calls = {"login": 0, "warm": 0}

        def fake_warm(proxy):
            calls["warm"] += 1
            return object()

        def fake_login(email, fetch_otp, **kwargs):
            calls["login"] += 1
            return object(), {"access_token": "fresh-token", "session_info": {}}

        with patch.object(rebind_service, "_pick_rebind_proxy", return_value=("", "test")), \
             patch.object(rebind_service, "_warm_chatgpt_session", side_effect=fake_warm), \
             patch.object(rebind_service, "protocol_login", side_effect=fake_login), \
             patch.object(rebind_service, "change_email_begin",
                          side_effect=begin if begin is not None else (lambda *a, **k: {})), \
             patch.object(rebind_service, "change_email_verify", return_value={}), \
             patch.object(rebind_service, "_fetch_new_email_otp", return_value="123456"), \
             patch.object(rebind_service, "_pick_new_email",
                          return_value=({"email": "new@test.com", "kind": "generic_api",
                                         "code_url": "https://x/c", "copy_line": "new@test.com----https://x/c"},
                                        "new@test.com", None)), \
             patch.object(rebind_service, "_relogin_delay", return_value=None), \
             patch.object(rebind_service, "_relogin_new_email",
                          side_effect=lambda new_email, source, pool_row, result, **kw: {**result, "ok": True}):
            db.claim_account_rebind(1, source="pool")
            result = rebind_service.run_rebind(
                account_id=1, email="old@test.com", source="pool", stored_token=stored_token,
            )
        return calls, result

    def test_valid_token_skips_old_login(self):
        calls, result = self._run(self._jwt(3600))
        self.assertTrue(result["ok"])
        self.assertEqual(calls["login"], 0)
        self.assertEqual(calls["warm"], 1)

    def test_expired_token_falls_back_to_login(self):
        calls, result = self._run(self._jwt(-10))
        self.assertTrue(result["ok"])
        self.assertEqual(calls["login"], 1)
        self.assertEqual(calls["warm"], 0)

    def test_missing_token_uses_login(self):
        calls, result = self._run("")
        self.assertTrue(result["ok"])
        self.assertEqual(calls["login"], 1)
        self.assertEqual(calls["warm"], 0)

    def test_reauth_required_falls_back_to_login_without_consuming_attempt(self):
        from core import email_change

        state = {"begin": 0}

        def fake_begin(session, token, new_email):
            state["begin"] += 1
            if state["begin"] == 1:
                raise email_change.ChangeEmailReauthRequiredError(
                    "换绑接口要求最近登录态: status=401, "
                    'body={"detail":{"message":"Recent login required","code":"reauth_required"}}'
                )
            return {}

        calls, result = self._run(self._jwt(3600), begin=fake_begin)
        self.assertTrue(result["ok"])
        self.assertEqual(state["begin"], 2)   # 第一次被拒，回退登录后重试成功
        self.assertEqual(calls["login"], 1)   # 回退发生了一次旧邮箱登录
        self.assertEqual(calls["warm"], 1)


if __name__ == "__main__":
    unittest.main()
