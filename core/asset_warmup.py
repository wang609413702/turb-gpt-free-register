# -*- coding: utf-8 -*-
"""
纯协议资源预热层（实验 1：验证“请求稀疏”是否为封号主因）。

真实浏览器加载页面时会抓取 HTML 里引用的 CSS/JS/字体/图片等静态资源，
注册会话有数百条此类请求；纯协议原本只打 ~18 条核心 API，“零资源加载”
是强自动化特征。本模块在协议流程加载关键页面后，解析 HTML 引用的资源
并按接近真实浏览器的顺序带 Referer 抓取，把请求画像拉到接近真实浏览器。

设计约束：
- 纯附加、可降级、默认关闭（config.PROTOCOL_ASSET_WARMUP）。
- 任何资源请求失败都吞掉（404/超时是正常现象），不影响注册主流程。
- 直接用底层 curl_cffi session 抓资源，**不走 BrowserSession.get 的熔断器**：
  避免某个静态资源返回 403/429 把整个注册会话熔断掉。Cookie jar 仍共享。
- 不执行 JS，因此产生不了 JS 的二阶请求——这是协议层的固有天花板，
  本模块只能补“静态资源抓取”这一段。
"""
from __future__ import annotations

import logging
import random
import re
import time
from urllib.parse import urljoin, urlparse

from core.session import BrowserSession

logger = logging.getLogger(__name__)

# 只抓这些 host 下的资源，避免抓到第三方/广告/分析外链
_ALLOWED_HOSTS = (
    "chatgpt.com",
    "auth.openai.com",
    "auth-cdn.oaistatic.com",
    "cdn.openai.com",
    "sentinel.openai.com",
    "oaistatic.com",
    "persistent.oaistatic.com",
    "static.oaistatic.com",
)

# 资源扩展名 → (accept, sec-fetch-dest, 优先级组：0=css/js 先抓, 1=font, 2=image)
_EXT_PROFILE: dict[str, tuple[str, str, int]] = {
    "css": ("text/css,*/*;q=0.1", "style", 0),
    "js": ("*/*", "script", 0),
    "mjs": ("*/*", "script", 0),
    "woff": ("font/woff;q=0.9,*/*;q=0.8", "font", 1),
    "woff2": ("font/woff2;q=0.9,*/*;q=0.8", "font", 1),
    "ttf": ("font/ttf;q=0.9,*/*;q=0.8", "font", 1),
    "otf": ("font/otf;q=0.9,*/*;q=0.8", "font", 1),
    "png": ("image/avif,image/webp,image/apng,image/*,*/*;q=0.8", "image", 2),
    "jpg": ("image/avif,image/webp,image/apng,image/*,*/*;q=0.8", "image", 2),
    "jpeg": ("image/avif,image/webp,image/apng,image/*,*/*;q=0.8", "image", 2),
    "gif": ("image/avif,image/webp,image/apng,image/*,*/*;q=0.8", "image", 2),
    "webp": ("image/avif,image/webp,image/apng,image/*,*/*;q=0.8", "image", 2),
    "svg": ("image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8", "image", 2),
    "ico": ("image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8", "image", 2),
}

_IMG_ACCEPT = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"

_LINK_RE = re.compile(r"<link\b[^>]*\bhref=[\"']([^\"']+)[\"']", re.IGNORECASE)
_SCRIPT_RE = re.compile(r"<script\b[^>]*\bsrc=[\"']([^\"']+)[\"']", re.IGNORECASE)
_IMG_RE = re.compile(r"<img\b[^>]*\bsrc=[\"']([^\"']+)[\"']", re.IGNORECASE)

_SKIP_PREFIXES = ("data:", "blob:", "javascript:", "#", "mailto:", "tel:", "{", "http://{{")


def _ext_of(url: str) -> str:
    path = urlparse(url).path.lower()
    for sep in ("?", "#"):
        path = path.split(sep, 1)[0]
    if "." in path:
        return path.rsplit(".", 1)[-1][:5]
    return ""


def _host_allowed(host: str) -> bool:
    host = host.lower()
    return any(host == h or host.endswith("." + h) for h in _ALLOWED_HOSTS)


def extract_asset_urls(html: str, base_url: str) -> list[str]:
    """从 HTML 抽取静态资源 URL（去重、仅允许 host、仅已知资源类型）。

    保持文档出现顺序（浏览器大致按文档顺序发起），同 URL 去重。
    """
    raw: list[str] = []
    raw += _LINK_RE.findall(html)
    raw += _SCRIPT_RE.findall(html)
    raw += _IMG_RE.findall(html)

    seen: set[str] = set()
    out: list[str] = []
    for ref in raw:
        ref = (ref or "").strip()
        if not ref or ref.startswith(_SKIP_PREFIXES):
            continue
        try:
            url = urljoin(base_url, ref)
        except Exception:
            continue
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            continue
        if not _host_allowed(parsed.netloc):
            continue
        if _ext_of(url) not in _EXT_PROFILE:
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def _asset_headers(session: BrowserSession, asset_url: str, referer: str) -> dict:
    ext = _ext_of(asset_url)
    accept, dest, _grp = _EXT_PROFILE.get(ext, ("*/*", "empty", 3))
    # sec-fetch-mode：字体是 cors，style/script/image 是 no-cors（对齐真实浏览器）
    mode = "cors" if dest == "font" else "no-cors"
    site = "same-origin" if urlparse(asset_url).netloc == urlparse(referer).netloc else "cross-site"
    # 静态资源不带 oai-*/datadog（真实浏览器加载资源时也不带这些头）
    headers = session._get_common_headers()
    headers.update({
        "accept": accept,
        "sec-fetch-site": site,
        "sec-fetch-mode": mode,
        "sec-fetch-dest": dest,
        "referer": referer,
        # css/js/font 浏览器标较高优先级；图片较低
        "priority": "u=1, i" if dest in ("style", "script", "font") else "u=4, i",
    })
    return headers


def warmup_page_assets(
    session: BrowserSession,
    page_url: str,
    *,
    referer: str | None = None,
    max_assets: int = 60,
    timeout: float = 15.0,
    html: str | None = None,
) -> int:
    """加载 page_url 的 HTML 并抓取其引用的静态资源。

    Args:
        page_url: 页面 URL（用于抓 HTML 和解析相对路径）。
        referer: 资源请求的 Referer；默认用 page_url。
        max_assets: 单页最多抓多少个资源，控制耗时。
        timeout: 单请求超时。
        html: 已抓取的页面 HTML；传入则不再 GET 页面。
    Returns:
        成功抓取的资源数（5xx/异常不计）。
    """
    ref = referer or page_url

    if html is None:
        try:
            # 用底层 session 抓 HTML，避免触发 BrowserSession 熔断器
            resp = session.session.get(page_url, timeout=timeout)
            html = getattr(resp, "text", "") or ""
            if int(getattr(resp, "status_code", 0) or 0) >= 400:
                logger.debug("[AssetWarmup] %s HTML 状态 %s，跳过", page_url, resp.status_code)
                return 0
        except Exception as exc:
            logger.debug("[AssetWarmup] 抓取页面 HTML 失败 %s: %s: %s", page_url, type(exc).__name__, exc)
            return 0

    assets = extract_asset_urls(html, page_url)
    if not assets:
        logger.debug("[AssetWarmup] %s 未解析出资源", page_url)
        return 0

    # 按优先级分组（css/js 先、font 次、image 后），组内保持文档顺序
    assets.sort(key=lambda u: _EXT_PROFILE[_ext_of(u)][2])
    if len(assets) > max_assets:
        assets = assets[:max_assets]

    fetched = 0
    for url in assets:
        try:
            # 直接用底层 curl_cffi session：共享 cookie jar 和 TLS，但不触发熔断器，
            # 避免某个静态资源 403/429 把整个注册会话熔断。
            resp = session.session.get(url, headers=_asset_headers(session, url, ref), timeout=timeout)
            if int(getattr(resp, "status_code", 0) or 0) < 500:
                fetched += 1
        except Exception:
            continue
        # 轻微抖动，避免完全无间隔的机器节奏
        if random.random() < 0.25:
            time.sleep(random.uniform(0.02, 0.12))

    logger.info("[AssetWarmup] %s 解析 %d 资源，抓取成功 %d", page_url, len(assets), fetched)
    return fetched
