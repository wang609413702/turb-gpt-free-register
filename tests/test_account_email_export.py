# -*- coding: utf-8 -*-
"""账号页「导出邮箱」接口回归测试：导出格式与邮箱池 copy_line 一致。"""
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from webui.app import _account_secret_value


def _row(**overrides):
    row = {"id": 1, "email": "a@b.c"}
    row.update(overrides)
    return row


class OriginalEmailLineTests(unittest.TestCase):
    def test_outlook_account_exports_material_line(self):
        row = _row(
            original_email_line="a@b.c----pwd----cid----rt",
            copy_line="a@b.c----pwd----cid----rt----token----totp",
        )
        self.assertEqual(_account_secret_value(row, "original_email_line"), "a@b.c----pwd----cid----rt")

    def test_generic_api_account_exports_email_code_url(self):
        row = _row(original_email_line="a@b.c----https://code.url/x")
        self.assertEqual(_account_secret_value(row, "original_email_line"), "a@b.c----https://code.url/x")

    def test_account_without_material_falls_back_to_email(self):
        self.assertEqual(_account_secret_value(_row(), "original_email_line"), "a@b.c")

    def test_missing_material_rebuilt_from_email_pool(self):
        """generic_api 注册账号素材只在邮箱池；导出时按邮箱回查重建。"""
        from webui.app import _account_secret_value as value_of
        with patch("webui.app.db.get_outlook_by_email", return_value=None), \
             patch("webui.app.db.get_generic_api_email_by_email",
                   return_value={"email": "a@b.c", "copy_line": "a@b.c----https://code.url/x"}):
            self.assertEqual(value_of(_row(), "original_email_line"), "a@b.c----https://code.url/x")

    def test_unknown_field_still_rejected(self):
        # password/totp_secret/totp_code 已是合法导出字段；未知字段仍拒绝。
        with self.assertRaises(ValueError):
            _account_secret_value(_row(), "no_such_field")


class SecretBulkEndpointTests(unittest.TestCase):
    def test_bulk_export_returns_material_lines(self):
        from webui.app import create_app
        from core import db

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with ExitStack() as stack:
                for p in (
                    patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
                    patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"),
                    patch.object(db, "_OUTLOOK_JSON", root / "outlook.json"),
                    patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
                    patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
                    patch.object(db, "_OUTLOOK_TXT", root / "outlook.txt"),
                    patch.object(db, "_VIEWER_HTML", root / "viewer.html"),
                ):
                    stack.enter_context(p)
                    id1 = db.insert_account(email="x@y.z", access_token="tok")
                    # 直接补素材行（insert_account 只在邮箱池命中时才写 original_email_line）
                    acc = db.get_account(id1)
                    acc["original_email_line"] = "x@y.z----p----c----r"
                    db._save_accounts([acc])
                    id2 = db.insert_account(email="plain@y.z", access_token="tok2")

                    app = create_app(auth_code="test-auth")
                    client = app.test_client()
                    resp = client.post("/api/accounts/secret-bulk",
                                       json={
                                           "account_ids": [id1, id2],
                                           "field": "original_email_line",
                                       },
                                       headers={"X-Auth-Code": "test-auth"})
                    self.assertEqual(resp.status_code, 200)
                    data = resp.get_json()
                    self.assertTrue(data["ok"])
                    self.assertEqual(data["count"], 2)
                    values = {v["id"]: v["value"] for v in data["values"]}
                    self.assertEqual(values[id1], "x@y.z----p----c----r")
                    self.assertEqual(values[id2], "plain@y.z")


if __name__ == "__main__":
    unittest.main()
