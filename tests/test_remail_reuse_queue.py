# -*- coding: utf-8 -*-
"""Remail 失败邮箱复用队列测试。

复用队列通过项目根 remail_reuse_queue.json 工作；这里把队列路径指向临时
文件，避免测试消耗真实队列条目。
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from config import email as email_config
from core import remail_client


def _order_detail_response(email: str, token: str = "st-restored", order_no: str = "R-REUSE-1"):
    response = Mock(status_code=200)
    response.json.return_value = {
        "orderNo": order_no,
        "status": "active",
        "deliveryEmail": email,
        "serviceToken": token,
    }
    return response


class RemailReuseQueueTests(unittest.TestCase):
    def setUp(self):
        remail_client._CONTEXT_CACHE.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.queue_path = Path(self._tmp.name) / "remail_reuse_queue.json"
        self._path_patcher = patch.object(remail_client, "_REUSE_QUEUE_PATH", self.queue_path)
        self._path_patcher.start()
        self.addCleanup(self._path_patcher.stop)

    def _write_queue(self, entries):
        self.queue_path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")

    def _read_queue(self):
        return json.loads(self.queue_path.read_text(encoding="utf-8"))

    @patch("core.remail_client.requests.request")
    def test_pick_account_prefers_reuse_queue_entry(self, request):
        request.return_value = _order_detail_response("reused@icloud.test")
        self._write_queue([
            {"email": "reused@icloud.test", "order_no": "R-REUSE-1"},
            {"email": "second@icloud.test", "order_no": "R-REUSE-2"},
        ])

        with patch.object(email_config, "REMAIL_API_KEY", "rk-test-key", create=True), patch.object(
            email_config, "REMAIL_PROJECT_ID", 1001, create=True
        ), patch.object(email_config, "REMAIL_EMAIL_SUFFIX", "outlook.com", create=True):
            account = remail_client.pick_account()

        # 只发生一次请求：按订单号恢复上下文，不再创建新订单。
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[:2], ("GET", "https://remail.aishop6.com/v1/open/orders/R-REUSE-1"))
        self.assertEqual(account.email, "reused@icloud.test")
        self.assertEqual(account.service_token, "st-restored")
        self.assertIsNotNone(remail_client.get_account_context("reused@icloud.test"))
        # 队列只弹出已消费的一条，第二条保留。
        self.assertEqual([e["email"] for e in self._read_queue()], ["second@icloud.test"])

    @patch("core.remail_client.requests.request")
    def test_pick_account_falls_back_to_new_order_when_queue_empty(self, request):
        response = Mock(status_code=201)
        response.json.return_value = {
            "orderNo": "R-NEW-1",
            "status": "active",
            "deliveryEmail": "fresh@outlook.test",
            "serviceToken": "st-fresh",
        }
        request.return_value = response

        with patch.object(email_config, "REMAIL_API_KEY", "rk-test-key", create=True), patch.object(
            email_config, "REMAIL_PROJECT_ID", 1001, create=True
        ), patch.object(email_config, "REMAIL_EMAIL_SUFFIX", "outlook.com", create=True):
            account = remail_client.pick_account()

        self.assertEqual(account.email, "fresh@outlook.test")
        self.assertEqual(request.call_args.args[:2], ("POST", "https://remail.aishop6.com/v1/open/orders"))

    @patch("core.remail_client.requests.request")
    def test_failed_restore_is_requeued_then_discarded(self, request):
        # 恢复请求一直失败（网络/订单失效）：每次注册给条目一次恢复机会，
        # 失败记 attempts 放回队列留给下次；累计两次失败后第三次淘汰。
        create_response = Mock(status_code=201)
        create_response.json.return_value = {
            "orderNo": "R-NEW-1",
            "status": "active",
            "deliveryEmail": "fresh@outlook.test",
            "serviceToken": "st-fresh",
        }

        def _request_mock(method, url, **kwargs):
            if str(method).upper() == "GET":
                raise remail_client.RemailError("network down")
            return create_response

        request.side_effect = _request_mock
        self._write_queue([{"email": "dead@icloud.test", "order_no": "R-DEAD-1"}])

        with patch.object(email_config, "REMAIL_API_KEY", "rk-test-key", create=True), patch.object(
            email_config, "REMAIL_PROJECT_ID", 1001, create=True
        ), patch.object(email_config, "REMAIL_EMAIL_SUFFIX", "outlook.com", create=True):
            remail_client.pick_account()
            queue_after_first = self._read_queue()
            remail_client.pick_account()
            queue_after_second = self._read_queue()
            remail_client.pick_account()
            queue_after_third = self._read_queue()

        self.assertEqual(queue_after_first[0]["attempts"], 1)
        self.assertEqual(queue_after_second[0]["attempts"], 2)
        self.assertEqual(queue_after_third, [])

    @patch("core.remail_client.requests.request")
    def test_disabled_flag_leaves_queue_untouched(self, request):
        request.return_value = _order_detail_response("reused@icloud.test")
        self._write_queue([{"email": "reused@icloud.test", "order_no": "R-REUSE-1"}])

        with patch.object(email_config, "REMAIL_REUSE_FAILED_EMAILS", False, create=True), patch.object(
            email_config, "REMAIL_API_KEY", "rk-test-key", create=True
        ), patch.object(email_config, "REMAIL_PROJECT_ID", 1001, create=True), patch.object(
            email_config, "REMAIL_EMAIL_SUFFIX", "outlook.com", create=True
        ):
            account = remail_client.pick_account()

        self.assertEqual(account.email, "reused@icloud.test")
        # 没有按订单号恢复的 GET 请求；唯一一次请求是正常下单 POST。
        self.assertEqual(request.call_args.args[:2], ("POST", "https://remail.aishop6.com/v1/open/orders"))
        self.assertEqual(len(self._read_queue()), 1)
    def test_release_available_account_requeues_order_at_front(self):
        remail_client._CONTEXT_CACHE["retry@icloud.test"] = remail_client.RemailAccount(
            email="retry@icloud.test",
            service_token="st-retry",
            order_no="R-RETRY-1",
            project_id=1001,
            email_suffix="icloud.com",
        )
        self._write_queue([{"email": "later@icloud.test", "order_no": "R-LATER-1"}])

        remail_client.release_account("retry@icloud.test", status="available", note="network retry")

        self.assertIsNone(remail_client.get_account_context("retry@icloud.test"))
        self.assertEqual(
            [entry["order_no"] for entry in self._read_queue()],
            ["R-RETRY-1", "R-LATER-1"],
        )
        self.assertEqual(self._read_queue()[0]["attempts"], 0)

    def test_release_failed_account_does_not_requeue_order(self):
        remail_client._CONTEXT_CACHE["failed@icloud.test"] = remail_client.RemailAccount(
            email="failed@icloud.test",
            service_token="st-failed",
            order_no="R-FAILED-1",
            project_id=1001,
            email_suffix="icloud.com",
        )

        remail_client.release_account("failed@icloud.test", status="failed")



if __name__ == "__main__":
    unittest.main()
