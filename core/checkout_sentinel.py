# -*- coding: utf-8 -*-
"""支付检测专用 Sentinel 令牌生成（同步封装）。

无 Sentinel 令牌的 /backend-api/payments/checkout 请求会被 OpenAI 风控按
"Our systems have detected unusual activity" 拒绝（HTTP 400）。本模块把
core/sentinel_token.py（逆向自 sentinel SDK）+ core/gen_token_jsdom.js
（Node.js + jsdom 运行 turnstile/session-observer VM）封装为同步入口，
供 MoMo/GCash/Kakao/PayPal/IDEAL/GoPay 检测内核统一调用。

运行依赖：Node.js 18+ 与项目根 node_modules/jsdom（npm install）。
任何缺依赖 / 生成失败都返回 None，检测自动降级为无令牌请求，不影响主流程。
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from core.sentinel_token import SentinelTokenProvider
from curl_cffi import requests

logger = logging.getLogger(__name__)

_CORE_DIR = Path(__file__).resolve().parent
_GEN_SCRIPT = _CORE_DIR / "gen_token_jsdom.js"
_SDK_SCRIPT = _CORE_DIR / "sentinel_sdk_full.js"

# 运行环境检查只做一次；失败原因记录后不再重复探测。
_env_checked = False
_env_available = False
_env_reason = ""


def _check_environment() -> bool:
    global _env_checked, _env_available, _env_reason
    if _env_checked:
        return _env_available
    _env_checked = True
    if not (_GEN_SCRIPT.exists() and _SDK_SCRIPT.exists()):
        _env_reason = f"缺少 {_GEN_SCRIPT.name} 或 {_SDK_SCRIPT.name}"
        return False
    if shutil.which("node") is None and shutil.which("node.exe") is None:
        _env_reason = "未找到 Node.js（需要 18+ 并加入 PATH）"
        return False
    if not (_CORE_DIR / ".." / "node_modules" / "jsdom").exists():
        _env_reason = "项目根缺少 node_modules/jsdom，请先执行 npm install"
        return False
    _env_available = True
    return True


def environment_status() -> dict:
    """供诊断/前端展示：Sentinel 运行时是否可用及原因。"""
    available = _check_environment()
    return {"available": available, "reason": "" if available else _env_reason}


class _ProxySentinel(SentinelTokenProvider):
    """经同一代理访问 sentinel.openai.com，与 checkout 请求出口保持一致。"""

    def __init__(self, proxy: str, cookies: dict[str, str]):
        super().__init__(impersonate="firefox144", cookies=cookies)
        self.proxy = proxy

    async def _get_session(self):
        if not self._session:
            self._session = requests.AsyncSession(
                impersonate="firefox144", timeout=70,
                proxies={"http": self.proxy, "https": self.proxy} if self.proxy else None,
            )
        return self._session


async def _generate(proxy: str, device_id: str, did: str) -> dict[str, str]:
    provider = _ProxySentinel(proxy, {"oai-did": did})
    try:
        token, so, diag = await provider.get_token_pair("chatgpt_checkout", device_id)
        if not token:
            raise RuntimeError(f"token 为空: {diag.get('init_error') or diag}")
        if diag.get("turnstile_required") and not diag.get("has_t"):
            raise RuntimeError("turnstile proof 缺失")
        if diag.get("so_required") and not diag.get("has_so"):
            raise RuntimeError("session-observer proof 缺失")
        import json as _json
        headers = {
            "OpenAI-Sentinel-Token": _json.dumps(token, separators=(",", ":")),
            "OpenAI-Sentinel-SO-Token": _json.dumps(so, separators=(",", ":")) if so else "",
        }
        return {k: v for k, v in headers.items() if v}
    finally:
        await provider.close()


def generate_checkout_sentinel_headers(proxy: str, device_id: str, did: str) -> dict[str, str] | None:
    """为一次 checkout 请求生成 Sentinel 头；失败返回 None（调用方降级）。"""
    if not _check_environment():
        logger.warning("[CheckoutSentinel] 运行环境不可用，本次检测不带 Sentinel: %s", _env_reason)
        return None
    try:
        return asyncio.run(_generate(proxy, device_id, did))
    except Exception as exc:
        logger.warning(
            "[CheckoutSentinel] 生成失败，本次检测不带 Sentinel: %s: %s",
            type(exc).__name__, str(exc)[:200],
        )
        return None
