# -*- coding: utf-8 -*-
"""Chrome 版本一致性回归测试。

背景：纯协议曾出现 TLS(IMPERSONATE=chrome146) 与 UA/sec-ch-ua(Chrome/149) 版本错位，
这种“TLS 说 146、HTTP 头说 149”的组合不存在于真实 Chrome，是强自动化信号。
约束：curl_cffi 0.15/0.16 最高仅支持 chrome146，因此 UA/sec-ch-ua 统一对齐到 146。
"""
import re
import unittest

from config import browser as b
from config.browser import build_browser_environment, validate_browser_profile


class ChromeVersionAlignmentTests(unittest.TestCase):
    def test_impersonate_user_agent_and_sec_ch_ua_share_same_major(self):
        """IMPERSONATE / UA / sec-ch-ua 的 Chrome 主版本必须三者一致。"""
        tls_major = re.search(r"chrome(\d+)", b.IMPERSONATE).group(1)
        ua_major = re.search(r"Chrome/(\d+)\.", b.USER_AGENT).group(1)
        # sec-ch-ua 里 Google Chrome 的版本
        gc_major = re.search(r'"Google Chrome";v="(\d+)"', b.SEC_CH_UA).group(1)
        cr_major = re.search(r'"Chromium";v="(\d+)"', b.SEC_CH_UA).group(1)

        self.assertEqual(tls_major, b.CHROME_MAJOR, "IMPERSONATE 主版本应等于 CHROME_MAJOR")
        self.assertEqual(ua_major, b.CHROME_MAJOR, "UA Chrome 版本应等于 CHROME_MAJOR")
        self.assertEqual(gc_major, b.CHROME_MAJOR, "sec-ch-ua Google Chrome 版本应等于 CHROME_MAJOR")
        self.assertEqual(cr_major, b.CHROME_MAJOR, "sec-ch-ua Chromium 版本应等于 CHROME_MAJOR")

    def test_impersonate_does_not_exceed_curl_cffi_ceiling(self):
        """curl_cffi 0.15/0.16 最高 chrome146；IMPERSONATE 不能高于 146，否则又会出现错位。"""
        tls_major = int(re.search(r"chrome(\d+)", b.IMPERSONATE).group(1))
        self.assertLessEqual(tls_major, 146, "curl_cffi 当前最高仅支持 chrome146")

    def test_default_browser_profile_is_internally_consistent(self):
        """默认画像通过 validate_browser_profile，UA/sec-ch-ua/platform 互不矛盾。"""
        profile = build_browser_environment(geo=None)
        issues = validate_browser_profile(profile)
        self.assertEqual(issues, [], f"默认画像存在不一致: {issues}")
        # sec-ch-ua 版本与画像 chrome_major 一致
        self.assertEqual(profile["chrome_major"], b.CHROME_MAJOR)


if __name__ == "__main__":
    unittest.main()
