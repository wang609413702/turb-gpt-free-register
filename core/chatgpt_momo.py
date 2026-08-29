# -*- coding: utf-8 -*-
"""ChatGPT 账号 Checkout MoMo 支付支持检测。

只探测一件事：当前账号创建 VN/VND 的 Plus Checkout Session 后，
其支付方式列表里是否包含 momo。Plus 活动/试用资格由「查套餐」功能负责，
这里不重复查询。

探测流程：创建一个未确认的 Checkout Session（country=VN, currency=VND）→
读取其支付方式（OpenAI Checkout 直接取响应，Stripe Session 调 /init）→
判断 "momo" 是否在 payment_method_types 中。从不确认支付或创建 PaymentMethod。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

from core.chatgpt_plan import normalize_token, now_iso, resolve_plan_check_route, token_claims
from core.session import BrowserSession

logger = logging.getLogger(__name__)

CHECKOUT_PATH = "/backend-api/payments/checkout"
CHECKOUT_URL = f"https://chatgpt.com{CHECKOUT_PATH}"
STRIPE_INIT_URL = "https://api.stripe.com/v1/payment_pages/{checkout_id}/init"

# 兜底 Stripe publishable key，当响应里缺失时使用（仅用于读取支付方式，不会发起付款）。
DEFAULT_STRIPE_PK = "pk_live_stripe_publishable_key_placeholder"

# 检测决策码到中文说明的模板映射。{method} 会被替换为实际支付方式名称。
DECISION_TEXT = {
    "available": "当前 Checkout Session 支持 {method} 支付",
    "not_enabled": "当前 Checkout 未返回 {method} 支付方式",
    "already_paid": "账号已订阅，无法创建新订阅 Checkout",
    "credential_invalid": "凭据无效或已过期",
    "risk_blocked": "账号被风控，Checkout 创建被拒绝",
    "checkout_failed": "Checkout 创建失败，结果不确定",
    "stripe_init_failed": "Checkout 已创建，但 Stripe init 失败",
    "payment_methods_unknown": "Checkout 未返回明确的支付方式列表",
    "unexpected_mode": "Checkout 不是 subscription 模式",
}


# --------------------------------------------------------------------------- #
# 纯解析/决策函数
# --------------------------------------------------------------------------- #
def is_user_already_paid_error(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(
        marker in lowered
        for marker in (
            "already has an active subscription",
            "already subscribed",
            "active subscription already exists",
            "user_already_paid",
        )
    )


def is_cloudflare_response(response: Any) -> bool:
    headers = getattr(response, "headers", {}) or {}
    text = str(getattr(response, "text", "") or "").lower()
    status = int(getattr(response, "status_code", 0) or 0)
    server = str(headers.get("server", "")).lower()
    return (
        (status in {403, 429, 503} and ("cloudflare" in server or headers.get("cf-ray")))
        or "just a moment" in text
        or "attention required" in text
    )


def is_risk_blocked_error(text: str) -> bool:
    """OpenAI 风控拒绝 Checkout 创建（HTTP 400 + unusual activity 文案）。

    该标记跟随账号、跨代理持久存在，检测到时立即失败，不再换代理重试。
    """
    lowered = str(text or "").lower()
    return "unusual activity" in lowered or "systems have detected" in lowered


def extract_checkout_error_detail(response: Any) -> str:
    """从 checkout 错误响应里提取服务端 detail 原文，失败时返回空串。"""
    try:
        payload = response.json()
        if isinstance(payload, dict) and payload.get("detail"):
            return str(payload["detail"]).strip()
    except Exception:
        pass
    text = str(getattr(response, "text", "") or "").strip()
    return text[:200]


def classify_checkout_error(response: Any) -> str:
    if is_user_already_paid_error(getattr(response, "text", "")):
        return "already_paid"
    if is_risk_blocked_error(getattr(response, "text", "")):
        return "risk_blocked"
    if is_cloudflare_response(response):
        return "cloudflare"
    status = int(getattr(response, "status_code", 0) or 0)
    if status == 401:
        return "credential_invalid"
    if status == 429:
        return "rate_limited"
    return f"http_{status}"


def checkout_body(country: str = "VN", currency: str = "VND") -> dict[str, Any]:
    # 携带 plus-1-month-free 0 元活动，与真实结账页一致；不确认支付。
    return {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": country, "currency": currency},
        "promo_campaign": {"promo_campaign_id": "plus-1-month-free", "is_coupon_from_query_param": False},
        "checkout_ui_mode": "custom",
    }


def promo_state(data: dict) -> dict:
    """从 checkout 响应提取优惠授予状态。

    OpenAI 对同一账号的每次会话独立决定是否授予 plus-1-month-free 等优惠：
    授予优惠（0 元首月）的会话只配置 card/link，不配置 GCash 等本地支付方式；
    未授予（原价）的会话才会带上 custom_payment_methods。检测结果必须同时
    记录优惠状态，否则 not_enabled 会被误读为"账号不支持该支付方式"。
    """
    promo = data.get("promo_campaign") if isinstance(data, dict) else None
    granted = isinstance(promo, dict) and bool(promo.get("promo_campaign_id"))
    return {
        "promo_granted": granted,
        "promo_campaign_id": promo.get("promo_campaign_id") if granted else None,
    }


def extract_methods(payload: dict[str, Any]) -> tuple[list[str] | None, str | None]:
    methods = payload.get("payment_method_types")
    source = "top_level"
    if not isinstance(methods, list):
        elements = payload.get("elements_options")
        methods = elements.get("payment_method_types") if isinstance(elements, dict) else None
        source = "elements_options"
    if not isinstance(methods, list):
        return None, None
    return sorted({str(method).lower() for method in methods}), source


def extract_custom_methods(payload: dict[str, Any]) -> tuple[list[str] | None, str | None]:
    """提取 OpenAI 自定义结账里的本地支付方式（cpmt_*，如 PH 市场的 GCash）。

    返回 (ids, source)：字段缺失返回 (None, None)；字段存在但为空返回 ([], source)。
    """
    raw = payload.get("custom_payment_methods")
    if not isinstance(raw, list):
        elements = payload.get("elements_options")
        if isinstance(elements, dict):
            raw = elements.get("custom_payment_methods")
    if not isinstance(raw, list):
        return None, None
    ids = [str(item.get("id")) for item in raw if isinstance(item, dict) and item.get("id")]
    return ids, "custom_payment_methods"


def stripe_field(payload: dict[str, Any], key: str) -> Any:
    elements = payload.get("elements_options")
    if isinstance(elements, dict) and key in elements:
        return elements[key]
    return payload.get(key)


def choose_decision(stripe_mode: Any, has_target: bool | None) -> str:
    if stripe_mode != "subscription":
        return "unexpected_mode"
    if has_target is None:
        return "payment_methods_unknown"
    if not has_target:
        return "not_enabled"
    return "available"


def checkout_session_kind(checkout_id: Any, checkout_provider: Any) -> str | None:
    """从 checkout session id / provider 派生会话类型：oaics / cs / None。

    oaics_ 前缀或 checkout_provider=open_ai 视为 OpenAI 自定义结账(oaics)；
    cs_ 前缀视为标准 Stripe Checkout(cs)。供前端支持标签 hover 提示。
    """
    cid = str(checkout_id or "")
    provider = str(checkout_provider or "").strip().lower()
    if cid.startswith("oaics_") or provider == "open_ai":
        return "oaics"
    if cid.startswith("cs_"):
        return "cs"
    return None


# --------------------------------------------------------------------------- #
# 网络探测（使用 BrowserSession）
# --------------------------------------------------------------------------- #
def _momo_settings(timeout: float | None, max_attempts: int | None, retry_delay: float | None) -> tuple[float, int, float]:
    from config import proxy as proxy_cfg

    timeout_value = timeout if timeout is not None else getattr(proxy_cfg, "MOMO_CHECK_TIMEOUT", 12.0)
    attempts_value = max_attempts if max_attempts is not None else getattr(proxy_cfg, "MOMO_CHECK_MAX_ATTEMPTS", 3)
    delay_value = retry_delay if retry_delay is not None else getattr(proxy_cfg, "MOMO_CHECK_RETRY_DELAY", 1.0)
    return (
        max(1.0, min(60.0, float(timeout_value or 12.0))),
        max(1, min(6, int(attempts_value or 3))),
        max(0.0, min(30.0, float(delay_value or 0.0))),
    )


def _checkout_headers(env: BrowserSession, token: str) -> dict[str, str]:
    headers = env._get_common_headers()
    headers.update({
        "accept": "*/*",
        "authorization": f"Bearer {normalize_token(token)}",
        "content-type": "application/json",
        "oai-device-id": env.device_id,
        "oai-language": env.navigator_language(),
        "origin": "https://chatgpt.com",
        "referer": "https://chatgpt.com/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        # x-openai-target-* 由 BrowserSession.get/post 自动补齐（基于 chatgpt.com path）。
    })
    return headers


def _stripe_init(env: BrowserSession, checkout_id: str, stripe_key: str) -> dict[str, Any]:
    """调用 Stripe /init 读取 Checkout Session 的支付方式。不会创建 PaymentMethod。"""
    response = env.session.post(
        STRIPE_INIT_URL.format(checkout_id=checkout_id),
        data={"key": stripe_key, "browser_locale": "en-US"},
        headers={
            "accept": "application/json",
            "origin": "https://checkout.stripe.com",
            "referer": f"https://checkout.stripe.com/c/pay/{checkout_id}",
        },
        timeout=env.session.timeout,
    )
    status = int(getattr(response, "status_code", 0) or 0)
    if status != 200:
        error_code = "unknown"
        try:
            error_code = str((response.json().get("error") or {}).get("code") or "unknown")
        except Exception:
            pass
        raise RuntimeError(f"Stripe init HTTP {status} ({error_code})")
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Stripe init 返回的不是 JSON 对象")
    return payload


def _result_ok_or_fail(
    *,
    ok: bool,
    decision: str,
    decision_text: str,
    error: str | None = None,
    http_status: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": ok,
        "checked_at": now_iso(),
        "decision": decision,
        "decision_text": decision_text,
    }
    if error:
        result["error"] = error
    if http_status is not None:
        result["http_status"] = http_status
    if extra:
        result.update(extra)
    return result


def _run_probe(env: BrowserSession, token: str, timeout_seconds: float,
               payment_method: str = "momo", country: str = "VN", currency: str = "VND",
               label: str = "MoMo", custom_method_ids: list[str] | None = None,
               sentinel_headers: dict[str, str] | None = None) -> dict[str, Any]:
    """对单个已建好的 BrowserSession 执行探测，返回富结果 dict。

    payment_method: 要检测的支付方式标识(小写)，如 momo / gcash。
    country/currency: Checkout 账单地区/货币。
    label: 日志和文案里显示的名称。
    custom_method_ids: 不为 None 时，把响应中 custom_payment_methods（cpmt_*）的出现
                       视为目标支付方式可用（OpenAI 自定义结账里 GCash 等本地方式只出现在
                       这里，payment_method_types 恒为 card/link）。空列表 = 出现即算；
                       非空列表 = 只有命中白名单的 cpmt_ id 才算。
    sentinel_headers: OpenAI Sentinel 令牌头。无令牌的 checkout 请求会被风控按
                      "unusual activity" 拦截（HTTP 400）；生成失败传 None 降级请求。
    """
    method_label = label or payment_method
    # 创建未确认的 Checkout Session
    headers = _checkout_headers(env, token)
    if sentinel_headers:
        headers.update({str(k): str(v) for k, v in sentinel_headers.items() if v})
    response = env.post(
        CHECKOUT_URL,
        json=checkout_body(country, currency),
        headers=headers,
        timeout=timeout_seconds,
    )
    status = int(getattr(response, "status_code", 0) or 0)
    if status >= 400:
        failure = classify_checkout_error(response)
        if failure == "already_paid":
            return _result_ok_or_fail(ok=True, decision="already_paid",
                                       decision_text=DECISION_TEXT["already_paid"])
        if failure == "credential_invalid":
            return _result_ok_or_fail(ok=False, decision="credential_invalid",
                                       decision_text=DECISION_TEXT["credential_invalid"],
                                       error=DECISION_TEXT["credential_invalid"], http_status=status)
        if failure == "risk_blocked":
            # 风控跟随账号、换代理重试无意义：直接标记失败，error 保留服务端原文，
            # 前端悬停 gcash_check_error 即可看到 "Our systems have detected unusual activity..."。
            detail = extract_checkout_error_detail(response) or "Our systems have detected unusual activity. Please try again later."
            return _result_ok_or_fail(ok=False, decision="risk_blocked",
                                       decision_text=DECISION_TEXT["risk_blocked"],
                                       error=detail, http_status=status)
        return _result_ok_or_fail(ok=False, decision="checkout_failed",
                                   decision_text=DECISION_TEXT["checkout_failed"],
                                   error=f"checkout 创建失败 ({failure})", http_status=status)

    data = response.json() or {}
    if not isinstance(data, dict):
        return _result_ok_or_fail(ok=False, decision="checkout_failed",
                                   decision_text=DECISION_TEXT["checkout_failed"],
                                   error="checkout 响应不是 JSON 对象")
    promo = promo_state(data)

    checkout_id = data.get("checkout_session_id") or data.get("session_id") or data.get("id")
    checkout_provider = str(data.get("checkout_provider") or "").strip().lower()
    if str(checkout_id or "").startswith("cs_"):
        checkout_provider = "stripe"
    is_openai_checkout = checkout_provider == "open_ai" and bool(checkout_id)
    if not (str(checkout_id or "").startswith("cs_") or is_openai_checkout):
        return _result_ok_or_fail(ok=False, decision="checkout_failed",
                                   decision_text=DECISION_TEXT["checkout_failed"],
                                   error="checkout 未返回有效 session id")

    # 读取支付方式：OpenAI Checkout 直接用响应；Stripe Session 调 /init。
    if checkout_provider == "open_ai":
        init_payload: dict[str, Any] | None = data
    else:
        raw_key = (
            data.get("stripe_publishable_key") or data.get("publishable_key")
            or data.get("publishableKey") or data.get("stripePublishableKey")
            or data.get("key") or ""
        )
        key_match = re.search(r"pk_live_[A-Za-z0-9]+", str(raw_key))
        stripe_key = key_match.group(0) if key_match else DEFAULT_STRIPE_PK
        try:
            init_payload = _stripe_init(env, str(checkout_id), stripe_key)
        except Exception as exc:
            logger.debug("[%s] Stripe init 失败: %s: %s", method_label, type(exc).__name__, exc)
            return _result_ok_or_fail(ok=False, decision="stripe_init_failed",
                                       decision_text=DECISION_TEXT["stripe_init_failed"],
                                       error=f"Stripe init 失败: {type(exc).__name__}: {str(exc)[:160]}")

    methods, methods_source = extract_methods(init_payload)
    custom_ids, custom_source = extract_custom_methods(init_payload)
    has_target = None if methods is None else payment_method in methods
    if custom_ids is not None and custom_ids and custom_method_ids is not None:
        # OpenAI 自定义结账（PH/PHP 的 oaics_ 会话）：GCash 等本地支付方式只出现在
        # custom_payment_methods(cpmt_*)，payment_method_types 恒为 card/link；
        # 只读 payment_method_types 会把支持 GCash 的账号误判为 not_enabled。
        custom_hit = (not custom_method_ids) or any(cid in custom_method_ids for cid in custom_ids)
        if custom_hit:
            has_target = True
    if custom_ids:
        # 展示时把自定义支付方式合入 methods，便于前端/日志看到本地支付方式的来源。
        merged = sorted({str(m).lower() for m in (methods or [])} | {f"custom:{cid}" for cid in custom_ids})
        if methods is not None or custom_source:
            methods, methods_source = merged, methods_source or custom_source
    stripe_mode = stripe_field(init_payload, "mode")
    if checkout_provider == "open_ai" and data.get("plan_name") == "chatgptplusplan":
        stripe_mode = "subscription"

    decision = choose_decision(stripe_mode, has_target)
    supported = (
        True if decision == "available"
        else False if decision == "not_enabled"
        else None
    )
    decision_text = DECISION_TEXT.get(decision, decision).format(method=method_label)
    if decision == "not_enabled" and promo["promo_granted"]:
        decision_text += "；本次会话被授予 0 元试用优惠，优惠会话不配置本地支付方式"
    return _result_ok_or_fail(
        ok=True,
        decision=decision,
        decision_text=decision_text,
        extra={
            "has_target": has_target,
            "supported": supported,
            "conclusive": supported is not None,
            "methods": methods,
            "methods_source": methods_source,
            "custom_methods": custom_ids,
            "stripe_mode": stripe_mode,
            "checkout_provider": checkout_provider or None,
            "checkout_id": str(checkout_id) if checkout_id else None,
            "session_kind": checkout_session_kind(checkout_id, checkout_provider),
            "promo_granted": promo["promo_granted"],
            "promo_campaign_id": promo["promo_campaign_id"],
        },
    )


def _check_payment_support(
    token: str,
    *,
    payment_method: str,
    country: str,
    currency: str,
    label: str,
    proxy: Optional[str] = None,
    timeout: float | None = None,
    max_attempts: int | None = None,
    retry_delay: float | None = None,
    custom_method_ids: list[str] | None = None,
) -> dict:
    """通用支付方式检测内核。返回结构对齐 check_account_plan。"""
    token = normalize_token(token)
    if not token:
        return {"ok": False, "checked_at": now_iso(), "error": "token 为空"}
    claims = token_claims(token)
    if claims.get("token_expired") is True:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "http_status": None,
            "error": "token 已过期",
            "decision": "credential_invalid",
            "decision_text": DECISION_TEXT["credential_invalid"],
            **{k: v for k, v in claims.items() if k != "payload"},
        }

    try:
        timeout_seconds, attempts, base_delay = _momo_settings(timeout, max_attempts, retry_delay)
    except Exception as exc:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "http_status": None,
            "error": f"{label} 检测重试配置错误: {exc}",
            **{k: v for k, v in claims.items() if k != "payload"},
        }

    # 网络类失败可重试；明确的业务决策结果直接返回。
    _RETRYABLE_DECISIONS = {"checkout_failed", "stripe_init_failed", "payment_methods_unknown"}

    try:
        from config import proxy as _pc
        sentinel_enabled = bool(getattr(_pc, "CHECKOUT_SENTINEL_ENABLED", True))
    except Exception:
        sentinel_enabled = True

    last_result: dict | None = None
    for attempt in range(1, attempts + 1):
        env = None
        try:
            # 每次尝试（含重试）都重新解析路由；代理池会随机轮换出口，
            # 避免同一个坏代理在重试时反复失败。
            route = resolve_plan_check_route(proxy)
            route_meta = {k: v for k, v in route.items() if k != "proxy"}
            env = BrowserSession(proxy=route["proxy"], detect_exit_geo=False)
            # Sentinel 令牌按次生成（challenge 单次有效），设备 ID 与请求头/cookie 一致；
            # 生成失败自动降级为无令牌请求，由 risk_blocked 分类兜底提示。
            sentinel_headers = None
            if sentinel_enabled:
                try:
                    from core.checkout_sentinel import generate_checkout_sentinel_headers
                    sentinel_headers = generate_checkout_sentinel_headers(
                        route["proxy"], env.device_id, env.device_id,
                    )
                except Exception as exc:
                    logger.warning("[%s] Sentinel 令牌生成异常，本次降级: %s: %s",
                                   label, type(exc).__name__, str(exc)[:160])
            result = _run_probe(env, token, timeout_seconds,
                                payment_method=payment_method, country=country,
                                currency=currency, label=label,
                                custom_method_ids=custom_method_ids,
                                sentinel_headers=sentinel_headers)
            result["attempt_count"] = attempt
            result["max_attempts"] = attempts
            result["request_timeout"] = timeout_seconds
            result["sentinel_attached"] = bool(sentinel_headers)
            result.update(route_meta)
            retryable = result.get("decision") in _RETRYABLE_DECISIONS
            if attempt >= attempts:
                result["retryable"] = False
            else:
                result["retryable"] = retryable
            if not retryable or attempt >= attempts:
                return result
            logger.info(
                "[%s] 第 %s/%s 次尝试失败，将换代理重试: decision=%s",
                label, attempt, attempts, result.get("decision"),
            )
        except Exception as exc:
            logger.debug("[%s] 探测异常: %s: %s", label, type(exc).__name__, exc, exc_info=True)
            last_result = {
                "ok": False,
                "checked_at": now_iso(),
                "http_status": None,
                "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                "decision": "checkout_failed",
                "decision_text": DECISION_TEXT["checkout_failed"],
                "retryable": attempt < attempts,
            }
            if attempt >= attempts:
                last_result["attempt_count"] = attempt
                last_result["max_attempts"] = attempts
                return last_result
        finally:
            if env is not None:
                try:
                    env.session.close()
                except Exception:
                    pass

        last_result = last_result or {
            "ok": False,
            "checked_at": now_iso(),
            "error": "未知错误",
            "decision": "checkout_failed",
            "decision_text": DECISION_TEXT["checkout_failed"],
            "retryable": True,
        }
        last_result.update({
            "attempt_count": attempt,
            "max_attempts": attempts,
            "request_timeout": timeout_seconds,
        })
        if attempt >= attempts:
            return last_result
        wait_seconds = max(0.0, min(30.0, base_delay * attempt))
        if wait_seconds > 0:
            time.sleep(wait_seconds)

    return last_result or {
        "ok": False,
        "checked_at": now_iso(),
        "http_status": None,
        "error": f"{label} 检测未执行",
        "decision": "checkout_failed",
        "decision_text": DECISION_TEXT["checkout_failed"],
    }


def check_account_momo(
    token: str,
    *,
    proxy: Optional[str] = None,
    timeout: float | None = None,
    max_attempts: int | None = None,
    retry_delay: float | None = None,
) -> dict:
    """检测账号 Checkout 是否支持 MoMo 支付（VN/VND）。"""
    return _check_payment_support(
        token, payment_method="momo", country="VN", currency="VND", label="MoMo",
        proxy=proxy, timeout=timeout, max_attempts=max_attempts, retry_delay=retry_delay,
    )


def check_account_gcash(
    token: str,
    *,
    proxy: Optional[str] = None,
    timeout: float | None = None,
    max_attempts: int | None = None,
    retry_delay: float | None = None,
) -> dict:
    """检测账号 Checkout 是否支持 GCash 支付（PH/PHP）。

    PH/PHP 返回 OpenAI 自定义结账（oaics_），GCash 只出现在 custom_payment_methods
    （cpmt_*），payment_method_types 恒为 card/link；这里把自定义支付方式视为 GCash
    信号，白名单 GCASH_CUSTOM_PAYMENT_METHOD_IDS 留空 = 出现即算。
    """
    from config import proxy as proxy_cfg

    custom_ids = list(getattr(proxy_cfg, "GCASH_CUSTOM_PAYMENT_METHOD_IDS", []) or [])
    return _check_payment_support(
        token, payment_method="gcash", country="PH", currency="PHP", label="GCash",
        proxy=proxy, timeout=timeout, max_attempts=max_attempts, retry_delay=retry_delay,
        custom_method_ids=custom_ids,
    )


def check_account_kakao(
    token: str,
    *,
    proxy: Optional[str] = None,
    timeout: float | None = None,
    max_attempts: int | None = None,
    retry_delay: float | None = None,
) -> dict:
    """检测账号 Checkout 是否支持 Kakao Pay 支付（KR/KRW）。

    KR/KRW 通常返回标准 Stripe Checkout（cs_），kakao_pay 出现在 payment_method_types。
    """
    return _check_payment_support(
        token, payment_method="kakao_pay", country="KR", currency="KRW", label="Kakao",
        proxy=proxy, timeout=timeout, max_attempts=max_attempts, retry_delay=retry_delay,
    )


def check_account_gopay(
    token: str,
    *,
    proxy: Optional[str] = None,
    timeout: float | None = None,
    max_attempts: int | None = None,
    retry_delay: float | None = None,
) -> dict:
    """检测账号 Checkout 是否支持 GoPay 支付（ID/IDR）。

    ID/IDR 返回 OpenAI 自定义结账（oaics_），GoPay 只出现在 custom_payment_methods
    （cpmt_*），payment_method_types 恒为 card/link；这里把自定义支付方式视为 GoPay
    信号，白名单 GOPAY_CUSTOM_PAYMENT_METHOD_IDS 留空 = 出现即算。
    """
    from config import proxy as proxy_cfg

    custom_ids = list(getattr(proxy_cfg, "GOPAY_CUSTOM_PAYMENT_METHOD_IDS", []) or [])
    return _check_payment_support(
        token, payment_method="gopay", country="ID", currency="IDR", label="GoPay",
        proxy=proxy, timeout=timeout, max_attempts=max_attempts, retry_delay=retry_delay,
        custom_method_ids=custom_ids,
    )


# PayPal 检测地区 → Checkout 账单国家/货币。代理出口需与地区一致。
PAYPAL_CHECK_REGIONS: dict[str, tuple[str, str]] = {
    "br": ("BR", "BRL"),
    "th": ("TH", "THB"),
    "de": ("DE", "EUR"),
}


def check_account_paypal(
    token: str,
    *,
    region: str = "br",
    proxy: Optional[str] = None,
    timeout: float | None = None,
    max_attempts: int | None = None,
    retry_delay: float | None = None,
) -> dict:
    """检测账号 Checkout 是否支持 PayPal 支付（按地区：br=BR/BRL，th=TH/THB，de=DE/EUR）。

    BR/TH 通常返回标准 Stripe Checkout（cs_），paypal 出现在 payment_method_types；
    代理需走对应地区的 PayPal 检测代理池。
    """
    key = str(region or "br").strip().lower()
    if key not in PAYPAL_CHECK_REGIONS:
        key = "br"
    country, currency = PAYPAL_CHECK_REGIONS[key]
    result = _check_payment_support(
        token, payment_method="paypal", country=country, currency=currency, label="PayPal",
        proxy=proxy, timeout=timeout, max_attempts=max_attempts, retry_delay=retry_delay,
    )
    result["region"] = key
    return result


def check_account_ideal(
    token: str,
    *,
    proxy: Optional[str] = None,
    timeout: float | None = None,
    max_attempts: int | None = None,
    retry_delay: float | None = None,
) -> dict:
    """检测账号 Checkout 是否支持 IDEAL 支付（NL/EUR）。

    NL/EUR 通常返回标准 Stripe Checkout（cs_），ideal 出现在 payment_method_types。
    """
    return _check_payment_support(
        token, payment_method="ideal", country="NL", currency="EUR", label="IDEAL",
        proxy=proxy, timeout=timeout, max_attempts=max_attempts, retry_delay=retry_delay,
    )
