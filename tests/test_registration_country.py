# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from core.registration_geo import normalize_registration_country
from webui.app import _compact_account_for_list


class RegistrationCountryTests(unittest.TestCase):
    def test_normalizes_geo_and_cloud_proxy_values(self):
        cases = {
            "jp": "JP",
            "UK": "GB",
            "RESIDENTIAL_JP": "JP",
            "residential-kr": "KR",
            "RESIDENTIAL": "US",
            "Japan": "JP",
            "United States": "US",
            "default": "",
            "": "",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(normalize_registration_country(value), expected)

        self.assertEqual(
            normalize_registration_country({"country": "Japan", "country_code": "jp"}),
            "JP",
        )

    def test_account_country_persists_and_empty_update_preserves_it(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"), \
                 patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy_accounts.json"), \
                 patch.object(db, "_OUTLOOK_JSON", root / "outlook.json"), \
                 patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"), \
                 patch.object(db, "_TOKENS_TXT", root / "tokens.txt"), \
                 patch.object(db, "_OUTLOOK_TXT", root / "outlook.txt"), \
                 patch.object(db, "_VIEWER_HTML", root / "viewer.html"):
                account_id = db.insert_account(
                    email="region@test.com",
                    access_token="token-1",
                    registration_country="JP",
                )
                self.assertEqual(db.get_account(account_id)["registration_country"], "JP")

                db.insert_account(
                    email="region@test.com",
                    access_token="token-2",
                    registration_country="",
                )
                self.assertEqual(db.get_account(account_id)["registration_country"], "JP")

    def test_compact_account_includes_country_without_token(self):
        compact = _compact_account_for_list({
            "id": 1,
            "email": "region@test.com",
            "access_token": "sensitive-token",
            "registration_country": "PH",
        })
        self.assertEqual(compact["registration_country"], "PH")
        self.assertTrue(compact["has_access_token"])
        self.assertNotIn("access_token", compact)


if __name__ == "__main__":
    unittest.main()
