# -*- coding: utf-8 -*-
"""MoMo 提链引擎核心。

移植自 ideal-link-extractor-open-source/momo/momo_extract.py，
适配本项目的 BrowserSession（curl_cffi + Chrome 指纹）。

完整流程：
  Checkout → Stripe Init(bootstrap) → checkout/update(应用 0 元活动)
  → Stripe Init(refresh, 校验 amount==0) → PreConfirm(momo)
  → Create MoMo PM → Confirm(expected=0) → Approve(如需) → Poll
  → 跟随 redirect 到 payment.momo.vn 最终 URL
"""
from __future__ import annotations

import logging
import random
import re
import time
import uuid
from typing import Any
from urllib.parse import urljoin, urlparse

from core.session import BrowserSession

logger = logging.getLogger(__name__)

# Sentinel 相关（复用注册流程的 token 生成，绕开 checkout 风控）
try:
    from core.openai_auth import build_sentinel_header, request_sentinel_token
    _SENTINEL_AVAILABLE = True
except Exception:  # pragma: no cover
    build_sentinel_header = None
    request_sentinel_token = None
    _SENTINEL_AVAILABLE = False

# ==================== 常量（来自参考项目） ==================== #
CHATGPT_TIMEOUT = 45
STRIPE_TIMEOUT = 30
MOMO_UNAVAILABLE_ERROR = "当前账号支付方式不支持 MoMo"
STRIPE_VERSION_FULL = (
    "2025-03-31.basil; checkout_server_update_beta=v1; "
    "checkout_manual_approval_preview=v1"
)
DEFAULT_STRIPE_RUNTIME_VERSION = "6f8494a281"
CHATGPT_CLIENT_VERSION = "prod-db390ebea64862bf1899c420a4c736e0cf639747"
CHATGPT_CLIENT_BUILD_NUMBER = "7904904"

PROMO_ID = "plus-1-month-free"

CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
CHECKOUT_UPDATE_URL = "https://chatgpt.com/backend-api/payments/checkout/update"
APPROVE_URL = "https://chatgpt.com/backend-api/payments/checkout/approve"

# Stripe 请求统一头：必须带浏览器 UA，否则 Stripe 只返回 card/link 不含目标支付方式。
_STRIPE_HEADERS = {
    "accept": "application/json",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
}

# 支付方式配置：每种支付方式的国家/货币/账单资料/目标域名。
PAYMENT_CONFIGS: dict[str, dict] = {
    "momo": {
        "label": "MoMo",
        "stripe_type": "momo",
        "country": "VN",
        "currency": "VND",
        "locale": "vi-VN",
        "timezone": "Asia/Ho_Chi_Minh",
        "names": [
            ("Nguyen", "Van An"), ("Tran", "Thi Bich"), ("Le", "Minh Chau"),
            ("Pham", "Thi Dung"), ("Hoang", "Van Em"),
        ],
        "addresses": [
            ("1 Nguyen Trai", "Ho Chi Minh City", "700001"),
            ("2 Le Loi", "Hanoi", "100000"),
            ("50 Pham Van Dong", "Da Nang", "550000"),
            ("1 Lam Son", "Hai Phong", "040300"),
        ],
        "target_hosts": (".momo.vn",),
    },
    "gcash": {
        "label": "GCash",
        # Stripe 侧当前用 grabpay 承载菲律宾电子钱包（GCash），不认 gcash 类型。
        "stripe_type": "grabpay",
        "country": "PH",
        "currency": "PHP",
        "locale": "en-PH",
        "timezone": "Asia/Manila",
        "names": [
            ("Miguel", "Santos"), ("Maria", "Cruz"), ("Jose", "Reyes"),
            ("Ana", "Garcia"), ("Juan", "Dela Cruz"),
        ],
        "addresses": [
            ("6750 Ayala Avenue", "Makati", "1226"),
            ("21st Floor One Galleon Place", "Taguig", "1634"),
            ("100 Eastwood Avenue", "Quezon City", "1110"),
            ("2nd Ave Corner 30th St", "Taguig", "1634"),
        ],
        "target_hosts": (".gcash.com", ".mynt.com"),
    },
    "gopay": {
        "label": "GoPay",
        # Stripe 侧 type=gopay，ID/IDR；最终落地到 Midtrans SNAP 重定向页。
        "stripe_type": "gopay",
        "country": "ID",
        "currency": "IDR",
        "locale": "id-ID",
        "timezone": "Asia/Jakarta",
        "names": [
            ("Budi", "Santoso"), ("Siti", "Rahayu"), ("Agus", "Pratama"),
            ("Dewi", "Lestari"), ("Rudi", "Hartono"),
        ],
        "addresses": [
            ("Jl. Jenderal Sudirman Kav 52", "Jakarta", "12190"),
            ("Jl. M.H. Thamrin No 1", "Jakarta", "10310"),
            ("Jl. Ahmad Yani No 105", "Surabaya", "60236"),
            ("Jl. Gatot Subroto No 23", "Bandung", "40271"),
        ],
        "target_hosts": (".gopay.co.id", ".gojek.com", ".midtrans.com"),
    },
}


# ==================== 辅助函数 ==================== #
def _redact_proxy(proxy: str) -> str:
    """脱敏代理字符串用于日志：socks5h://***:***@host:port。

    代理里可能带账号/密码/session id，避免明文落入日志文件与前端展示。
    """
    if not proxy:
        return proxy or "直连"
    try:
        from urllib.parse import unquote, urlsplit
        parts = urlsplit(proxy)
        host = parts.hostname or ""
        if not host:
            return f"{str(proxy)[:24]}***"
        port = parts.port
        auth = ""
        if parts.username is not None or parts.password is not None:
            auth = "***:***@"
        base = f"{parts.scheme}://{auth}{host}"
        if port:
            base += f":{port}"
        return base
    except Exception:
        return f"{str(proxy)[:24]}***"


def _stripe_browser_id() -> str:
    return f"{uuid.uuid4()}{uuid.uuid4().hex[:8]}"


def _billing_profile(payment_method: str) -> dict[str, str]:
    cfg = PAYMENT_CONFIGS[payment_method]
    first, last = random.choice(cfg["names"])
    line1, city, postal = random.choice(cfg["addresses"])
    return {
        "email": f"{first.lower()}.{last.lower().replace(' ', '')}.{random.randint(100, 999)}@gmail.com",
        "name": f"{first} {last}",
        "country": cfg["country"],
        "line1": line1,
        "line2": "",
        "city": city,
        "postal_code": postal,
        "state": "",
    }


def _amount_from_payload(payload: dict[str, Any]) -> int:
    """从 stripe init 响应提取金额（最小单位）。"""
    summary = payload.get("total_summary")
    if isinstance(summary, dict) and summary.get("due") is not None:
        try:
            return int(summary["due"])
        except (TypeError, ValueError):
            pass
    for key in ("amount_total", "amount"):
        v = payload.get(key)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
    elements = payload.get("elements_options")
    if isinstance(elements, dict):
        v = elements.get("amount")
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
    return 0


def _normalize_method_token(value: Any) -> str:
    """归一化支付方式 token（对齐参考项目：grab_pay→grabpay 等）。"""
    token = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "kakao": "kakao_pay",
        "card_payment": "card",
        "direct_card": "card",
        "go_pay": "gopay",
        "grab_pay": "grabpay",
    }
    return aliases.get(token, token)


def _first_payment_method_types(payload: dict[str, Any]) -> list[str] | None:
    """递归收集支付方式（对齐参考项目）。

    读取 payment_method_types / ordered_payment_method_types / custom_payment_methods
    三组，支持 dict 条目（type/payment_method_type/name/id）。GCash 的 grabpay
    可能出现在 custom_payment_methods 里，只读 payment_method_types 会漏掉。
    """
    def _collect(obj: Any, out: list[str], depth: int = 0) -> None:
        if depth > 10:
            return
        if isinstance(obj, dict):
            for key, item in obj.items():
                lkey = str(key).lower()
                if lkey in ("payment_method_types", "ordered_payment_method_types", "custom_payment_methods"):
                    if isinstance(item, list):
                        for entry in item:
                            if isinstance(entry, str):
                                tok = _normalize_method_token(entry)
                                if tok:
                                    out.append(tok)
                            elif isinstance(entry, dict):
                                for ck in ("type", "payment_method_type", "name", "id"):
                                    tok = _normalize_method_token(entry.get(ck))
                                    if tok:
                                        out.append(tok)
                                        break
                if isinstance(item, (dict, list)):
                    _collect(item, out, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, (dict, list)):
                    _collect(item, out, depth + 1)

    collected: list[str] = []
    _collect(payload, collected)
    if not collected:
        return None
    seen = set()
    result = []
    for m in collected:
        if m not in seen:
            seen.add(m)
            result.append(m)
    return result


def _extract_redirect_url(payload: Any) -> str:
    """从 confirm/poll 响应递归提取支付跳转 URL。"""
    if isinstance(payload, dict):
        next_action = payload.get("next_action")
        if isinstance(next_action, dict):
            redirect = next_action.get("redirect_to_url")
            if isinstance(redirect, dict):
                url = str(redirect.get("url") or "").strip()
                if url and url.startswith("http"):
                    return url
            for key in ("url", "redirect_url", "redirect_to_url", "hosted_url"):
                value = next_action.get(key)
                if isinstance(value, str) and value.startswith("http"):
                    return value
        for key in ("redirect_url", "redirect_to_url", "authorization_url", "authentication_url"):
            value = payload.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value
        for value in payload.values():
            nested = _extract_redirect_url(value)
            if nested:
                return nested
    elif isinstance(payload, list):
        for item in payload:
            nested = _extract_redirect_url(item)
            if nested:
                return nested
    return ""


def _extract_qr_candidates(payload: Any) -> list[str]:
    """提取 QR 候选（data:image 或含 qr 的 URL）。"""
    qr: list[str] = []
    def _scan(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, str) and ("qr" in k.lower() or "qrcode" in k.lower()):
                    if v.startswith("data:image") or "qr" in v.lower():
                        qr.append(v)
                else:
                    _scan(v)
        elif isinstance(obj, list):
            for item in obj:
                _scan(item)
    _scan(payload)
    return list(dict.fromkeys(qr))


def _find_submission_attempt(payload: dict[str, Any]) -> dict[str, Any]:
    """查找 submission_attempt 状态。"""
    if isinstance(payload, dict):
        sa = payload.get("submission_attempt")
        if isinstance(sa, dict):
            return sa
        checkout_session = payload.get("checkout_session")
        if isinstance(checkout_session, dict):
            sa = checkout_session.get("submission_attempt")
            if isinstance(sa, dict):
                return sa
    return {}


def _processor_entity(data: dict[str, Any]) -> str:
    return str(data.get("processor_entity") or data.get("processorEntity") or "openai_ie")


def _is_checkout_not_active(text: str) -> bool:
    return "checkout_not_active_session" in str(text or "").lower()


def _is_already_paid(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(m in lowered for m in (
        "already has an active subscription", "already subscribed",
        "active subscription already exists", "user_already_paid",
    ))


# ==================== 网络请求函数 ==================== #
def _checkout_headers(env: BrowserSession, token: str, payment_method: str = "momo") -> dict[str, str]:
    token = str(token or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    headers = env._get_common_headers()
    headers.update({
        "accept": "*/*",
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
        "oai-device-id": env.device_id,
        # oai-language 跟随支付方式地区（momo=vi-VN / gcash=en-PH / gopay=id-ID），
        # 避免 OpenAI 按语言推断错地区导致支付方式列表不对。
        "oai-language": PAYMENT_CONFIGS[payment_method]["locale"],
        "oai-session-id": env.oai_session_id,
        "oai-client-version": CHATGPT_CLIENT_VERSION,
        "oai-client-build-number": CHATGPT_CLIENT_BUILD_NUMBER,
        "origin": "https://chatgpt.com",
        "referer": "https://chatgpt.com/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    })
    return headers


def _checkout_headers_sentinel(env: BrowserSession, token: str, payment_method: str = "momo",
                               log_fn=None) -> dict[str, str]:
    """带 Sentinel 头的 checkout 请求头。

    所有打 chatgpt.com /backend-api/payments/* 的 POST 都必须带 sentinel，
    否则 Cloudflare 会返回 403（ID/PH 等非 US 出口尤其严格）。
    sentinel 生成失败时降级为基础头（调用方据此判定是否重试）。
    """
    headers = _checkout_headers(env, token, payment_method)
    sentinel_headers = _generate_sentinel_headers(env, log_fn=log_fn)
    if sentinel_headers:
        headers.update(sentinel_headers)
    return headers


def _generate_sentinel_headers(env: BrowserSession, log_fn=None) -> dict[str, str]:
    """生成 OpenAI-Sentinel-Token 请求头。

    参考脚本通过浏览器加载 sdk.js 生成；这里复用注册流程的
    sentinel/req challenge + Node runner 方案，效果相同。
    失败返回空 dict（调用方降级为不带 sentinel 的 checkout）。
    log_fn: 失败时把原因写进前端日志（不写则只走 logger）。
    """
    def _log(msg: str) -> None:
        logger.warning("[MoMoLink] %s", msg)
        if log_fn:
            try:
                log_fn(msg)
            except Exception:
                pass

    if not _SENTINEL_AVAILABLE or request_sentinel_token is None or build_sentinel_header is None:
        _log("⚠ Sentinel 模块不可用，降级为不带 sentinel 的 checkout（可能被 Cloudflare 拦截）")
        return {}
    try:
        challenge = request_sentinel_token(env, "chatgpt_checkout")
        sentinel_header, so_header = build_sentinel_header(env, challenge, "chatgpt_checkout")
        out: dict[str, str] = {}
        if sentinel_header:
            out["OpenAI-Sentinel-Token"] = str(sentinel_header)
        if so_header:
            out["OpenAI-Sentinel-So-Token"] = str(so_header)
        return out
    except Exception as exc:
        _log(f"⚠ Sentinel token 生成失败（{type(exc).__name__}: {str(exc)[:120]}），降级为不带 sentinel 的 checkout")
        return {}


def _create_checkout(env: BrowserSession, token: str, payment_method: str = "momo",
                     log_fn=None) -> dict[str, str]:
    """Step 1: 创建 Checkout Session（国家/货币按支付方式，带 plus-1-month-free 0 元活动）。

    通道选择：
      - momo:  sidebar_upsell → Stripe cs_ 通道
      - gcash/gopay: all_plans_pricing_modal + checkout_ui_mode: custom →
               OpenAI 自营 oaics_ 通道（支付方式在 custom_payment_methods 里）；
               GoPay 同入口，实测可能返回 cs_（Stripe）通道，双通道流程均通用。
    """
    cfg = PAYMENT_CONFIGS[payment_method]
    if payment_method in ("gcash", "gopay"):
        # oaics_ 通道：必须用 all_plans_pricing_modal + checkout_ui_mode: custom
        body = {
            "entry_point": "all_plans_pricing_modal",
            "plan_name": "chatgptplusplan",
            "billing_details": {"country": cfg["country"], "currency": cfg["currency"]},
            "checkout_ui_mode": "custom",
            "promo_campaign": {"promo_campaign_id": PROMO_ID, "is_coupon_from_query_param": False},
        }
    else:
        body = {
            "entry_point": "sidebar_upsell",
            "plan_name": "chatgptplusplan",
            "price_interval": "month",
            "seat_quantity": 1,
            "billing_details": {"country": cfg["country"], "currency": cfg["currency"]},
            "cancel_url": "https://chatgpt.com/#pricing",
            "promo_campaign": {"promo_campaign_id": PROMO_ID, "is_coupon_from_query_param": False},
        }
    headers = _checkout_headers_sentinel(env, token, payment_method, log_fn=log_fn)
    resp = env.post(CHECKOUT_URL, json=body, headers=headers, timeout=CHATGPT_TIMEOUT)
    status = int(getattr(resp, "status_code", 0) or 0)
    if status >= 400:
        if _is_already_paid(getattr(resp, "text", "")):
            raise RuntimeError("用户已支付: User is already paid")
        raise RuntimeError(f"checkout 创建失败 HTTP {status}: {(getattr(resp, 'text', '') or '')[:400]}")
    data = resp.json() or {}
    cs_id = data.get("checkout_session_id") or data.get("session_id") or data.get("id")
    if not cs_id or not (str(cs_id).startswith("cs_") or str(cs_id).startswith("oaics_")):
        raise RuntimeError(f"checkout 响应缺少有效 session id: {str(data)[:300]}")
    raw_pk = (
        data.get("stripe_publishable_key") or data.get("publishable_key")
        or data.get("publishableKey") or data.get("stripePublishableKey")
        or data.get("key") or ""
    )
    match = re.search(r"pk_live_[A-Za-z0-9]+", str(raw_pk))
    stripe_pk = match.group(0) if match else ""
    provider = str(data.get("checkout_provider") or "")
    cpmt_id = ""
    cpmts = data.get("custom_payment_methods")
    if isinstance(cpmts, list) and cpmts:
        first = cpmts[0]
        cpmt_id = str((first.get("id") if isinstance(first, dict) else "") or "")
    logger.info("[MoMoLink] checkout 创建成功: %s (provider=%s)", cs_id, provider or "stripe")
    is_oaics = str(cs_id).startswith("oaics_")
    return {
        "cs_id": str(cs_id),
        "processor_entity": _processor_entity(data),
        "stripe_pk": stripe_pk,
        # 统一用 oaics 标识 OpenAI 自营通道（checkout_provider=open_ai 或 id 前缀 oaics_）
        "provider": "oaics" if is_oaics else ("stripe" if provider in ("", "stripe") else provider),
        "custom_payment_method_id": cpmt_id,
    }


def _update_checkout_promotion(env: BrowserSession, token: str, checkout: dict[str, str],
                               payment_method: str = "momo", log_fn=None) -> None:
    """Step 3: 应用 0 元优惠活动。env 使用独立代理（第二个代理池）。"""
    body = {
        "checkout_session_id": checkout["cs_id"],
        "processor_entity": checkout.get("processor_entity", "openai_ie"),
        "plan_name": "chatgptplusplan",
        "price_interval": "month",
        "seat_quantity": 1,
        "promo_campaign": {"promo_campaign_id": PROMO_ID, "is_coupon_from_query_param": False},
    }
    checkout_page_url = f"https://chatgpt.com/checkout/{checkout.get('processor_entity', 'openai_ie')}/{checkout['cs_id']}"
    headers = _checkout_headers_sentinel(env, token, payment_method, log_fn=log_fn)
    headers["referer"] = checkout_page_url
    resp = env.post(CHECKOUT_UPDATE_URL, json=body, headers=headers, timeout=CHATGPT_TIMEOUT)
    status = int(getattr(resp, "status_code", 0) or 0)
    if status >= 400:
        if _is_checkout_not_active(getattr(resp, "text", "")):
            raise RuntimeError("checkout_not_active_session")
        raise RuntimeError(f"checkout/update 失败 HTTP {status}: {(getattr(resp, 'text', '') or '')[:400]}")
    try:
        payload = resp.json() or {}
    except Exception:
        payload = {}
    if isinstance(payload, dict) and payload.get("success") is False:
        raise RuntimeError(f"checkout/update rejected: {str(payload)[:300]}")
    logger.info("[MoMoLink] checkout/update 成功")


def _stripe_init(env: BrowserSession, cs_id: str, stripe_pk: str, payment_method: str = "momo") -> dict[str, Any]:
    """Stripe init：读取 init_checksum / config_id / amount / payment_method_types。

    必须带浏览器 User-Agent：Stripe 依据请求指纹决定返回的支付方式集，
    缺 UA（curl 默认）时只返回 card/link，不含目标支付方式。
    """
    cfg = PAYMENT_CONFIGS[payment_method]
    stripe_js_id = str(uuid.uuid4())
    body = {
        "browser_locale": cfg["locale"],
        "browser_timezone": cfg["timezone"],
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": cfg["locale"],
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION_FULL,
    }
    url = f"https://api.stripe.com/v1/payment_pages/{cs_id}/init"
    resp = env.session.post(url, data=body, headers=dict(_STRIPE_HEADERS), timeout=STRIPE_TIMEOUT)
    status = int(getattr(resp, "status_code", 0) or 0)
    if status >= 400:
        raise RuntimeError(f"Stripe init 失败 HTTP {status}: {(getattr(resp, 'text', '') or '')[:400]}")
    payload = resp.json() or {}
    payload["_stripe_js_id"] = stripe_js_id
    # 提取 hosted URL 供 confirm 构造 return_url（对齐参考项目）
    hosted = str(payload.get("stripe_hosted_url") or payload.get("hosted_url") or "").strip()
    if hosted:
        payload["_stripe_hosted_url"] = hosted
    return payload


def _build_ctx(init_payload: dict[str, Any]) -> dict[str, Any]:
    stripe_js_id = str(init_payload.get("_stripe_js_id") or uuid.uuid4())
    return {
        "stripe_js_id": stripe_js_id,
        "client_session_id": str(uuid.uuid4()),
        "guid": _stripe_browser_id(),
        "muid": _stripe_browser_id(),
        "sid": _stripe_browser_id(),
        "elements_session_id": f"elements_session_{uuid.uuid4().hex[:11]}",
        "elements_session_config_id": str(init_payload.get("config_id") or uuid.uuid4()),
        "config_id": init_payload.get("config_id") or "",
        "init_checksum": init_payload.get("init_checksum") or "",
        "checkout_amount": _amount_from_payload(init_payload),
        "runtime_version": DEFAULT_STRIPE_RUNTIME_VERSION,
        "stripe_version": STRIPE_VERSION_FULL,
    }


def _elements_session_params(ctx: dict[str, Any], payment_method: str = "momo") -> dict[str, str]:
    cfg = PAYMENT_CONFIGS[payment_method]
    return {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[session_id]": str(ctx.get("elements_session_id") or ""),
        "elements_session_client[stripe_js_id]": str(ctx.get("stripe_js_id") or ""),
        "elements_session_client[locale]": cfg["locale"],
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
    }


def _stripe_pre_confirm(env: BrowserSession, cs_id: str, stripe_pk: str, payment_method: str = "momo") -> dict[str, Any]:
    stripe_type = PAYMENT_CONFIGS[payment_method]["stripe_type"]
    body = {"payment_method_type": stripe_type, "key": stripe_pk, "_stripe_version": STRIPE_VERSION_FULL}
    url = f"https://api.stripe.com/v1/payment_pages/{cs_id}/pre_confirm"
    resp = env.session.post(url, data=body, headers=dict(_STRIPE_HEADERS), timeout=STRIPE_TIMEOUT)
    status = int(getattr(resp, "status_code", 0) or 0)
    if status >= 400:
        raise RuntimeError(f"{payment_method} PreConfirm 失败 HTTP {status}: {(getattr(resp, 'text', '') or '')[:400]}")
    return resp.json() or {}


def _stripe_create_pm(env: BrowserSession, cs_id: str, stripe_pk: str, billing: dict[str, str],
                      payment_method: str = "momo", ctx: dict[str, Any] | None = None) -> str:
    """创建支付方式 PM。参数对齐参考项目（GPT-Register-Tool wallet_provider）。

    必须带完整的 payment_user_agent / attribution 元数据，
    否则 Stripe 对 PM 的信任判定降低，可能导致后续 SetupIntent 绑定
    generic_decline（GoPay 0 元单实测踩过）。
    """
    stripe_js_id = str((ctx or {}).get("stripe_js_id") or uuid.uuid4())
    body = {
        "billing_details[name]": billing["name"],
        "billing_details[email]": billing["email"],
        "billing_details[address][country]": billing["country"],
        "billing_details[address][line1]": billing["line1"],
        "billing_details[address][city]": billing["city"],
        "billing_details[address][state]": billing.get("state") or "",
        "billing_details[address][postal_code]": billing["postal_code"],
        "type": PAYMENT_CONFIGS[payment_method]["stripe_type"],
        "payment_user_agent": (
            f"stripe.js/{DEFAULT_STRIPE_RUNTIME_VERSION}; "
            f"stripe-js-v3/{DEFAULT_STRIPE_RUNTIME_VERSION}; "
            "payment-element; deferred-intent"
        ),
        "referrer": "https://chatgpt.com",
        "time_on_page": "30000",
        "client_attribution_metadata[client_session_id]": stripe_js_id,
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "2021",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "guid": _stripe_browser_id(),
        "muid": _stripe_browser_id(),
        "sid": _stripe_browser_id(),
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION_FULL,
    }
    resp = env.session.post("https://api.stripe.com/v1/payment_methods", data=body,
                            headers=dict(_STRIPE_HEADERS), timeout=STRIPE_TIMEOUT)
    status = int(getattr(resp, "status_code", 0) or 0)
    if status >= 400:
        raise RuntimeError(f"创建 {payment_method} PM 失败 HTTP {status}: {(getattr(resp, 'text', '') or '')[:400]}")
    pm_id = str((resp.json() or {}).get("id") or "")
    if not pm_id.startswith("pm_"):
        raise RuntimeError(f"{payment_method} PM 响应异常: {(getattr(resp, 'text', '') or '')[:300]}")
    return pm_id


def _stripe_confirm(env: BrowserSession, cs_id: str, pm_id: str, stripe_pk: str,
                    init_payload: dict[str, Any], ctx: dict[str, Any],
                    checkout: dict[str, str], payment_method: str = "momo") -> dict[str, Any]:
    # return_url 对齐参考项目：优先由 init 响应的 stripe_hosted_url 映射
    # （checkout.stripe.com → pay.openai.com），否则用标准 fallback
    hosted_url = str(init_payload.get("_stripe_hosted_url") or init_payload.get("stripe_hosted_url") or "").strip()
    if hosted_url.startswith("https://checkout.stripe.com"):
        return_url = "https://pay.openai.com" + hosted_url[len("https://checkout.stripe.com"):]
    elif hosted_url.startswith("https://pay.openai.com"):
        return_url = hosted_url
    else:
        return_url = f"https://pay.openai.com/c/pay/{cs_id}?returned_from_redirect=true&ui_mode=custom"
    # 0 元提链：promo 已生效，confirm 用 expected_amount=0
    expected_amount = "0"
    body = {
        "expected_amount": expected_amount,
        "expected_payment_method_type": PAYMENT_CONFIGS[payment_method]["stripe_type"],
        "return_url": return_url,
        "_stripe_version": STRIPE_VERSION_FULL,
        "guid": ctx["guid"],
        "muid": ctx["muid"],
        "sid": ctx["sid"],
        "key": stripe_pk,
        "version": DEFAULT_STRIPE_RUNTIME_VERSION,
        "init_checksum": str(init_payload.get("init_checksum") or ctx.get("init_checksum") or ""),
        "client_attribution_metadata[client_session_id]": ctx.get("stripe_js_id") or ctx.get("client_session_id") or "",
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "client_attribution_metadata[checkout_config_id]": ctx.get("config_id") or "",
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "custom",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[elements_session_id]": ctx["elements_session_id"],
        "client_attribution_metadata[elements_session_config_id]": ctx["elements_session_config_id"],
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "consent[terms_of_service]": "accepted",
        "payment_method": pm_id,
    }
    body.update(_elements_session_params(ctx, payment_method))
    url = f"https://api.stripe.com/v1/payment_pages/{cs_id}/confirm"
    resp = env.session.post(url, data=body, headers=dict(_STRIPE_HEADERS), timeout=STRIPE_TIMEOUT)
    status = int(getattr(resp, "status_code", 0) or 0)
    if status >= 400:
        raise RuntimeError(f"{payment_method} confirm 失败 HTTP {status}: {(getattr(resp, 'text', '') or '')[:400]}")
    return resp.json() or {}


def _chatgpt_approve(env: BrowserSession, token: str, checkout: dict[str, str],
                     payment_method: str = "momo", log_fn=None) -> None:
    cs_id = checkout["cs_id"]
    processor = checkout.get("processor_entity", "openai_ie")
    checkout_page_url = f"https://chatgpt.com/checkout/{processor}/{cs_id}"
    body = {"checkout_session_id": cs_id, "processor_entity": processor}
    headers = _checkout_headers_sentinel(env, token, payment_method, log_fn=log_fn)
    headers["referer"] = checkout_page_url
    resp = env.post(APPROVE_URL, json=body, headers=headers, timeout=CHATGPT_TIMEOUT)
    status = int(getattr(resp, "status_code", 0) or 0)
    if status >= 400:
        raise RuntimeError(f"approve 失败 HTTP {status}: {(getattr(resp, 'text', '') or '')[:300]}")
    result = ""
    try:
        result = str((resp.json() or {}).get("result") or "")
    except Exception:
        pass
    if result != "approved":
        raise RuntimeError(f"approve 未通过: {result or (getattr(resp, 'text', '') or '')[:200]}")


# ==================== 扫码支付监测 ==================== #
def _pw_proxy_config(proxy: str) -> dict[str, str] | None:
    """把代理字符串转成 Playwright 代理配置；空/解析失败返回 None（直连）。

    Chromium 不认 socks5h/socks4a 协议头（会报 ERR_NO_SUPPORTED_PROXIES），
    归一化为 socks5/socks4（Playwright 的 socks5 默认即远程 DNS 解析）。
    """
    if not proxy:
        return None
    try:
        from urllib.parse import unquote, urlsplit
        parts = urlsplit(proxy)
        host = f"{parts.hostname}:{parts.port}" if parts.port else parts.hostname
        scheme = str(parts.scheme or "").lower()
        if scheme in ("socks5h", "socks5"):
            scheme = "socks5"
        elif scheme in ("socks4a", "socks4"):
            scheme = "socks4"
        elif scheme not in ("http", "https"):
            scheme = "http"
        pw: dict[str, str] = {"server": f"{scheme}://{host}"}
        if parts.username:
            pw["username"] = unquote(parts.username)
        if parts.password:
            pw["password"] = unquote(parts.password)
        return pw
    except Exception as exc:
        logger.warning("[Monitor] 代理配置解析失败，使用直连: %s", exc)
        return None


def _continue_custom_payment(access_token: str, proxy: str, cs_id: str,
                             redirect_result: str, timeout: float = 20) -> dict:
    """提交 Adyen 支付结果给 OpenAI（verify 页面的 continueCustomPaymentMethodFlow）。

    POST /backend-api/payments/checkout/custom_payment_method/continue
    body: {checkout_session_id, action_result: {redirectResult}}
    返回响应 dict；失败返回 {}。
    """
    if not access_token or not cs_id or not redirect_result:
        return {}
    path = "/backend-api/payments/checkout/custom_payment_method/continue"
    env = None
    try:
        env = BrowserSession(proxy=proxy or "", detect_exit_geo=False)
        # 用 payment_method 无关的 checkout 头构造（continue 不区分支付方式），
        # 复用 sentinel 生成逻辑绕过 Cloudflare（非 US 出口必须带 sentinel）。
        try:
            headers = _checkout_headers_sentinel(env, access_token, "momo")
        except Exception:
            headers = env._get_common_headers()
            headers.update({"accept": "*/*", "authorization": f"Bearer {str(access_token).strip()}"})
        headers.update({
            "oai-language": env.navigator_language(),
            "referer": "https://chatgpt.com/checkout/verify",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "x-openai-target-path": path,
            "x-openai-target-route": path,
        })
        body = {
            "checkout_session_id": cs_id,
            "action_result": {"redirectResult": redirect_result},
        }
        resp = env.post(f"https://chatgpt.com{path}", json=body, headers=headers, timeout=timeout)
        if not (200 <= int(resp.status_code) < 300):
            logger.warning("[Monitor] continue 提交失败 HTTP %s: %s",
                           resp.status_code, (getattr(resp, "text", "") or "")[:200])
            return {}
        try:
            data = resp.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("[Monitor] continue 提交异常: %s: %s", type(exc).__name__, str(exc)[:120])
        return {}
    finally:
        if env is not None:
            try:
                env.session.close()
            except Exception:
                pass


def _fetch_checkout_session_status(
    access_token: str,
    proxy: str,
    cs_id: str,
    processor_entity: str = "openai_llc",
    timeout: float = 20,
) -> dict:
    """用账号 token 查询 checkout session 状态（verify 页面轮询的同一接口）。

    GET /backend-api/payments/checkout/{processor_entity}/{checkout_session_id}
    返回响应 dict；失败返回 {}。
    """
    if not access_token or not cs_id:
        return {}
    path = f"/backend-api/payments/checkout/{processor_entity}/{cs_id}"
    env = None
    try:
        env = BrowserSession(proxy=proxy or "", detect_exit_geo=False)
        headers = env._get_common_headers()
        headers.update({
            "accept": "*/*",
            "authorization": f"Bearer {str(access_token).strip()}",
            "oai-device-id": env.device_id,
            "oai-language": env.navigator_language(),
            "referer": "https://chatgpt.com/checkout/verify",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "x-openai-target-path": path,
            "x-openai-target-route": path,
        })
        resp = env.session.get(f"https://chatgpt.com{path}", headers=headers, timeout=timeout)
        if not (200 <= int(resp.status_code) < 300):
            return {}
        try:
            data = resp.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("[Monitor] checkout 状态查询失败: %s: %s", type(exc).__name__, str(exc)[:120])
        return {}
    finally:
        if env is not None:
            try:
                env.session.close()
            except Exception:
                pass


def _start_socks_relay(proxy: str) -> tuple[str, int] | None:
    """把带认证的 socks5h 代理包装成本地无认证 socks5 转发器。

    Playwright 的 Chromium 不支持 socks5 用户名/密码认证，
    这里在 127.0.0.1 随机端口起一个转发器：本地无认证握手 + 与远程
    代理完成认证握手后双向透传。返回 (127.0.0.1, port)；失败返回 None。
    """
    import socket
    import threading
    from urllib.parse import unquote, urlsplit

    try:
        parts = urlsplit(proxy)
        if not parts.hostname:
            return None
        remote_host = str(parts.hostname)
        remote_port = int(parts.port or 1080)
        username = unquote(parts.username) if parts.username else ""
        password = unquote(parts.password) if parts.password else ""
    except Exception:
        return None

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        listener.settimeout(0.5)
    except Exception:
        listener.close()
        return None
    local_port = listener.getsockname()[1]

    def _pump(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except Exception:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except Exception:
                pass

    def _handle(client: socket.socket) -> None:
        remote = None
        try:
            # 1) 读取本地 Chromium 的 SOCKS5 协商请求并回复"无认证"
            req = client.recv(2)
            if len(req) < 2 or req[0] != 0x05:
                return
            nmethods = req[1]
            methods = b""
            while len(methods) < nmethods:
                chunk = client.recv(nmethods - len(methods))
                if not chunk:
                    return
                methods += chunk
            client.sendall(b"\x05\x00")

            # 2) 连接远程代理并完成认证握手
            remote = socket.create_connection((remote_host, remote_port), timeout=20)
            if username:
                remote.sendall(b"\x05\x02\x00\x02")
            else:
                remote.sendall(b"\x05\x01\x00")
            resp = remote.recv(2)
            if len(resp) < 2 or resp[0] != 0x05:
                return
            method = resp[1]
            if method == 0x02 and username:
                u = username.encode("utf-8")
                p = password.encode("utf-8")
                if len(u) > 255 or len(p) > 255:
                    return
                remote.sendall(bytes([0x01, len(u)]) + u + bytes([len(p)]) + p)
                auth = remote.recv(2)
                if len(auth) < 2 or auth[1] != 0x00:
                    return
            elif method != 0x00:
                return

            # 3) 读取本地 CONNECT 请求（支持 IPv4 / 域名 / IPv6）并转发给远程
            hdr = client.recv(4)
            if len(hdr) < 4 or hdr[0] != 0x05 or hdr[1] != 0x01:
                return
            atyp = hdr[3]
            if atyp == 0x01:      # IPv4
                connect = hdr + client.recv(4) + client.recv(2)
            elif atyp == 0x03:    # 域名
                ln = client.recv(1)
                if not ln:
                    return
                connect = hdr + ln + client.recv(ln[0]) + client.recv(2)
            elif atyp == 0x04:    # IPv6
                connect = hdr + client.recv(16) + client.recv(2)
            else:
                return
            remote.sendall(connect)

            # 4) 读取远程响应并转发给本地
            rresp = remote.recv(4)
            if len(rresp) < 4 or rresp[1] != 0x00:
                return
            ratyp = rresp[3]
            rest = b""
            if ratyp == 0x01:
                rest = remote.recv(6)
            elif ratyp == 0x03:
                ln = remote.recv(1)
                rest = remote.recv(ln[0] + 2) if ln else b""
            elif ratyp == 0x04:
                rest = remote.recv(18)
            client.sendall(rresp + rest)

            # 5) 双向透传
            t1 = threading.Thread(target=_pump, args=(client, remote), daemon=True)
            t2 = threading.Thread(target=_pump, args=(remote, client), daemon=True)
            t1.start()
            t2.start()
            t1.join()
            t2.join()
        except Exception:
            pass
        finally:
            try:
                client.close()
            except Exception:
                pass
            if remote is not None:
                try:
                    remote.close()
                except Exception:
                    pass

    def _accept_loop() -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except Exception:
                break
            threading.Thread(target=_handle, args=(conn,), daemon=True).start()

    threading.Thread(target=_accept_loop, daemon=True).start()
    return ("127.0.0.1", local_port, listener)


def monitor_payment_qr(
    redirect_url: str,
    *,
    proxy: str = "",
    access_token: str = "",
    cs_id: str = "",
    processor_entity: str = "openai_llc",
    timeout_sec: int = 900,
    plan_check_interval: float = 8.0,
    log_fn=None,
    on_qr=None,
    is_cancelled=None,
    require_qr: bool = True,
) -> dict:
    """保持浏览器会话打开支付页面，提取二维码并监测扫码支付结果。

    二维码是会话绑定的：必须用同一个浏览器会话打开支付页，
    页面显示的二维码扫码后，支付回调会驱动页面跳回 chatgpt.com。
    因此这里保持浏览器不关闭，直到支付完成或超时。

    支付成功判定（两个信号任一成立）：
      1. checkout session 状态变为 complete
         （verify 页面轮询的同一接口：GET /payments/checkout/{processor}/{cs_id}）
      2. 账号套餐状态变为 plus / 有活跃订阅

    redirect_url: 提链得到的支付页链接（Adyen / momo.vn）
    proxy: 必须与创建 checkout 时使用的代理一致
    access_token: 用于轮询 checkout session 与套餐状态
    cs_id: checkout session id（oaics_xxx），用于轮询支付确认状态
    processor_entity: openai_llc（oaics 通道）或 openai_ie（stripe 通道）
    timeout_sec: 等待扫码支付的总超时（默认 15 分钟）
    plan_check_interval: 轮询状态的间隔秒数
    on_qr(qr_data): 提取到二维码后回调（存入记录供前端展示）
    is_cancelled() -> bool: 返回 True 时提前结束监测（如记录被删除）
    require_qr: 默认 True——限时未找到二维码即失败退出；GoPay（Midtrans
                SNAP 页）可能不渲染二维码、改由手机 App 确认，传 False
                时无码继续轮询到账。
    返回 {ok, status: paid/timeout/error/cancelled, plan_type, qr_data, error}
    """
    def _log(msg: str) -> None:
        logger.info("[Monitor] %s", msg)
        if log_fn:
            try:
                log_fn(msg)
            except Exception:
                pass

    qr_data = ""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        _log(f"✗ playwright 不可用: {exc}")
        return {"ok": False, "status": "error", "error": f"playwright 不可用: {exc}"}

    launch_kwargs: dict[str, Any] = {"headless": True}
    relay_listener = None
    pw_proxy = _pw_proxy_config(proxy)
    if pw_proxy:
        # Chromium 不支持带认证的 socks5 代理（socks5 proxy authentication），
        # 本地起无认证转发器：Chromium -> 本地 socks5 -> 远程认证代理。
        if str(pw_proxy.get("server") or "").startswith("socks"):
            relay = _start_socks_relay(proxy)
            if relay:
                relay_host, relay_port, relay_listener = relay
                pw_proxy = {"server": f"socks5://{relay_host}:{relay_port}"}
                _log(f"已启动本地 socks5 转发器 127.0.0.1:{relay_port} -> {_redact_proxy(proxy)}")
            else:
                _log("⚠ 本地 socks5 转发器启动失败，尝试系统 Chrome")
                launch_kwargs["channel"] = "chrome"
        launch_kwargs["proxy"] = pw_proxy

    deadline = time.time() + timeout_sec
    page = None
    browser = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(**launch_kwargs)
            page = browser.new_page()
            _log(f"打开支付页: {redirect_url[:80]}...")
            page.goto(redirect_url, timeout=45000, wait_until="domcontentloaded")

            # 等待并提取二维码（#qrcode 容器；部分页面延迟渲染）
            qr_deadline = time.time() + 60
            while time.time() < qr_deadline:
                try:
                    qr_el = page.query_selector("#qrcode img")
                    if qr_el:
                        src = qr_el.get_attribute("src") or ""
                        if src.startswith("data:image") and len(src) > 1000:
                            qr_data = src
                            break
                    for sel in ("#qrcode img[src^='data:image']",
                                ".qr-container img[src^='data:image']",
                                ".qr-section img[src^='data:image']"):
                        for el in page.query_selector_all(sel):
                            src = el.get_attribute("src") or ""
                            if len(src) > 1000:
                                qr_data = src
                                break
                        if qr_data:
                            break
                    # 兜底：全局扫描 data:image 图片（覆盖 Midtrans SNAP 等自定义页面）
                    if not qr_data:
                        for el in page.query_selector_all("img"):
                            src = el.get_attribute("src") or ""
                            if src.startswith("data:image") and len(src) > 1000:
                                qr_data = src
                                break
                except Exception:
                    pass
                if qr_data:
                    break
                page.wait_for_timeout(1500)

            if not qr_data:
                if require_qr:
                    _log("✗ 未在支付页中找到二维码")
                    return {"ok": False, "status": "error", "qr_data": "", "error": "支付页未显示二维码，链接可能已失效"}
                _log("⚠ 未在支付页中找到二维码（继续监测到账，请在手机端完成支付）")
            else:
                _log("✓ 二维码已提取，等待扫码支付...")
            if on_qr and qr_data:
                try:
                    on_qr(qr_data)
                except Exception as exc:
                    _log(f"  on_qr 回调失败: {exc}")

            # 监测循环：
            #   1) 页面跳回 chatgpt.com（Adyen/momo 回调完成）是"支付已发生"的信号
            #   2) 但到账需后端确认：轮询 checkout session 至 complete / planType 变 plus
            last_plan = ""
            last_checkout_status = ""
            next_plan_check = 0.0
            next_checkout_check = 0.0
            redirected_back = False
            submitted_continue = False
            paid_reason = ""
            while time.time() < deadline:
                # 记录被删除/用户取消时提前结束
                if is_cancelled is not None:
                    try:
                        if is_cancelled():
                            _log("✗ 监测已取消（记录被删除）")
                            return {
                                "ok": False, "status": "cancelled", "qr_data": qr_data,
                                "plan_type": last_plan, "error": "监测已取消",
                            }
                    except Exception:
                        pass

                # 页面跳转检测：支付回调完成后 Adyen/momo 页会跳回 chatgpt.com
                try:
                    current_url = page.url or ""
                except Exception:
                    current_url = ""
                if not redirected_back and "chatgpt.com" in current_url:
                    redirected_back = True
                    _log(f"  支付页已跳回 chatgpt.com: {current_url[:100]}")
                    # 从跳转 URL 提取 redirectResult（Adyen 回调参数）并提交确认
                    try:
                        from urllib.parse import parse_qs, urlsplit
                        qs = parse_qs(urlsplit(current_url).query)
                        rr = (qs.get("redirectResult") or [""])[0]
                        if rr and cs_id and not submitted_continue:
                            submitted_continue = True
                            _log("  检测到 redirectResult，提交支付确认...")
                            cont_res = _continue_custom_payment(
                                access_token, proxy, cs_id, rr)
                            cont_status = str(cont_res.get("status") or "")
                            if cont_status:
                                _log(f"  continue 结果: {cont_status}")
                            else:
                                _log("  continue 提交未返回状态（后端可能已确认）")
                    except Exception as exc:
                        _log(f"  redirectResult 提交失败: {type(exc).__name__}: {str(exc)[:100]}")

                # 轮询 checkout session 状态（到账确认）
                if access_token and cs_id and time.time() >= next_checkout_check:
                    next_checkout_check = time.time() + plan_check_interval
                    checkout_data = _fetch_checkout_session_status(
                        access_token, proxy, cs_id, processor_entity=processor_entity)
                    status = str(checkout_data.get("status") or "").strip()
                    if status and status != last_checkout_status:
                        last_checkout_status = status
                        _log(f"  checkout session 状态: {status}")
                    if status.lower() == "complete":
                        paid_reason = "checkout session 已 complete（支付已确认到账）"
                        break

                # 轮询套餐状态
                if access_token and time.time() >= next_plan_check:
                    next_plan_check = time.time() + plan_check_interval
                    try:
                        from core.chatgpt_plan import check_account_plan
                        plan_res = check_account_plan(access_token, proxy=proxy or None, max_attempts=1)
                        plan_type = str(plan_res.get("current_plan_type") or "").strip()
                        has_active = bool(plan_res.get("has_active_subscription"))
                        if plan_type and plan_type != last_plan:
                            last_plan = plan_type
                            _log(f"  套餐状态: {plan_type or 'unknown'} (active={has_active})")
                        if plan_type.lower() == "plus" or has_active:
                            paid_reason = f"套餐已变为 {plan_type}"
                            break
                    except Exception as exc:
                        _log(f"  套餐查询失败: {type(exc).__name__}: {str(exc)[:100]}")

                try:
                    page.wait_for_timeout(2000)
                except Exception:
                    break

            if not paid_reason:
                _log("✗ 等待扫码支付超时")
                return {
                    "ok": False, "status": "timeout", "qr_data": qr_data,
                    "plan_type": last_plan, "error": f"等待扫码支付超时（{timeout_sec}s）",
                }
            _log(f"✓ 支付完成: {paid_reason}")
            return {
                "ok": True, "status": "paid", "qr_data": qr_data,
                "plan_type": last_plan, "error": "", "reason": paid_reason,
            }
    except Exception as exc:
        logger.warning("[Monitor] 监测异常: %s: %s", type(exc).__name__, str(exc)[:200])
        return {
            "ok": False, "status": "error", "qr_data": qr_data,
            "error": f"监测异常: {type(exc).__name__}: {str(exc)[:180]}",
        }
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if relay_listener is not None:
            try:
                relay_listener.close()
            except Exception:
                pass


def _fetch_oaics_amount(env: BrowserSession, token: str, cs_id: str, processor: str,
                        payment_method: str, log_fn=None) -> int:
    """查询 oaics 通道 checkout session 的应付金额（最小单位）。

    GET /backend-api/payments/checkout/{processor}/{cs_id}
    从 checkout_snapshot 提取金额（优先 due/minorUnitsAmount/amount）。
    查询失败返回 0（放行，兼容接口偶发异常；金额校验防的是活动失效场景，
    该场景下查询接口正常工作会返回全价金额）。
    """
    def _log(msg: str) -> None:
        logger.info("[Oaics] %s", msg)
        if log_fn:
            try: log_fn(msg)
            except Exception: pass

    path = f"/backend-api/payments/checkout/{processor}/{cs_id}"
    try:
        headers = _checkout_headers_sentinel(env, token, payment_method, log_fn=_log)
        headers["referer"] = f"https://chatgpt.com/checkout/{processor}/{cs_id}"
        headers["x-openai-target-path"] = path
        headers["x-openai-target-route"] = path
        resp = env.session.get(f"https://chatgpt.com{path}", headers=headers, timeout=CHATGPT_TIMEOUT)
        if int(getattr(resp, "status_code", 0) or 0) >= 400:
            _log(f"  ⚠ 金额查询失败 HTTP {getattr(resp, 'status_code', 0)}，放行")
            return 0
        data = resp.json() or {}
    except Exception as exc:
        _log(f"  ⚠ 金额查询异常（{type(exc).__name__}: {str(exc)[:80]}），放行")
        return 0

    def _scan(obj: Any, depth: int = 0) -> int | None:
        if depth > 6:
            return None
        if isinstance(obj, dict):
            for k, v in obj.items():
                lk = str(k).lower()
                # 金额字段：due / minorUnitsAmount / amount* / total（子串匹配防拼写差异）
                if isinstance(v, (int, float)) and (lk in ("due", "total") or "amount" in lk):
                    return int(v)
                r = _scan(v, depth + 1)
                if r is not None:
                    return r
        elif isinstance(obj, list):
            for item in obj:
                r = _scan(item, depth + 1)
                if r is not None:
                    return r
        return None

    amount = _scan(data)
    if amount is None:
        _log("  ⚠ 未找到金额字段，放行")
        return 0
    return int(amount)


def _run_oaics_flow(env: BrowserSession, token: str, checkout: dict[str, str],
                    payment_method: str, promo_env: BrowserSession | None = None,
                    log_fn=None) -> dict[str, Any]:
    """OpenAI 自营 oaics_ 通道提链流程（GCash）。

    参考 pay.153 流程：
      1. checkout/update（promo 应用，用第二个代理池）
      2. 金额校验（0 元优惠必须生效，防止活动失效时产出全价链接）
      3. checkout/confirm（selected_payment_method_type = cpmt id）
      4. custom_payment_method/start（创建 attempt）
      5. custom_payment_method/continue（带 action_result.redirectResult）
      6. 提取 Adyen checkoutshopper 跳转链接
    """
    cs_id = checkout["cs_id"]
    cpmt_id = checkout.get("custom_payment_method_id") or ""
    processor = checkout.get("processor_entity", "openai_llc")
    checkout_page_url = f"https://chatgpt.com/checkout/{processor}/{cs_id}"

    def _log(msg: str) -> None:
        logger.info("[Oaics] %s", msg)
        if log_fn:
            try: log_fn(msg)
            except Exception: pass

    # 1. checkout/update（promo 应用；promo_env 为第二个代理池）
    upd_env = promo_env or env
    upd_headers = _checkout_headers_sentinel(upd_env, token, payment_method, log_fn=_log)
    upd_headers["referer"] = checkout_page_url
    body_upd = {
        "checkout_session_id": cs_id,
        "processor_entity": processor,
        "plan_name": "chatgptplusplan",
        "price_interval": "month",
        "seat_quantity": 1,
        "promo_campaign": {"promo_campaign_id": PROMO_ID, "is_coupon_from_query_param": False},
    }
    _log("Step A: checkout/update（应用 0 元优惠）...")
    resp = upd_env.post(CHECKOUT_UPDATE_URL, json=body_upd, headers=upd_headers, timeout=CHATGPT_TIMEOUT)
    status = int(getattr(resp, "status_code", 0) or 0)
    if status >= 400:
        raise RuntimeError(f"oaics checkout/update 失败 HTTP {status}: {(getattr(resp, 'text', '') or '')[:300]}")
    _log("  checkout/update 成功")

    # 1.5 金额校验：0 元优惠必须生效，否则拒绝继续（防止活动失效时产出全价链接）
    amount = _fetch_oaics_amount(env, token, cs_id, processor, payment_method, log_fn=_log)
    _log(f"  当前金额(最小单位): {amount}")
    if amount != 0:
        raise RuntimeError(f"0 元优惠未生效，当前金额={amount}（plus-1-month-free 活动可能已失效）")
    _log("  ✓ 金额为 0，继续")

    # 2. checkout/confirm（选中 cpmt 支付方式）
    headers = _checkout_headers_sentinel(env, token, payment_method, log_fn=_log)
    headers["referer"] = checkout_page_url
    body_confirm = {
        "checkout_session_id": cs_id,
        "processor_entity": processor,
        "selected_payment_method_type": cpmt_id,
    }
    _log("Step B: checkout/confirm（确认 GCash 支付方式）...")
    resp = env.post("https://chatgpt.com/backend-api/payments/checkout/confirm", json=body_confirm, headers=headers, timeout=CHATGPT_TIMEOUT)
    status = int(getattr(resp, "status_code", 0) or 0)
    if status >= 400:
        raise RuntimeError(f"oaics confirm 失败 HTTP {status}: {(getattr(resp, 'text', '') or '')[:300]}")
    confirm_data = resp.json() or {}
    _log(f"  confirm 成功: {str(confirm_data.get('status'))}")

    # 3. custom_payment_method/start（创建 attempt；响应里带 next_action.url = Adyen 跳转链接）
    body_start = {
        "checkout_session_id": cs_id,
        "custom_payment_method_id": cpmt_id,
        "payment_method_type": payment_method,
        "custom_payment_method_type_id": cpmt_id,
    }
    _log("Step C: custom_payment_method/start...")
    resp = env.post("https://chatgpt.com/backend-api/payments/checkout/custom_payment_method/start",
                    json=body_start, headers=headers, timeout=CHATGPT_TIMEOUT)
    status = int(getattr(resp, "status_code", 0) or 0)
    if status >= 400:
        raise RuntimeError(f"custom PM start 失败 HTTP {status}: {(getattr(resp, 'text', '') or '')[:300]}")
    start_data = resp.json() or {}
    _log(f"  start 成功: status={start_data.get('status')}")

    # 4. 提取跳转链接（next_action.url，Adyen checkoutshopper）
    redirect_url = ""
    next_action = start_data.get("next_action")
    if isinstance(next_action, dict):
        redirect_url = str(next_action.get("url") or "")
    if not redirect_url:
        for key in ("provider_redirect_url", "redirect_url", "url", "checkout_url", "payment_url", "authorization_url"):
            v = start_data.get(key)
            if isinstance(v, str) and v.startswith("http"):
                redirect_url = v
                break
    if not redirect_url:
        redirect_url = _extract_redirect_url(start_data)
    if redirect_url:
        _log(f"  跳转链接: {redirect_url[:80]}")

    return {
        "ok": True,
        "redirect_url": redirect_url,
        "qr_urls": [],
        "qr_data": "",
        "cs_id": cs_id,
        "processor_entity": processor,
        "error": "",
        "raw": start_data,
    }


def _poll_payment_page(env: BrowserSession, cs_id: str, stripe_pk: str,
                       ctx: dict[str, Any], timeout_sec: int = 45, payment_method: str = "momo") -> tuple[str, list[str]]:
    deadline = time.time() + timeout_sec
    params = {**_elements_session_params(ctx, payment_method), "key": stripe_pk, "_stripe_version": STRIPE_VERSION_FULL}
    url = f"https://api.stripe.com/v1/payment_pages/{cs_id}"
    last_error = ""
    while time.time() < deadline:
        try:
            resp = env.session.get(url, params=params, headers=dict(_STRIPE_HEADERS), timeout=STRIPE_TIMEOUT)
        except Exception as exc:
            last_error = str(exc)
            time.sleep(1)
            continue
        status = int(getattr(resp, "status_code", 0) or 0)
        if status >= 400:
            last_error = f"HTTP {status}"
            time.sleep(1)
            continue
        payload = resp.json() or {}
        redirect_url = _extract_redirect_url(payload)
        qr_urls = _extract_qr_candidates(payload)
        if redirect_url or qr_urls:
            return redirect_url, qr_urls
        submission = _find_submission_attempt(payload)
        if submission.get("state") == "failed":
            # 不立即退出：Stripe approve 为异步处理，submission failed 可能随后
            # 转为 requires_action + redirect_to_url（需用户/钱包授权）。
            # 参考项目仅对顶层 state=failed 退出，嵌套 submission_attempt 的
            # failed 会继续轮询直到 deadline 或出现 redirect URL。
            last_error = f"submission failed: {submission.get('error') or submission}"
            time.sleep(1)
            continue
        last_error = str(submission.get("state") or "waiting")
        time.sleep(1)
    raise RuntimeError(f"redirect url resolution timeout: {last_error}")


def _resolve_external_redirect(env: BrowserSession, start_url: str, payment_method: str = "momo") -> str:
    """跟随跳转直到到达目标支付方式的域名（momo.vn / gcash.com / mynt.com）。"""
    target_hosts = PAYMENT_CONFIGS[payment_method]["target_hosts"]
    current = start_url
    for _ in range(5):
        host = (urlparse(current).netloc or "").lower()
        if any(host.endswith(t) or host == t.lstrip(".") for t in target_hosts):
            return current
        try:
            resp = env.session.get(current, timeout=STRIPE_TIMEOUT, allow_redirects=False)
        except Exception:
            return current
        location = ""
        try:
            location = resp.headers.get("location") or resp.headers.get("Location") or ""
        except Exception:
            pass
        if not location:
            return current
        current = urljoin(current, location)
    return current


# ==================== 主入口 ==================== #
def _extract_payment_link(payment_method: str, access_token: str, proxy: str = "",
                          promo_proxy: str = "", log_fn=None,
                          pick_proxy=None, pick_promo_proxy=None) -> dict:
    """通用提链流程内核（MoMo / GCash / GoPay 共用）。

    payment_method: "momo" / "gcash" / "gopay"
    proxy: 主代理池初始代理（创建 checkout / Stripe init / PM / confirm）
    promo_proxy: 第二个代理池初始代理（仅用于 checkout/update 应用 0 元活动）
    log_fn(message: str): 每步流程的日志回调，供 service 写入日志文件。
    pick_proxy: 重新挑主代理的回调（提链失败重试时换代理）；无则只用初始 proxy。
    pick_promo_proxy: 重新挑 promo 代理的回调。
    返回 {ok, redirect_url, qr_urls, error}。

    checkout 首步（含 sentinel 生成）失败时会换代理重试最多 3 次，
    覆盖代理对 sentinel.openai.com/chatgpt.com 不稳定导致的 curl 97 / 403。
    """
    cfg = PAYMENT_CONFIGS[payment_method]
    label = cfg["label"]
    country = cfg["country"]
    currency = cfg["currency"]

    def _log(msg: str) -> None:
        logger.info("[%sLink] %s", label, msg)
        if log_fn:
            try:
                log_fn(msg)
            except Exception:
                pass

    # checkout 首步重试：sentinel 失败/403/curl97/超时时换代理重建 env
    MAX_CHECKOUT_ATTEMPTS = 3
    # Stripe 直连步骤（init/refresh）重试上限：代理池通常 5-6 个且多数对
    # api.stripe.com 出口不稳（TLS 35），必须能覆盖整个池子（去重后试遍所有代理）
    MAX_STRIPE_ATTEMPTS = 6
    env = None
    promo_env = None
    cur_proxy = proxy or ""
    tried_proxies: set[str] = {cur_proxy}
    checkout = None
    last_err = ""
    for attempt in range(1, MAX_CHECKOUT_ATTEMPTS + 1):
        # 关闭上一轮残留会话
        if env is not None:
            try:
                env.session.close()
            except Exception:
                pass
            env = None
        try:
            env = BrowserSession(proxy=cur_proxy or "", detect_exit_geo=False)
            if attempt == 1:
                _log(f"开始提链，代理={_redact_proxy(cur_proxy)}")
            else:
                _log(f"Step 1 第 {attempt}/{MAX_CHECKOUT_ATTEMPTS} 次尝试，换代理={_redact_proxy(cur_proxy)}")
            billing = _billing_profile(payment_method)
            if attempt == 1:
                _log(f"{label} 账单: {billing['name']} / {billing['city']} / {billing['postal_code']}")
            _log(f"Step 1: 创建 Checkout ({country}/{currency} + plus-1-month-free)...")
            checkout = _create_checkout(env, access_token, payment_method, log_fn=_log)
            break  # 成功
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {str(exc)[:200]}"
            err_low = str(exc).lower()
            # 仅对可重试的代理/风控类错误换代理重试：curl 97 / proxy / 403 / timeout / sentinel 相关
            retryable = any(k in err_low for k in (
                "curl", "proxy", "connection", "timed out", "timeout",
                "403", "forbidden", "sentinel",
            )) and not _is_already_paid(str(exc))
            if attempt < MAX_CHECKOUT_ATTEMPTS and retryable and pick_proxy is not None:
                _log(f"  ✗ Step 1 失败（第 {attempt} 次）: {last_err}")
                # 换一个没试过的代理
                next_proxy = ""
                for _ in range(5):
                    cand = pick_proxy() or ""
                    if cand and cand not in tried_proxies:
                        next_proxy = cand
                        break
                if not next_proxy:
                    # 池里没有新代理了，重试已用过的也优于直接放弃
                    next_proxy = pick_proxy() or cur_proxy
                cur_proxy = next_proxy
                tried_proxies.add(cur_proxy)
                _log(f"  将换代理重试...")
                continue
            # 不可重试或重试用尽
            raise RuntimeError(f"checkout 创建失败（第 {attempt}/{MAX_CHECKOUT_ATTEMPTS} 次）: {last_err}")
    if not checkout:
        return {"ok": False, "redirect_url": "", "qr_urls": [], "error": last_err or "checkout 创建失败"}

    _log(f"  Checkout 创建成功: cs_id={checkout['cs_id'][:20]}... provider={checkout.get('provider', 'stripe')}")
    try:

        # oaics_ 通道（OpenAI 自营/Adyen）：GCash 走 custom_payment_method 流程
        if checkout.get("provider") == "oaics":
            _log("  检测到 oaics_ 通道，走 OpenAI 自营 custom payment 流程...")
            if promo_proxy:
                promo_env = BrowserSession(proxy=promo_proxy, detect_exit_geo=False)
                _log(f"  promo 代理={_redact_proxy(promo_proxy)}")
            oaics_result = _run_oaics_flow(
                env, access_token, checkout, payment_method,
                promo_env=promo_env if promo_proxy else None,
                log_fn=_log,
            )
            if oaics_result.get("redirect_url"):
                _log(f"  跳转链接: {oaics_result['redirect_url'][:80]}")
            return oaics_result

        # Step 2: Stripe init (bootstrap) — 网络失败/支付方式缺失时换代理重试
        # （部分 ID 代理对 api.stripe.com 出口 TLS 不稳：curl 35 WRONG_VERSION_NUMBER；
        #   且 Stripe 按出口 IP 决定支付方式集，换代理后可能不返回目标支付方式）
        target_type = PAYMENT_CONFIGS[payment_method]["stripe_type"]
        init_payload = None
        methods: list[str] = []
        for s2_attempt in range(1, MAX_STRIPE_ATTEMPTS + 1):
            try:
                _log("Step 2: Stripe init (bootstrap)...")
                init_payload = _stripe_init(env, checkout["cs_id"], checkout["stripe_pk"], payment_method)
                methods = _first_payment_method_types(init_payload) or []
                _log(f"  可用支付方式: {methods}")
                if target_type in methods:
                    break  # 目标支付方式可用
                if s2_attempt < MAX_STRIPE_ATTEMPTS and pick_proxy is not None:
                    _log(f"  ⚠ 支付方式不含 {target_type}: {methods}，换代理重试")
                else:
                    raise RuntimeError(
                        f"当前账号支付方式不支持 {label}: {methods}（{s2_attempt} 个代理均未返回 {target_type}，请更换出口代理）")
            except Exception as exc:
                err_low = str(exc or "").lower()
                retryable = any(k in err_low for k in (
                    "curl", "proxy", "connection", "timed out", "timeout",
                    "ssl", "tls", "403", "429", "forbidden",
                ))
                if s2_attempt < MAX_STRIPE_ATTEMPTS and retryable and pick_proxy is not None:
                    _log(f"  ✗ Step 2 失败（第 {s2_attempt} 次）: {type(exc).__name__}: {str(exc)[:120]}，换代理重试")
                else:
                    raise
            # 换主代理重建 env（网络失败或支付方式缺失共用）
            if env is not None:
                try: env.session.close()
                except Exception: pass
                env = None
            next_proxy = ""
            for _ in range(5):
                cand = pick_proxy() or ""
                if cand and cand not in tried_proxies:
                    next_proxy = cand; break
            if not next_proxy:
                next_proxy = pick_proxy() or cur_proxy
            cur_proxy = next_proxy
            tried_proxies.add(cur_proxy)
            env = BrowserSession(proxy=cur_proxy or "", detect_exit_geo=False)

        # Step 3: checkout/update 再次应用 0 元活动（使用第二个代理池）
        # 带换 promo 代理重试（覆盖 sentinel/Cloudflare 间歇 403）
        _log("Step 3: checkout/update (应用 0 元优惠活动, 第二个代理池)...")
        cur_promo_proxy = promo_proxy or ""
        promo_tried: set[str] = {cur_promo_proxy}
        promo_done = False
        for attempt in range(1, MAX_CHECKOUT_ATTEMPTS + 1):
            # 关闭上一轮残留 promo 会话
            if promo_env is not None and attempt > 1:
                try: promo_env.session.close()
                except Exception: pass
                promo_env = None
            try:
                if promo_proxy:
                    if attempt == 1:
                        _log(f"  promo 代理={_redact_proxy(cur_promo_proxy)}")
                    else:
                        _log(f"  Step 3 第 {attempt}/{MAX_CHECKOUT_ATTEMPTS} 次尝试，换 promo 代理={_redact_proxy(cur_promo_proxy)}")
                    promo_env = BrowserSession(proxy=cur_promo_proxy, detect_exit_geo=False)
                _update_checkout_promotion(promo_env or env, access_token, checkout, payment_method, log_fn=_log)
                promo_done = True
                break
            except Exception as exc:
                err_low = str(exc).lower()
                retryable = any(k in err_low for k in (
                    "curl", "proxy", "connection", "timed out", "timeout",
                    "403", "forbidden", "sentinel",
                ))
                if attempt < MAX_CHECKOUT_ATTEMPTS and retryable and pick_promo_proxy is not None:
                    _log(f"  ✗ Step 3 失败（第 {attempt} 次）: {type(exc).__name__}: {str(exc)[:120]}")
                    next_p = ""
                    for _ in range(5):
                        cand = pick_promo_proxy() or ""
                        if cand and cand not in promo_tried:
                            next_p = cand; break
                    if not next_p:
                        next_p = pick_promo_proxy() or cur_promo_proxy
                    cur_promo_proxy = next_p
                    promo_tried.add(cur_promo_proxy)
                    continue
                raise
        if promo_done:
            _log("  checkout/update 成功")

        # Step 4: Stripe init (refresh) — 校验金额为 0（网络失败换代理重试）
        init_payload = None
        for s4_attempt in range(1, MAX_STRIPE_ATTEMPTS + 1):
            try:
                _log("Step 4: Stripe init (refresh) — 校验金额...")
                init_payload = _stripe_init(env, checkout["cs_id"], checkout["stripe_pk"], payment_method)
                break
            except Exception as exc:
                err_low = str(exc or "").lower()
                retryable = any(k in err_low for k in (
                    "curl", "proxy", "connection", "timed out", "timeout",
                    "ssl", "tls", "403", "429", "forbidden",
                ))
                if s4_attempt < MAX_STRIPE_ATTEMPTS and retryable and pick_proxy is not None:
                    _log(f"  ✗ Step 4 失败（第 {s4_attempt} 次）: {type(exc).__name__}: {str(exc)[:120]}，换代理重试")
                    if env is not None:
                        try: env.session.close()
                        except Exception: pass
                        env = None
                    next_proxy = ""
                    for _ in range(5):
                        cand = pick_proxy() or ""
                        if cand and cand not in tried_proxies:
                            next_proxy = cand; break
                    if not next_proxy:
                        next_proxy = pick_proxy() or cur_proxy
                    cur_proxy = next_proxy
                    tried_proxies.add(cur_proxy)
                    env = BrowserSession(proxy=cur_proxy or "", detect_exit_geo=False)
                    continue
                raise
        ctx = _build_ctx(init_payload)
        amount = ctx["checkout_amount"]
        _log(f"  当前金额(最小单位): {amount}")
        if amount != 0:
            _log(f"  ✗ 0 元优惠未生效，金额={amount}")
            return {"ok": False, "redirect_url": "", "qr_urls": [], "error": f"0 元优惠未生效，当前金额={amount}"}
        _log("  ✓ 金额为 0，继续")

        # ===== Step 5-8: PreConfirm → PM → Confirm → approve/poll（换代理重试） =====
        # confirm 阶段出口 IP 参与 Stripe 风控，失败时换主代理重建会话重试（最多 3 次）。
        # 换代理后需重新 init（ctx/checksum 绑定会话）再 confirm。
        redirect_url = ""
        qr_urls: list[str] = []
        confirm_last_err = ""
        for cattempt in range(1, MAX_STRIPE_ATTEMPTS + 1):
            try:
                if cattempt > 1:
                    # 关闭旧会话，换代理重建
                    if env is not None:
                        try: env.session.close()
                        except Exception: pass
                        env = None
                    next_proxy = ""
                    for _ in range(5):
                        cand = pick_proxy() or ""
                        if cand and cand not in tried_proxies:
                            next_proxy = cand; break
                    if not next_proxy:
                        next_proxy = pick_proxy() or cur_proxy
                    cur_proxy = next_proxy
                    tried_proxies.add(cur_proxy)
                    _log(f"  confirm 第 {cattempt}/{MAX_STRIPE_ATTEMPTS} 次尝试，换代理={_redact_proxy(cur_proxy)}")
                    env = BrowserSession(proxy=cur_proxy or "", detect_exit_geo=False)
                    init_payload = _stripe_init(env, checkout["cs_id"], checkout["stripe_pk"], payment_method)
                    ctx = _build_ctx(init_payload)

                # 注：参考项目（GPT-Register-Tool）confirm 前不调 pre_confirm，
                # pre_confirm 会提前创建 submission_attempt、改变 session 状态，
                # 实测对 GoPay 0 元单可能导致后续 confirm/approve 异常，已移除。

                # Step 6: Create PM
                _log(f"Step 6: 创建 {label} PaymentMethod (type={payment_method}, {billing['country']})...")
                pm_id = _stripe_create_pm(env, checkout["cs_id"], checkout["stripe_pk"], billing, payment_method, ctx=ctx)
                _log(f"  PM 创建成功: {pm_id}")

                # Step 7: Confirm (expected_amount=实际金额)
                _log(f"Step 7: Stripe Confirm (expected_amount={amount})...")
                confirm_payload = _stripe_confirm(env, checkout["cs_id"], pm_id, checkout["stripe_pk"], init_payload, ctx, checkout, payment_method)
                submission = _find_submission_attempt(confirm_payload)
                _log(f"  Confirm 完成, submission state={submission.get('state', '?')}")

                # Step 8: 解析 confirm 响应（含二次 confirm 重试）
                # checkout_upcoming_invoice_mismatch 等错误需要重新 init 刷新金额后二次 confirm
                try:
                    redirect_url = _extract_redirect_url(confirm_payload)
                    qr_urls = _extract_qr_candidates(confirm_payload)

                    if not redirect_url and submission.get("state") == "requires_approval":
                        _log("Step 8a: 需要 ChatGPT approve...")
                        _chatgpt_approve(env, access_token, checkout, payment_method, log_fn=_log)
                        _log("  approve 通过")
                        _log("Step 8b: 轮询 payment page 提取跳转 URL (最多 45s)...")
                        redirect_url, poll_qr = _poll_payment_page(env, checkout["cs_id"], checkout["stripe_pk"], ctx, payment_method=payment_method)
                        qr_urls.extend(poll_qr)
                    elif not redirect_url and not qr_urls:
                        _log("Step 8: 轮询 payment page 提取跳转 URL (最多 45s)...")
                        redirect_url, poll_qr = _poll_payment_page(env, checkout["cs_id"], checkout["stripe_pk"], ctx, payment_method=payment_method)
                        qr_urls.extend(poll_qr)
                except Exception as exc:
                    err_text = str(exc or "").lower()
                    if "checkout_upcoming_invoice_mismatch" not in err_text and "redirect url resolution timeout" not in err_text:
                        raise
                    _log(f"  ⚠ {type(exc).__name__}: {str(exc)[:150]}")
                    _log("  重新 init 后二次 Confirm...")
                    init_payload2 = _stripe_init(env, checkout["cs_id"], checkout["stripe_pk"], payment_method)
                    ctx2 = _build_ctx(init_payload2)
                    confirm_payload2 = _stripe_confirm(env, checkout["cs_id"], pm_id, checkout["stripe_pk"], init_payload2, ctx2, checkout, payment_method)
                    _log("  二次 Confirm 完成, 重新解析...")
                    redirect_url = _extract_redirect_url(confirm_payload2)
                    qr_urls = _extract_qr_candidates(confirm_payload2)
                    submission2 = _find_submission_attempt(confirm_payload2)
                    if not redirect_url and submission2.get("state") == "requires_approval":
                        _log("  二次 approve...")
                        _chatgpt_approve(env, access_token, checkout, payment_method, log_fn=_log)
                        redirect_url, poll_qr = _poll_payment_page(env, checkout["cs_id"], checkout["stripe_pk"], ctx2, payment_method=payment_method)
                        qr_urls.extend(poll_qr)
                    elif not redirect_url and not qr_urls:
                        redirect_url, poll_qr = _poll_payment_page(env, checkout["cs_id"], checkout["stripe_pk"], ctx2, payment_method=payment_method)
                        qr_urls.extend(poll_qr)

                if redirect_url:
                    break  # 拿到跳转 URL，退出重试
                confirm_last_err = "未提取到支付跳转 URL"
                _log("  ✗ 本轮 confirm 未拿到跳转 URL，尝试换代理重试...")
            except Exception as exc:
                confirm_last_err = f"{type(exc).__name__}: {str(exc)[:600]}"
                err_low = str(exc or "").lower()
                # 网络/风控类错误：换代理重试（同 cs_id 有效）
                retryable = any(k in err_low for k in (
                    "curl", "proxy", "connection", "timed out", "timeout",
                    "403", "429", "forbidden", "ratelimit", "rate limit",
                    "checkout_upcoming_invoice_mismatch", "redirect url resolution timeout",
                ))
                # 业务类错误：promo/支付已被该 session 消费，同 cs_id 重试无效
                # （如 submission failed / invalid_promotion / approve 业务拒绝），直接失败
                business_failure = any(k in err_low for k in (
                    "submission failed", "invalid_promotion", "approve 未通过",
                    "checkout_approval_payment_failure", "already paid", "already subscribed",
                ))
                if not business_failure and cattempt < MAX_STRIPE_ATTEMPTS and retryable and pick_proxy is not None:
                    _log(f"  ✗ confirm 失败（第 {cattempt} 次）: {confirm_last_err}")
                    continue  # 换代理重试
                if business_failure:
                    _log(f"  ✗ confirm 业务失败（promo/支付已消耗，同 session 无法重试）: {confirm_last_err}")
                raise RuntimeError(f"confirm 失败（第 {cattempt}/{MAX_STRIPE_ATTEMPTS} 次）: {confirm_last_err}")

        if not redirect_url:
            _log("  ✗ 未提取到支付跳转 URL")
            return {"ok": False, "redirect_url": "", "qr_urls": list(dict.fromkeys(qr_urls)),
                    "error": confirm_last_err or "未提取到支付跳转 URL"}

        # Step 9: 跟随 redirect 到最终目标域名
        _log(f"Step 9: 跟随 redirect ({redirect_url[:60]}...)...")
        final_url = _resolve_external_redirect(env, redirect_url, payment_method)
        _log(f"  最终 URL: {final_url[:80]}")

        _log("✓ 提链成功")
        return {"ok": True, "redirect_url": final_url or redirect_url,
                "qr_urls": list(dict.fromkeys(qr_urls)),
                "cs_id": checkout.get("cs_id") or "",
                "processor_entity": checkout.get("processor_entity") or "openai_ie",
                "error": ""}

    except Exception as exc:
        logger.warning("[%sLink] 提链失败: %s: %s", label, type(exc).__name__, str(exc)[:200])
        if log_fn:
            try: log_fn(f"✗ 提链异常: {type(exc).__name__}: {str(exc)[:200]}")
            except Exception: pass
        return {"ok": False, "redirect_url": "", "qr_urls": [],
                "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    finally:
        if env is not None:
            try:
                env.session.close()
            except Exception:
                pass
        if promo_env is not None:
            try:
                promo_env.session.close()
            except Exception:
                pass


def extract_momo_link(access_token: str, proxy: str = "", log_fn=None,
                      pick_proxy=None) -> dict:
    """执行完整 MoMo 提链流程（0 元，单代理池）。

    pick_proxy: 重新挑代理的回调（checkout 首步失败时换代理重试）；无则只用 proxy。
    """
    return _extract_payment_link("momo", access_token, proxy=proxy, log_fn=log_fn,
                                 pick_proxy=pick_proxy)


def extract_gcash_link(access_token: str, proxy: str = "", promo_proxy: str = "", log_fn=None,
                       pick_proxy=None, pick_promo_proxy=None) -> dict:
    """执行完整 GCash 提链流程（0 元，PH/PHP）。

    pick_proxy/pick_promo_proxy: 重新挑代理的回调（checkout 首步失败时换代理重试）。
    """
    return _extract_payment_link("gcash", access_token, proxy=proxy, promo_proxy=promo_proxy,
                                 log_fn=log_fn, pick_proxy=pick_proxy, pick_promo_proxy=pick_promo_proxy)


def extract_gopay_link(access_token: str, proxy: str = "", promo_proxy: str = "", log_fn=None,
                       pick_proxy=None, pick_promo_proxy=None) -> dict:
    """执行完整 GoPay 提链流程（0 元，ID/IDR）。

    与 GCash 同入口（all_plans_pricing_modal + custom），cs_/oaics_ 双通道
    流程通用；最终落地到 app.midtrans.com/snap/... 重定向页。

    pick_proxy/pick_promo_proxy: 重新挑代理的回调（checkout 首步失败时换代理重试）。
    """
    return _extract_payment_link("gopay", access_token, proxy=proxy, promo_proxy=promo_proxy,
                                 log_fn=log_fn, pick_proxy=pick_proxy, pick_promo_proxy=pick_promo_proxy)
