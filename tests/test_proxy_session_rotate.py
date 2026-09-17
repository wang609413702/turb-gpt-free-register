# -*- coding: utf-8 -*-
"""代理会话轮换测试：每次领取随机化 session-编号，换新出口 IP。"""
import re
import unittest
from unittest.mock import patch

from config import proxy as proxy_cfg


class ProxySessionRotateTests(unittest.TestCase):
    URL = "socks5h://USER343203-zone-custom-region-VN-session-75097477-sessTime-5-sessAuto-1:pw@gw.example:10000"

    def test_rotate_changes_session_id(self):
        out = proxy_cfg._rotate_proxy_session(self.URL)
        self.assertNotEqual(out, self.URL)
        m = re.search(r"session-(\d+)", out)
        self.assertIsNotNone(m)
        self.assertNotEqual(m.group(1), "75097477")
        # 其余部分（凭据/网关/区域参数）保持不变
        self.assertIn("region-VN", out)
        self.assertIn("sessTime-5", out)
        self.assertIn("gw.example:10000", out)

    def test_rotate_produces_fresh_sessions_each_call(self):
        a = proxy_cfg._rotate_proxy_session(self.URL)
        b = proxy_cfg._rotate_proxy_session(self.URL)
        self.assertNotEqual(
            re.search(r"session-(\d+)", a).group(1),
            re.search(r"session-(\d+)", b).group(1),
        )

    def test_url_without_session_untouched(self):
        url = "socks5h://user:pass@1.2.3.4:1080"
        self.assertEqual(proxy_cfg._rotate_proxy_session(url), url)

    def test_pick_proxy_rotates_when_enabled(self):
        with patch.object(proxy_cfg, "PROXY_POOL", [self.URL]), \
                patch.object(proxy_cfg, "PROXY_SESSION_ROTATE", True):
            out = proxy_cfg.pick_proxy()
        self.assertNotEqual(out, self.URL)

    def test_pick_proxy_keeps_url_when_disabled(self):
        with patch.object(proxy_cfg, "PROXY_POOL", [self.URL]), \
                patch.object(proxy_cfg, "PROXY_SESSION_ROTATE", False):
            self.assertEqual(proxy_cfg.pick_proxy(), self.URL)


if __name__ == "__main__":
    unittest.main()
