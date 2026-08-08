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

# 检测决策码到中文说明的映射。
DECISION_TEXT = {
    "momo_available": "当前 Checkout Session 支持 MoMo 支付",
    "momo_not_enabled": "当前 Checkout 未返回 MoMo 支付方式",
    "already_paid": "账号已订阅，无法创建新订阅 Checkout",
    "credential_invalid": "凭据无效或已过期",
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


def classify_checkout_error(response: Any) -> str:
    if is_user_already_paid_error(getattr(response, "text", "")):
        return "already_paid"
    if is_cloudflare_response(response):
        return "cloudflare"
    status = int(getattr(response, "status_code", 0) or 0)
    if status == 401:
        return "credential_invalid"
    if status == 429:
        return "rate_limited"
    return f"http_{status}"


def checkout_body() -> dict[str, Any]:
    # 只关心支付方式是否含 MoMo；不附带任何活动/试用信息，不确认支付。
    return {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "price_interval": "month",
        "seat_quantity": 1,
        "billing_details": {"country": "VN", "currency": "VND"},
        "checkout_ui_mode": "custom",
        "one_click_trial": False,
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


def stripe_field(payload: dict[str, Any], key: str) -> Any:
    elements = payload.get("elements_options")
    if isinstance(elements, dict) and key in elements:
        return elements[key]
    return payload.get(key)


def choose_decision(stripe_mode: Any, has_momo: bool | None) -> str:
    if stripe_mode != "subscription":
        return "unexpected_mode"
    if has_momo is None:
        return "payment_methods_unknown"
    if not has_momo:
        return "momo_not_enabled"
    return "momo_available"


# --------------------------------------------------------------------------- #
# 网络探测（使用 BrowserSession）
# --------------------------------------------------------------------------- #
def _momo_settings(timeout: float | None, max_attempts: int | None, retry_delay: float | None) -> tuple[float, int, float]:
    from config import proxy as proxy_cfg

    timeout_value = timeout if timeout is not None else getattr(proxy_cfg, "MOMO_CHECK_TIMEOUT", 20.0)
    attempts_value = max_attempts if max_attempts is not None else getattr(proxy_cfg, "MOMO_CHECK_MAX_ATTEMPTS", 3)
    delay_value = retry_delay if retry_delay is not None else getattr(proxy_cfg, "MOMO_CHECK_RETRY_DELAY", 1.5)
    return (
        max(1.0, min(60.0, float(timeout_value or 20.0))),
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


def _run_probe(env: BrowserSession, token: str, timeout_seconds: float) -> dict[str, Any]:
    """对单个已建好的 BrowserSession 执行探测，返回富结果 dict。"""
    # 创建未确认的 Checkout Session（VN/VND）
    response = env.post(
        CHECKOUT_URL,
        json=checkout_body(),
        headers=_checkout_headers(env, token),
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
        return _result_ok_or_fail(ok=False, decision="checkout_failed",
                                   decision_text=DECISION_TEXT["checkout_failed"],
                                   error=f"checkout 创建失败 ({failure})", http_status=status)

    data = response.json() or {}
    if not isinstance(data, dict):
        return _result_ok_or_fail(ok=False, decision="checkout_failed",
                                   decision_text=DECISION_TEXT["checkout_failed"],
                                   error="checkout 响应不是 JSON 对象")

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
            logger.debug("[MoMo] Stripe init 失败: %s: %s", type(exc).__name__, exc)
            return _result_ok_or_fail(ok=False, decision="stripe_init_failed",
                                       decision_text=DECISION_TEXT["stripe_init_failed"],
                                       error=f"Stripe init 失败: {type(exc).__name__}: {str(exc)[:160]}")

    methods, methods_source = extract_methods(init_payload)
    has_momo = None if methods is None else "momo" in methods
    stripe_mode = stripe_field(init_payload, "mode")
    if checkout_provider == "open_ai" and data.get("plan_name") == "chatgptplusplan":
        stripe_mode = "subscription"

    decision = choose_decision(stripe_mode, has_momo)
    supported = (
        True if decision == "momo_available"
        else False if decision == "momo_not_enabled"
        else None
    )
    return _result_ok_or_fail(
        ok=True,
        decision=decision,
        decision_text=DECISION_TEXT.get(decision, decision),
        extra={
            "has_momo": has_momo,
            "supported": supported,
            "momo_conclusive": supported is not None,
            "methods": methods,
            "methods_source": methods_source,
            "stripe_mode": stripe_mode,
            "checkout_provider": checkout_provider or None,
            "checkout_id": str(checkout_id) if checkout_id else None,
        },
    )


def check_account_momo(
    token: str,
    *,
    proxy: Optional[str] = None,
    timeout: float | None = None,
    max_attempts: int | None = None,
    retry_delay: float | None = None,
) -> dict:
    """检测账号 Checkout 是否支持 MoMo 支付。返回结构对齐 check_account_plan。"""
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
            "error": f"MoMo 检测重试配置错误: {exc}",
            **{k: v for k, v in claims.items() if k != "payload"},
        }

    # 网络类失败可重试；明确的业务决策结果直接返回。
    # Stripe TLS/SSL、Cloudflare、Checkout 创建失败、支付方式未知 都属于可重试范畴。
    _RETRYABLE_DECISIONS = {"checkout_failed", "stripe_init_failed", "payment_methods_unknown"}

    last_result: dict | None = None
    for attempt in range(1, attempts + 1):
        env = None
        try:
            # 每次尝试（含重试）都重新解析路由；MoMo 池会随机轮换出口，
            # 避免同一个坏代理在重试时反复失败。
            route = resolve_plan_check_route(proxy)
            route_meta = {k: v for k, v in route.items() if k != "proxy"}
            # 不探测出口地理，避免额外网络请求；VN/VND 固定在请求体里指定。
            env = BrowserSession(proxy=route["proxy"], detect_exit_geo=False)
            result = _run_probe(env, token, timeout_seconds)
            result["attempt_count"] = attempt
            result["max_attempts"] = attempts
            result["request_timeout"] = timeout_seconds
            result.update(route_meta)
            retryable = result.get("decision") in _RETRYABLE_DECISIONS
            # 用尽重试次数后，标记为不可重试，避免前端/上层误判。
            if attempt >= attempts:
                result["retryable"] = False
            else:
                result["retryable"] = retryable
            # 明确结论（非可重试）或用尽次数则返回；可重试则换代理再来。
            if not retryable or attempt >= attempts:
                return result
            logger.info(
                "[MoMo] 第 %s/%s 次尝试失败，将换代理重试: decision=%s",
                attempt, attempts, result.get("decision"),
            )
        except Exception as exc:
            logger.debug("[MoMo] 探测异常: %s: %s", type(exc).__name__, exc, exc_info=True)
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
        "error": "MoMo 检测未执行",
        "decision": "checkout_failed",
        "decision_text": DECISION_TEXT["checkout_failed"],
    }
