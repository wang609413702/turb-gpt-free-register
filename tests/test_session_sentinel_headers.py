# -*- coding: utf-8 -*-
"""BrowserSession 请求头与真实浏览器 HAR 对齐的回归测试。

数据来源：har_captures/...-20260814-...-har.json（RoxyBrowser 真实注册流程采集）。
"""
import unittest

from core.session import BrowserSession


class SentinelHeaderAlignmentTests(unittest.TestCase):
    def setUp(self):
        # detect_exit_geo=False：避免测试触发网络（出口 IP 探测）请求
        self.session = BrowserSession(proxy="", detect_exit_geo=False)

    def _lower_keys(self, headers):
        return {k.lower() for k in headers}

    def test_sentinel_request_omits_oai_and_datadog_headers(self):
        """真实浏览器 sentinel/req 只发基础头 + 低熵 sec-ch-ua，不带 oai-*/datadog/trace。

        多发这些头是强自动化信号（2026-08-14 HAR 采集确认 sentinel/req 这些头一个都没有）。
        """
        headers = self.session.get_sentinel_headers()
        lower = self._lower_keys(headers)
        for forbidden in (
            "oai-device-id", "oai-client-version", "oai-client-build-number",
            "oai-session-id", "oai-language",
            "x-datadog-origin", "x-datadog-trace-id", "x-datadog-parent-id",
            "x-datadog-sampling-priority",
            "traceparent", "tracestate", "x-access-flow-invocation-id",
        ):
            self.assertNotIn(forbidden, lower, f"sentinel/req 不应携带 {forbidden}")

        # 基础头与真实浏览器一致
        self.assertEqual(headers["content-type"], "text/plain;charset=UTF-8")
        self.assertEqual(headers["origin"], "https://sentinel.openai.com")
        self.assertEqual(headers["accept"], "*/*")
        self.assertEqual(headers["sec-fetch-site"], "same-origin")
        self.assertEqual(headers["sec-fetch-mode"], "cors")
        self.assertEqual(headers["sec-fetch-dest"], "empty")
        # 低熵 client hints 仍在（真实浏览器也带）
        self.assertIn("sec-ch-ua", lower)
        self.assertIn("sec-ch-ua-platform", lower)
        self.assertIn("sec-ch-ua-mobile", lower)

    def test_chatgpt_headers_still_carry_oai_context(self):
        """chatgpt.com backend-api 仍应带 oai-*/datadog（与 sentinel/req 区分，避免误删）。"""
        headers = self.session.get_chatgpt_headers()
        lower = self._lower_keys(headers)
        self.assertIn("oai-device-id", lower)
        self.assertIn("oai-client-version", lower)
        self.assertIn("oai-session-id", lower)
        self.assertIn("x-datadog-origin", lower)


class EmailOtpSentinelDefaultTests(unittest.TestCase):
    def test_send_sentinel_on_email_otp_validate_defaults_true(self):
        """2026-08-14 HAR 确认 email-otp/validate 携带 sentinel，默认应开启。"""
        from config import openai_protocol as p
        self.assertTrue(
            getattr(p, "SEND_SENTINEL_ON_EMAIL_OTP_VALIDATE", False),
            "SEND_SENTINEL_ON_EMAIL_OTP_VALIDATE 默认应为 True（对齐真实浏览器）",
        )


if __name__ == "__main__":
    unittest.main()
