# -*- coding: utf-8 -*-
"""asset_warmup 资源预热层单元测试。"""
import unittest

from core.asset_warmup import extract_asset_urls, warmup_page_assets, _asset_headers, _ext_of


HTML = """
<html><head>
  <link rel="stylesheet" href="/cdn/assets/main.css">
  <link rel="preload" as="font" href="https://auth-cdn.oaistatic.com/font.woff2">
  <script src="/cdn/assets/app.js"></script>
  <script src="data:application/javascript,bad"></script>
  <link href="https://evil.com/x.css">
  <img src="/cdn/assets/logo.png">
  <img src="blob:https://chatgpt.com/abc">
  <a href="/cdn/assets/page.html">not an asset ext</a>
</head></html>
"""


class ExtractAssetUrlsTests(unittest.TestCase):
    def test_finds_css_js_font_image_and_resolves_relative(self):
        urls = extract_asset_urls(HTML, "https://chatgpt.com/login")
        self.assertIn("https://chatgpt.com/cdn/assets/main.css", urls)
        self.assertIn("https://chatgpt.com/cdn/assets/app.js", urls)
        self.assertIn("https://auth-cdn.oaistatic.com/font.woff2", urls)
        self.assertIn("https://chatgpt.com/cdn/assets/logo.png", urls)

    def test_skips_disallowed_hosts(self):
        urls = extract_asset_urls(HTML, "https://chatgpt.com/login")
        self.assertFalse(any("evil.com" in u for u in urls))

    def test_skips_data_and_blob_schemes(self):
        urls = extract_asset_urls(HTML, "https://chatgpt.com/login")
        self.assertFalse(any(u.startswith(("data:", "blob:")) for u in urls))

    def test_skips_non_asset_extensions(self):
        urls = extract_asset_urls(HTML, "https://chatgpt.com/login")
        # .html 不是已知资源类型，应被过滤
        self.assertFalse(any(u.endswith(".html") for u in urls))

    def test_dedupes_repeated_urls(self):
        html = '<link href="/cdn/assets/a.css"><link href="/cdn/assets/a.css">'
        self.assertEqual(extract_asset_urls(html, "https://chatgpt.com/"),
                         ["https://chatgpt.com/cdn/assets/a.css"])

    def test_empty_html(self):
        self.assertEqual(extract_asset_urls("", "https://chatgpt.com/"), [])


class _FakeResp:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text


class _FakeInner:
    def __init__(self, page_status=200, page_html=HTML, asset_status=200):
        self._page_status = page_status
        self._page_html = page_html
        self._asset_status = asset_status
        self.requested = []

    def get(self, url, headers=None, timeout=None, **kw):
        self.requested.append(url)
        if url == "https://chatgpt.com/login":
            return _FakeResp(self._page_status, self._page_html)
        return _FakeResp(self._asset_status)


class _FakeSession:
    def __init__(self, **kw):
        self.session = _FakeInner(**kw)

    def _get_common_headers(self):
        return {"User-Agent": "test-ua", "accept-language": "en-US,en;q=0.9", "sec-ch-ua": '"x"'}


class WarmupTests(unittest.TestCase):
    def test_fetches_assets_and_returns_count(self):
        sess = _FakeSession()
        n = warmup_page_assets(sess, "https://chatgpt.com/login", max_assets=10)
        # main.css / font.woff2 / app.js / logo.png 四个资源，全部 200
        self.assertEqual(n, 4)
        # HTML 抓取 + 4 个资源 = 5 次请求
        self.assertEqual(len(sess.session.requested), 5)

    def test_html_failure_returns_zero(self):
        sess = _FakeSession(page_status=404)
        self.assertEqual(warmup_page_assets(sess, "https://chatgpt.com/login"), 0)

    def test_respects_max_assets(self):
        html = "".join(f'<link href="/cdn/assets/{i}.css">' for i in range(10))
        sess = _FakeSession(page_html=html)
        n = warmup_page_assets(sess, "https://chatgpt.com/login", max_assets=3)
        self.assertEqual(n, 3)

    def test_5xx_not_counted_as_success(self):
        sess = _FakeSession(asset_status=500)
        n = warmup_page_assets(sess, "https://chatgpt.com/login", max_assets=10)
        self.assertEqual(n, 0)

    def test_uses_provided_html_without_fetching_page(self):
        sess = _FakeSession()
        n = warmup_page_assets(sess, "https://chatgpt.com/login", html=HTML, max_assets=10)
        self.assertEqual(n, 4)
        # 传了 html 就不应再 GET 页面，requested 只有 4 个资源
        self.assertEqual(len(sess.session.requested), 4)
        self.assertNotIn("https://chatgpt.com/login", sess.session.requested)


class AssetHeadersTests(unittest.TestCase):
    def test_headers_have_no_oai_or_datadog(self):
        sess = _FakeSession()
        h = _asset_headers(sess, "https://chatgpt.com/cdn/assets/main.css", "https://chatgpt.com/login")
        lower = {k.lower() for k in h}
        for forbidden in ("oai-device-id", "oai-client-version", "x-datadog-origin", "traceparent"):
            self.assertNotIn(forbidden, lower)
        self.assertEqual(h["sec-fetch-dest"], "style")
        self.assertEqual(h["sec-fetch-mode"], "no-cors")
        self.assertEqual(h["sec-fetch-site"], "same-origin")
        self.assertIn("text/css", h["accept"])

    def test_font_is_cors_cross_site(self):
        sess = _FakeSession()
        h = _asset_headers(sess, "https://auth-cdn.oaistatic.com/f.woff2", "https://chatgpt.com/login")
        self.assertEqual(h["sec-fetch-dest"], "font")
        self.assertEqual(h["sec-fetch-mode"], "cors")
        self.assertEqual(h["sec-fetch-site"], "cross-site")


if __name__ == "__main__":
    unittest.main()
