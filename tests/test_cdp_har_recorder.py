# -*- coding: utf-8 -*-
"""CdpHarRecorder 单元测试：事件→HAR 聚合、脱敏、指纹采集、降级行为。"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.cdp_har_recorder import (
    CdpHarRecorder,
    capture_js_fingerprint,
    start_har_recorder,
)


def _req_will_be_sent(
    req_id,
    url,
    method="POST",
    post_text=None,
    type_="XHR",
    ts=1.0,
    redirect=None,
    headers=None,
):
    params = {
        "requestId": req_id,
        "type": type_,
        "timestamp": ts,
        "request": {
            "url": url,
            "method": method,
            "headers": headers or {"User-Agent": "Mozilla/5.0", "Cookie": "abc=1"},
            "postData": post_text,
        },
    }
    if redirect:
        params["redirectResponse"] = redirect
    return params


def _response_received(req_id, ts=2.0, status=200, mime="application/json", headers=None):
    return {
        "requestId": req_id,
        "timestamp": ts,
        "response": {
            "status": status,
            "statusText": "OK",
            "mimeType": mime,
            "headers": headers or {"Content-Type": "application/json"},
        },
    }


def _loading_finished(req_id, ts=3.0):
    return {"requestId": req_id, "timestamp": ts}


class HarBuildTests(unittest.TestCase):
    def test_single_request_builds_har_compatible_with_analyze_tool(self):
        rec = CdpHarRecorder(redact=True)
        rec._on_request_will_be_sent(_req_will_be_sent(
            "1",
            "https://chatgpt.com/api/auth/providers",
            post_text='{"code":"123456","p":"gAAAAACxx","name":"a"}',
        ))
        rec._on_response_received(_response_received("1"))
        rec._on_loading_finished(_loading_finished("1"))

        har = rec.build_har()
        entries = har["log"]["entries"]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        req = entry["request"]
        # analyze_har_protocol.py 依赖的字段
        self.assertEqual(req["url"], "https://chatgpt.com/api/auth/providers")
        self.assertEqual(req["method"], "POST")
        self.assertIsInstance(req["headers"], list)
        self.assertIn("postData", req)
        self.assertEqual(entry["response"]["status"], 200)
        self.assertEqual(entry["response"]["content"]["mimeType"], "application/json")

        # Cookie 头值脱敏，但保留名字
        cookie = next(h for h in req["headers"] if h["name"].lower() == "cookie")
        self.assertEqual(cookie["value"], "<redacted:len=5>")

        # post 里 code 脱敏，指纹 p 保留
        post = json.loads(req["postData"]["text"])
        self.assertEqual(post["code"], "<redacted:len=6>")
        self.assertEqual(post["p"], "gAAAAACxx")

        # 时序：send=(2-1)*1000, receive=(3-2)*1000, total=(3-1)*1000
        self.assertEqual(entry["time"], 2000.0)
        self.assertEqual(entry["timings"]["send"], 1000.0)
        self.assertEqual(entry["timings"]["receive"], 1000.0)

    def test_redirect_hops_become_separate_entries(self):
        rec = CdpHarRecorder(redact=True)
        rec._on_request_will_be_sent(_req_will_be_sent(
            "r1", "https://auth.openai.com/authorize", method="GET", type_="Document", ts=1.0,
        ))
        rec._on_request_will_be_sent(_req_will_be_sent(
            "r1",
            "https://auth.openai.com/email-verification",
            method="GET",
            type_="Document",
            ts=1.5,
            redirect={"status": 302, "statusText": "Found",
                      "headers": {"Location": "https://auth.openai.com/email-verification"}},
        ))
        rec._on_response_received(_response_received("r1", ts=2.0, status=200, mime="text/html"))
        rec._on_loading_finished(_loading_finished("r1", ts=3.0))

        har = rec.build_har()
        entries = har["log"]["entries"]
        self.assertEqual(len(entries), 2)
        # 第一跳拿到重定向响应
        self.assertEqual(entries[0]["response"]["status"], 302)
        self.assertEqual(entries[0]["response"]["redirectURL"], "https://auth.openai.com/email-verification")
        self.assertEqual(entries[1]["request"]["url"], "https://auth.openai.com/email-verification")
        self.assertEqual(entries[1]["response"]["status"], 200)

    def test_extra_info_merges_headers_and_redacts(self):
        rec = CdpHarRecorder(redact=True)
        rec._on_request_will_be_sent(_req_will_be_sent("x1", "https://chatgpt.com/api/auth/session", method="GET"))
        rec._on_request_extra_info({
            "requestId": "x1",
            "headers": {"x-openai-target-path": "/api/auth/session", "Cookie": "session=abcdef"},
        })
        har = rec.build_har()
        headers = har["log"]["entries"][0]["request"]["headers"]
        names = {h["name"].lower() for h in headers}
        # 新头并入
        self.assertIn("x-openai-target-path", names)
        # Cookie 已存在则去重，不重复合并；且不会被二次打码（保持原始 len=5）
        self.assertEqual(sum(1 for h in headers if h["name"].lower() == "cookie"), 1)
        cookie = next(h for h in headers if h["name"].lower() == "cookie")
        self.assertEqual(cookie["value"], "<redacted:len=5>")


class RedactionTests(unittest.TestCase):
    def test_redact_off_keeps_raw_values(self):
        rec = CdpHarRecorder(redact=False)
        rec._on_request_will_be_sent(_req_will_be_sent(
            "1", "https://chatgpt.com/api/auth/providers", post_text='{"code":"123456"}',
        ))
        har = rec.build_har()
        req = har["log"]["entries"][0]["request"]
        cookie = next(h for h in req["headers"] if h["name"].lower() == "cookie")
        self.assertEqual(cookie["value"], "abc=1")
        self.assertEqual(json.loads(req["postData"]["text"])["code"], "123456")


class FingerprintTests(unittest.TestCase):
    def test_capture_returns_dict_from_driver(self):
        class FakeDriver:
            def execute_script(self, script):
                return {"userAgent": "ua", "languages": ["en-US"]}

        fp = capture_js_fingerprint(FakeDriver())
        self.assertEqual(fp["userAgent"], "ua")
        self.assertEqual(fp["languages"], ["en-US"])

    def test_capture_returns_empty_on_error(self):
        class BadDriver:
            def execute_script(self, script):
                raise RuntimeError("boom")

        self.assertEqual(capture_js_fingerprint(BadDriver()), {})

    def test_script_covers_p_array_window_flags(self):
        from core.cdp_har_recorder import _FINGERPRINT_SCRIPT
        for key in ("requestIdleCallback", "hardwareConcurrency", "canvas", "webgl", "timezone"):
            self.assertIn(key, _FINGERPRINT_SCRIPT)


class StartBehaviorTests(unittest.TestCase):
    def test_disabled_returns_none(self):
        with patch("config.roxybrowser.ROXY_CAPTURE_HAR", False):
            opened = SimpleNamespace(debugger_address="127.0.0.1:9999", ws_endpoint=None)
            self.assertIsNone(start_har_recorder(opened, "a@b.com"))

    def test_no_debugger_address_returns_none(self):
        with patch("config.roxybrowser.ROXY_CAPTURE_HAR", True):
            opened = SimpleNamespace(debugger_address="", ws_endpoint=None)
            self.assertIsNone(start_har_recorder(opened, "a@b.com"))

    def test_connection_failure_returns_none_without_raising(self):
        with patch("config.roxybrowser.ROXY_CAPTURE_HAR", True), \
             patch("core.cdp_har_recorder.urllib.request.urlopen", side_effect=OSError("refused")):
            opened = SimpleNamespace(debugger_address="127.0.0.1:1", ws_endpoint=None)
            self.assertIsNone(start_har_recorder(opened, "a@b.com"))

    def test_start_passes_suppress_origin_to_cdp_handshake(self):
        """Chrome 111+ CDP 会拒绝带 Origin 的 websocket 握手（403 Forbidden），
        必须以 suppress_origin=True 连接。这里用假 websocket 模块捕获调用参数。"""
        import sys
        import types

        captured = {}

        class FakeWS:
            """真实 CDP 语义：先收到命令，再回对应响应。用队列建模避免竞态。"""

            def __init__(self):
                import queue
                self._q = queue.Queue()

            def settimeout(self, t):
                pass

            def send(self, payload):
                msg = json.loads(payload)
                self._q.put(json.dumps({"id": msg.get("id"), "result": {}}))

            def recv(self):
                import queue
                try:
                    return self._q.get(timeout=2)
                except queue.Empty:
                    raise OSError("timeout")

            def close(self):
                self._q.put(None)  # 解除可能阻塞的 recv

        def fake_create_connection(url, **opts):
            captured["url"] = url
            captured["opts"] = opts
            return FakeWS()

        fake_mod = types.ModuleType("websocket")
        fake_mod.create_connection = fake_create_connection

        target_json = json.dumps([{
            "type": "page",
            "url": "https://chatgpt.com/",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9/devtools/page/abc",
        }]).encode("utf-8")

        with patch.dict(sys.modules, {"websocket": fake_mod}), \
             patch("core.cdp_har_recorder.urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = target_json
            rec = CdpHarRecorder(debugger_address="127.0.0.1:9", redact=True)
            self.assertTrue(rec.start())
        try:
            self.assertTrue(captured["opts"].get("suppress_origin"),
                            "create_connection 必须传 suppress_origin=True 绕过 Chrome CDP Origin 拦截")
            self.assertIn("/devtools/page/", captured["url"])
        finally:
            rec.stop()


class StopSaveTests(unittest.TestCase):
    def test_stop_saves_har_and_fingerprint_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = CdpHarRecorder(redact=True, email="a@b.com", output_dir=tmp)
            rec._on_request_will_be_sent(_req_will_be_sent(
                "1", "https://auth.openai.com/api/accounts/email-otp/validate",
                post_text='{"code":"654321"}',
            ))
            rec._on_response_received(_response_received("1"))
            rec._fingerprint = {"userAgent": "test-ua", "screen": {"width": 1440}}

            har_path, fp_path = rec.stop()
            self.assertTrue(har_path)
            self.assertTrue(fp_path)
            har = json.loads(Path(har_path).read_text(encoding="utf-8"))
            self.assertEqual(len(har["log"]["entries"]), 1)
            fp = json.loads(Path(fp_path).read_text(encoding="utf-8"))
            self.assertEqual(fp["userAgent"], "test-ua")
            # OTP 请求体已脱敏
            post = json.loads(har["log"]["entries"][0]["request"]["postData"]["text"])
            self.assertEqual(post["code"], "<redacted:len=6>")


if __name__ == "__main__":
    unittest.main()
