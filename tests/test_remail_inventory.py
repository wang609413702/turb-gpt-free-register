# -*- coding: utf-8 -*-
"""Remail 后缀库存查询测试：客户端解析、缓存与 WebUI 展示接口。"""
import unittest
from unittest.mock import Mock, patch

from config import email as email_config
from core import remail_client
from webui.app import create_app


def _project_detail_response():
    response = Mock(status_code=200)
    response.json.return_value = {
        "project": {"id": 2, "name": "Microsoft 账号验证码"},
        "products": [
            {
                "type": "microsoft",
                "status": "enabled",
                "totalAvailable": 815,
                "publicAvailable": 300,
                "suffixes": [
                    {"suffix": "outlook.com", "totalAvailable": 120, "publicAvailable": 80},
                    {"suffix": "hotmail.com", "totalAvailable": 15, "publicAvailable": 5},
                ],
            },
            {
                "type": "icloud",
                "status": "enabled",
                "totalAvailable": 50,
                "publicAvailable": 20,
                "suffixes": [
                    {"suffix": "icloud.com", "totalAvailable": 50, "publicAvailable": 20},
                ],
            },
        ],
    }
    return response


class RemailSuffixInventoryTests(unittest.TestCase):
    def setUp(self):
        remail_client._CONTEXT_CACHE.clear()
        remail_client._INVENTORY_CACHE.clear()

    def tearDown(self):
        remail_client._INVENTORY_CACHE.clear()

    @patch("core.remail_client.requests.request")
    def test_fetch_suffix_inventory_reads_suffix_entry(self, request):
        request.return_value = _project_detail_response()
        with patch.object(email_config, "REMAIL_API_KEY", "rk-test-key", create=True), patch.object(
            email_config, "REMAIL_PROJECT_ID", 2, create=True
        ), patch.object(email_config, "REMAIL_EMAIL_SUFFIX", "outlook.com", create=True), patch.object(
            email_config, "REMAIL_SUPPLY_POLICY", "public_only", create=True
        ):
            inv = remail_client.fetch_suffix_inventory()
        self.assertEqual(request.call_args.args[:2], ("GET", "https://remail.aishop6.com/v1/open/projects/2"))
        self.assertEqual(inv["suffix"], "outlook.com")
        self.assertEqual(inv["product_type"], "microsoft")
        self.assertEqual(inv["total_available"], 120)
        self.assertEqual(inv["public_available"], 80)
        # public_only 策略下可用数量取公开库存。
        self.assertEqual(inv["available"], 80)

    @patch("core.remail_client.requests.request")
    def test_fetch_suffix_inventory_private_first_uses_total(self, request):
        request.return_value = _project_detail_response()
        with patch.object(email_config, "REMAIL_API_KEY", "rk-test-key", create=True), patch.object(
            email_config, "REMAIL_PROJECT_ID", 2, create=True
        ), patch.object(email_config, "REMAIL_EMAIL_SUFFIX", "outlook.com", create=True), patch.object(
            email_config, "REMAIL_SUPPLY_POLICY", "private_first", create=True
        ):
            inv = remail_client.fetch_suffix_inventory()
        self.assertEqual(inv["available"], 120)

    @patch("core.remail_client.requests.request")
    def test_fetch_suffix_inventory_is_cached(self, request):
        request.return_value = _project_detail_response()
        with patch.object(email_config, "REMAIL_API_KEY", "rk-test-key", create=True), patch.object(
            email_config, "REMAIL_PROJECT_ID", 2, create=True
        ), patch.object(email_config, "REMAIL_EMAIL_SUFFIX", "outlook.com", create=True), patch.object(
            email_config, "REMAIL_SUPPLY_POLICY", "public_only", create=True
        ):
            remail_client.fetch_suffix_inventory()
            remail_client.fetch_suffix_inventory()
        self.assertEqual(request.call_count, 1)


class RemailInventoryWebuiTests(unittest.TestCase):
    def setUp(self):
        remail_client._INVENTORY_CACHE.clear()
        self.app = create_app(auth_code="test-auth")
        self.client = self.app.test_client()

    def tearDown(self):
        remail_client._INVENTORY_CACHE.clear()

    def test_inventory_endpoint_returns_suffix_stock(self):
        with patch(
            "core.remail_client.fetch_suffix_inventory",
            return_value={
                "suffix": "outlook.com",
                "product_type": "microsoft",
                "total_available": 120,
                "public_available": 80,
                "available": 80,
            },
        ):
            resp = self.client.get("/api/remail/inventory", headers={"X-Auth-Code": "test-auth"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["suffix"], "outlook.com")
        self.assertEqual(data["available"], 80)
        self.assertGreaterEqual(data["refresh_seconds"], 3)

    def test_summary_uses_remail_inventory_without_local_pool(self):
        with patch.object(email_config, "EMAIL_SOURCE", "remail", create=True), patch(
            "core.remail_client.fetch_suffix_inventory",
            return_value={
                "suffix": "outlook.com",
                "product_type": "microsoft",
                "total_available": 120,
                "public_available": 80,
                "available": 80,
            },
        ):
            resp = self.client.get("/api/summary", headers={"X-Auth-Code": "test-auth"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["email_sources"], ["remail"])
        # remail 来源不重复计入本地 outlook 池，可用数量来自服务端库存。
        self.assertEqual(data["outlook_available"], 80)
        self.assertEqual(data["outlook_total"], 120)
        self.assertEqual(data["remail_inventory"]["ok"], True)
        self.assertEqual(data["remail_inventory"]["suffix"], "outlook.com")


if __name__ == "__main__":
    unittest.main()
