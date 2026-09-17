# -*- coding: utf-8 -*-
"""Remail 钱包余额/单价测试：客户端解析、缓存、复用队列计数与 WebUI 接口。"""
import unittest
from unittest.mock import Mock, patch

from config import email as email_config
from core import remail_client
from webui.app import create_app


def _wallet_response(balance="2028.00"):
    response = Mock(status_code=200)
    response.json.return_value = {
        "userId": 1,
        "consumerBalance": balance,
        "supplierAvailable": "0.00",
        "supplierFrozen": "0.00",
        "totalRecharged": "560.00",
        "historicalSpend": "391.20",
        "orderCount": 486,
        "supplierAllocationCount": 0,
        "supplierFulfillmentSuccessRate": 0,
        "updatedAt": "2026-09-16T08:41:43+08:00",
    }
    return response


def _project_detail_response(purchase_price="15.000000", code_price="10.000000"):
    response = Mock(status_code=200)
    response.json.return_value = {
        "project": {"id": 2},
        "products": [
            {
                "type": "microsoft",
                "status": "enabled",
                "codePrice": code_price,
                "purchasePrice": purchase_price,
                "totalAvailable": 815,
                "publicAvailable": 300,
                "suffixes": [
                    {"suffix": "outlook.com", "totalAvailable": 120, "publicAvailable": 80},
                ],
            },
        ],
    }
    return response


class RemailWalletClientTests(unittest.TestCase):
    def setUp(self):
        remail_client._WALLET_CACHE.clear()
        remail_client._INVENTORY_CACHE.clear()

    def tearDown(self):
        remail_client._WALLET_CACHE.clear()
        remail_client._INVENTORY_CACHE.clear()

    @patch("core.remail_client.requests.request")
    def test_fetch_wallet_parses_consumer_balance(self, request):
        request.return_value = _wallet_response("2028.00")
        with patch.object(email_config, "REMAIL_API_KEY", "rk-test-key", create=True):
            wallet = remail_client.fetch_wallet()
        self.assertEqual(request.call_args.args[:2], ("GET", "https://remail.aishop6.com/v1/open/wallet"))
        self.assertEqual(wallet["balance"], 2028.0)

    @patch("core.remail_client.requests.request")
    def test_fetch_wallet_is_cached(self, request):
        request.return_value = _wallet_response()
        with patch.object(email_config, "REMAIL_API_KEY", "rk-test-key", create=True):
            remail_client.fetch_wallet()
            remail_client.fetch_wallet()
        self.assertEqual(request.call_count, 1)

    @patch("core.remail_client.requests.request")
    def test_fetch_wallet_force_bypasses_cache(self, request):
        request.return_value = _wallet_response()
        with patch.object(email_config, "REMAIL_API_KEY", "rk-test-key", create=True):
            remail_client.fetch_wallet()
            remail_client.fetch_wallet(force=True)
        self.assertEqual(request.call_count, 2)

    @patch("core.remail_client.requests.request")
    def test_fetch_wallet_rejects_unparseable_balance(self, request):
        request.return_value = _wallet_response("abc")
        with patch.object(email_config, "REMAIL_API_KEY", "rk-test-key", create=True):
            with self.assertRaises(remail_client.RemailError):
                remail_client.fetch_wallet()

    @patch("core.remail_client.requests.request")
    def test_inventory_price_follows_service_mode(self, request):
        request.return_value = _project_detail_response(purchase_price="15.000000", code_price="10.000000")
        with patch.object(email_config, "REMAIL_API_KEY", "rk-test-key", create=True), patch.object(
            email_config, "REMAIL_PROJECT_ID", 2, create=True
        ), patch.object(email_config, "REMAIL_EMAIL_SUFFIX", "outlook.com", create=True), patch.object(
            email_config, "REMAIL_SUPPLY_POLICY", "public_only", create=True
        ), patch.object(email_config, "REMAIL_SERVICE_MODE", "purchase", create=True):
            inv = remail_client.fetch_suffix_inventory()
        self.assertEqual(inv["price"], 15.0)
        remail_client._INVENTORY_CACHE.clear()
        with patch.object(email_config, "REMAIL_API_KEY", "rk-test-key", create=True), patch.object(
            email_config, "REMAIL_PROJECT_ID", 2, create=True
        ), patch.object(email_config, "REMAIL_EMAIL_SUFFIX", "outlook.com", create=True), patch.object(
            email_config, "REMAIL_SUPPLY_POLICY", "public_only", create=True
        ), patch.object(email_config, "REMAIL_SERVICE_MODE", "code", create=True):
            inv_code = remail_client.fetch_suffix_inventory()
        self.assertEqual(inv_code["price"], 10.0)

    @patch("core.remail_client.requests.request")
    def test_inventory_price_none_when_api_missing_price(self, request):
        response = _project_detail_response()
        payload = response.json.return_value
        payload["products"][0].pop("purchasePrice")
        response.json.return_value = payload
        request.return_value = response
        with patch.object(email_config, "REMAIL_API_KEY", "rk-test-key", create=True), patch.object(
            email_config, "REMAIL_PROJECT_ID", 2, create=True
        ), patch.object(email_config, "REMAIL_EMAIL_SUFFIX", "outlook.com", create=True), patch.object(
            email_config, "REMAIL_SUPPLY_POLICY", "public_only", create=True
        ), patch.object(email_config, "REMAIL_SERVICE_MODE", "purchase", create=True):
            inv = remail_client.fetch_suffix_inventory()
        self.assertIsNone(inv["price"])

    def test_reuse_queue_count_disabled_is_zero(self):
        with patch.object(email_config, "REMAIL_REUSE_FAILED_EMAILS", False, create=True), patch(
            "core.remail_client._load_reuse_queue", return_value=[{"email": "a@x.com"}, {"email": "b@x.com"}]
        ):
            self.assertEqual(remail_client.reuse_queue_count(), 0)

    def test_reuse_queue_count_counts_queue(self):
        queue = [{"email": "a@x.com", "order_no": "1"}, {"email": "b@x.com", "order_no": "2"}]
        with patch.object(email_config, "REMAIL_REUSE_FAILED_EMAILS", True, create=True), patch(
            "core.remail_client._load_reuse_queue", return_value=queue
        ):
            self.assertEqual(remail_client.reuse_queue_count(), 2)


class RemailWalletWebuiTests(unittest.TestCase):
    def setUp(self):
        remail_client._WALLET_CACHE.clear()
        remail_client._INVENTORY_CACHE.clear()
        self.app = create_app(auth_code="test-auth")
        self.client = self.app.test_client()

    def tearDown(self):
        remail_client._WALLET_CACHE.clear()
        remail_client._INVENTORY_CACHE.clear()

    def test_wallet_endpoint_returns_balance_price_reuse(self):
        with patch("core.remail_client.fetch_wallet", return_value={"balance": 2028.0, "updated_at": ""}), patch(
            "core.remail_client.fetch_suffix_inventory",
            return_value={"suffix": "icloud.com", "price": 100.0, "available": 3241},
        ), patch("core.remail_client.reuse_queue_count", return_value=3):
            resp = self.client.get("/api/remail/wallet", headers={"X-Auth-Code": "test-auth"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["balance"], 2028.0)
        self.assertEqual(data["price"], 100.0)
        self.assertEqual(data["price_source"], "api")
        self.assertEqual(data["reuse_count"], 3)
        self.assertGreaterEqual(data["refresh_seconds"], 3)

    def test_wallet_endpoint_falls_back_to_configured_price(self):
        with patch("core.remail_client.fetch_wallet", return_value={"balance": 5.0, "updated_at": ""}), patch(
            "core.remail_client.fetch_suffix_inventory",
            return_value={"suffix": "icloud.com", "price": None, "available": 10},
        ), patch("core.remail_client.reuse_queue_count", return_value=0), patch.object(
            email_config, "REMAIL_EMAIL_PRICE", 88.5, create=True
        ):
            resp = self.client.get("/api/remail/wallet", headers={"X-Auth-Code": "test-auth"})
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["price"], 88.5)
        self.assertEqual(data["price_source"], "config")

    def test_wallet_endpoint_reports_error(self):
        with patch(
            "core.remail_client.fetch_wallet",
            side_effect=remail_client.RemailError("Remail API Key 无效或已失效 (/v1/open/wallet)"),
        ):
            resp = self.client.get("/api/remail/wallet", headers={"X-Auth-Code": "test-auth"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertFalse(data["ok"])
        self.assertIn("API Key", data["error"])


if __name__ == "__main__":
    unittest.main()
