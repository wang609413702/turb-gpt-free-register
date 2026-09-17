# -*- coding: utf-8 -*-
"""GenericAPI 取码 settle 锁定测试：多封验证码轮询返回时按时间戳锁定最新。"""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core import generic_api_mail_client as g


class _FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    @property
    def text(self):
        return json.dumps(self._payload) if isinstance(self._payload, dict) else str(self._payload)


class _FakeSession:
    def __init__(self, payloads):
        self._payloads = payloads
        self.calls = 0

    def get(self, url, **kwargs):
        payload = self._payloads[self.calls % len(self._payloads)]
        self.calls += 1
        return _FakeResponse(payload)


class FetchLatestOtpSettleTests(unittest.TestCase):
    def _run(self, payloads, **kwargs):
        account = SimpleNamespace(email="user@example.com", code_url="https://mail.example.com/api/code")
        session = _FakeSession(payloads)
        with patch.object(g, "get_account_context", return_value=account), \
                patch.object(g, "_parse_yangyang_code_url", return_value=None), \
                patch.object(g.requests, "Session", return_value=session), \
                patch.object(g.time, "sleep"):
            return g.fetch_latest_otp(
                "user@example.com",
                after_ts=None,
                max_wait=5,
                poll_interval=None,
                settle_seconds=0.15,
                **kwargs,
            )

    def test_alternating_codes_lock_newest_by_timestamp(self):
        """接口在旧/新两封验证码间轮流返回时，最终必须锁定时间戳更新的那封。"""
        payloads = [
            {"code": "836398", "time": "2026-09-15T01:27:44Z", "subject": "Your temporary ChatGPT verification code"},
            {"code": "283370", "time": "2026-09-15T01:29:10Z", "subject": "Your temporary ChatGPT verification code"},
            {"code": "836398", "time": "2026-09-15T01:27:44Z", "subject": "Your temporary ChatGPT verification code"},
            {"code": "283370", "time": "2026-09-15T01:29:10Z", "subject": "Your temporary ChatGPT verification code"},
        ]
        otp = self._run(payloads)
        self.assertEqual(otp, "283370")

    def test_plain_code_without_ts_cannot_replace_locked_timestamped_candidate(self):
        """已锁定有时间戳的候选时，无时间戳的纯文本候选不应顶掉它。"""
        payloads = [
            {"code": "836398", "time": "2026-09-15T01:29:10Z", "subject": "Your temporary ChatGPT verification code"},
            "Your verification code is 111111. It expires in 10 minutes.",
            "Your verification code is 111111. It expires in 10 minutes.",
        ]
        otp = self._run(payloads)
        self.assertEqual(otp, "836398")


class StrictParamApiCacheBustTests(unittest.TestCase):
    """严格校验参数的取码接口（如 gxyf-ch.com）：400 后自动跳过缓存穿透参数。"""

    def test_400_invalid_request_falls_back_to_plain_url(self):
        from core import generic_api_mail_client as g

        account = SimpleNamespace(email="strict@gxyf-ch.com", code_url="https://inbox.gxyf-ch.com/api/v1/mailboxes/strict@gxyf-ch.com/code?key=abc")
        seen_urls: list[str] = []

        class _Resp:
            def __init__(self, status, payload):
                self.status_code = status
                self._payload = payload
                self.text = json.dumps(payload)

        def get(url, **kwargs):
            seen_urls.append(url)
            if "_otp_poll=" in url:
                return _Resp(400, {"success": False, "code": "invalid_request", "message": "请求参数无效"})
            return _Resp(200, {"code": "246810", "time": "2026-09-15T03:00:00Z", "subject": "Your temporary ChatGPT verification code"})

        session = SimpleNamespace(get=get)
        with patch.object(g, "get_account_context", return_value=account), \
                patch.object(g, "_parse_yangyang_code_url", return_value=None), \
                patch.object(g.requests, "Session", return_value=session), \
                patch.object(g.time, "sleep"):
            otp = g.fetch_latest_otp(account.email, after_ts=None, max_wait=5, settle_seconds=0.15)

        self.assertEqual(otp, "246810")
        # 首次带缓存穿透参数被 400 拒绝后，立即用原始 URL 重试并成功。
        self.assertTrue(any("_otp_poll=" in u for u in seen_urls))
        self.assertTrue(any("_otp_poll=" not in u for u in seen_urls[seen_urls.index([u for u in seen_urls if "_otp_poll=" in u][0]) + 1:]))
        # 该账号进入跳过名单：后续取码不再加参数。
        self.assertIn(account.email, g._NO_BUST_ACCOUNTS)
        g._NO_BUST_ACCOUNTS.discard(account.email)


if __name__ == "__main__":
    unittest.main()
