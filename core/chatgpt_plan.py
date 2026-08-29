# -*- coding: utf-8 -*-
"""ChatGPT 账号套餐/试用资格查询。"""
from __future__ import annotations

import base64
import ipaddress
import json
import logging
import socket
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote, unquote, urlparse

from config.trial import TRIAL_PROXY_POOL_NAMES, TRIAL_REGIONS, trial_timezone_offset_min
from core.session import BrowserSession

logger = logging.getLogger(__name__)

ACCOUNTS_CHECK_PATH = "/backend-api/accounts/check/v4-2023-04-27"
ACCOUNTS_CHECK_ROUTE = "/backend-api/accounts/check/{version}"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_token(token: str) -> str:
    token = (token or "").strip().strip('"').strip("'")
    if token.lower().startswith("authorization:"):
        token = token.split(":", 1)[1].strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def _mask_proxy(proxy: str) -> str:
    """返回可用于日志/API 结果的代理摘要，不泄露用户名和密码。"""
    value = str(proxy or "").strip()
    if not value:
        return ""
    try:
        parsed = urlparse(value if "://" in value else f"//{value}")
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        scheme = f"{parsed.scheme}://" if parsed.scheme else ""
        auth = "***:***@" if parsed.username or parsed.password else ""
        return f"{scheme}{auth}{host}{port}" or "***"
    except Exception:
        return "***"


def _local_proxy_status(proxy: str) -> tuple[bool, bool, str | None]:
    """检查回环代理端口；非本地代理不做预探测，避免额外网络请求。"""
    value = str(proxy or "").strip()
    if not value:
        return False, False, None
    try:
        parsed = urlparse(value if "://" in value else f"//{value}")
        host = parsed.hostname or ""
        is_loopback = host.lower() == "localhost"
        if not is_loopback:
            try:
                is_loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                is_loopback = False
        if not is_loopback:
            return False, True, None
        if not parsed.port:
            return True, False, "本地代理未配置端口"
        try:
            with socket.create_connection((host, parsed.port), timeout=0.5):
                return True, True, None
        except OSError as exc:
            return True, False, f"本地代理 {host}:{parsed.port} 未监听（{type(exc).__name__}）"
    except Exception as exc:
        return False, False, f"代理地址解析失败（{type(exc).__name__}）"


def proxy_username(proxy: Optional[str]) -> str:
    """提取代理的用户名（不含密码），用于检测结果对账用的是哪条线路。"""
    value = str(proxy or "").strip()
    if not value:
        return ""
    try:
        parsed = urlparse(value if "://" in value else f"//{value}")
        username = unquote(parsed.username or "")
        if username:
            return username
    except Exception:
        pass
    # 兼容历史裸 host:port:user:pass 格式
    if "://" not in value:
        parts = value.split(":")
        if len(parts) >= 4 and parts[2]:
            return parts[2]
    return ""


def resolve_plan_check_route(explicit_proxy: Optional[str] = None) -> dict:
    """解析套餐查询的实际网络路径。

    explicit_proxy 不是 None 时表示 API 调用方明确覆盖配置；空字符串代表直连。
    """
    if explicit_proxy is not None:
        selected = str(explicit_proxy or "").strip()
        return {
            "proxy": selected,
            "proxy_mode": "request",
            "network_route": "proxy" if selected else "direct",
            "proxy_used": _mask_proxy(selected) or None,
            "proxy_username": proxy_username(selected) or None,
            "proxy_fallback_reason": None,
        }

    from config import proxy as proxy_cfg

    mode = str(getattr(proxy_cfg, "PLAN_CHECK_PROXY_MODE", "auto") or "auto").strip().lower()
    if mode not in {"auto", "proxy", "direct"}:
        raise ValueError(f"PLAN_CHECK_PROXY_MODE={mode!r} 无效，可选 auto / proxy / direct")
    if mode == "direct":
        return {
            "proxy": "",
            "proxy_mode": mode,
            "network_route": "direct",
            "proxy_used": None,
            "proxy_username": None,
            "proxy_fallback_reason": None,
        }

    selected = str(getattr(proxy_cfg, "PLAN_CHECK_PROXY", "") or "").strip()
    if not selected:
        selected = str(proxy_cfg.pick_proxy() or "").strip()
    # 兜底归一化：兼容 .env 里历史保存的裸 host:port:user:pass 格式
    # （配置页保存时已归一化，这里对已有数据再做一次，幂等）。
    if selected:
        try:
            from config.proxy import normalize_proxy
            selected = normalize_proxy(selected)
        except Exception:
            pass
    if not selected:
        if mode == "proxy":
            raise ValueError("套餐查询网络模式为 proxy，但未配置 PLAN_CHECK_PROXY 或 PROXY_POOL")
        return {
            "proxy": "",
            "proxy_mode": mode,
            "network_route": "direct",
            "proxy_used": None,
            "proxy_username": None,
            "proxy_fallback_reason": "未配置套餐查询代理或代理池",
        }

    is_local, available, reason = _local_proxy_status(selected)
    if mode == "auto" and is_local and not available:
        return {
            "proxy": "",
            "proxy_mode": mode,
            "network_route": "direct_fallback",
            "proxy_used": _mask_proxy(selected),
            "proxy_username": None,
            "proxy_fallback_reason": reason,
        }
    return {
        "proxy": selected,
        "proxy_mode": mode,
        "network_route": "proxy",
        "proxy_used": _mask_proxy(selected),
        "proxy_username": proxy_username(selected) or None,
        "proxy_fallback_reason": None,
    }


def decode_jwt_payload_unverified(token: str) -> dict:
    """仅本地解析 JWT payload，不校验签名。"""
    token = normalize_token(token)
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return {}


def token_claims(token: str) -> dict:
    payload = decode_jwt_payload_unverified(token)
    auth = payload.get("https://api.openai.com/auth") or {}
    profile = payload.get("https://api.openai.com/profile") or {}
    exp = payload.get("exp")
    exp_iso = None
    expired = None
    if isinstance(exp, (int, float)):
        exp_iso = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        expired = datetime.now(tz=timezone.utc).timestamp() >= float(exp)
    return {
        "payload": payload,
        "email": profile.get("email"),
        "user_name": profile.get("name"),
        "user_id": auth.get("chatgpt_user_id") or auth.get("user_id"),
        "account_id": auth.get("chatgpt_account_id"),
        "claim_plan_type": auth.get("chatgpt_plan_type"),
        "exp": exp,
        "token_expires_at": exp_iso,
        "token_expired": expired,
    }


def _common_headers(env: BrowserSession, token: str, claims: dict | None = None) -> dict[str, str]:
    headers = env.get_chatgpt_headers(referer="https://chatgpt.com/")
    headers.update({
        "accept": "application/json",
        "authorization": f"Bearer {normalize_token(token)}",
        "referer": "https://chatgpt.com/",
        "x-openai-target-path": ACCOUNTS_CHECK_PATH,
        "x-openai-target-route": ACCOUNTS_CHECK_ROUTE,
    })
    account_id = str((claims or {}).get("account_id") or "").strip()
    if account_id:
        headers["chatgpt-account-id"] = account_id
    return headers


def _browser_plan_request_result(raw: Any, token: str, *, timezone_offset_min: str | None = None) -> dict:
    if not isinstance(raw, dict):
        return {
            "ok": False,
            "checked_at": now_iso(),
            "error": "浏览器套餐查询未返回有效响应",
            "retryable": True,
            "needs_live_check": False,
            "browser_context": True,
            "network_route": "browser",
            "proxy_mode": "browser",
        }

    http_status = int(raw.get("status") or 0)
    response_text = str(raw.get("body") or "")
    response_headers = raw.get("headers") if isinstance(raw.get("headers"), dict) else {}
    if not (200 <= http_status < 300):
        is_auth_expired = http_status == 401
        is_cf_challenge = http_status == 403 and (
            str(response_headers.get("cf-mitigated") or "").lower() == "challenge"
            or "enable javascript and cookies to continue" in response_text.lower()
            or "challenge-platform" in response_text.lower()
        )
        if is_auth_expired:
            error = "AT已过期/失效，请手动查活刷新"
        elif is_cf_challenge:
            error = "浏览器会话仍被 Cloudflare Challenge 拦截，请确认浏览器已完成验证"
        else:
            error = str(raw.get("error") or f"HTTP {http_status or '未知'}")
        return {
            "ok": False,
            "checked_at": now_iso(),
            "http_status": http_status or None,
            "error": error,
            "response_preview": response_text[:500],
            "retryable": http_status == 0,
            "cloudflare_challenge": is_cf_challenge,
            "token_expired": True if is_auth_expired else token_claims(token).get("token_expired"),
            "needs_live_check": bool(is_auth_expired or is_cf_challenge),
            "browser_context": True,
            "network_route": "browser",
            "proxy_mode": "browser",
        }

    try:
        data = json.loads(response_text)
    except Exception:
        data = None
    if not isinstance(data, dict):
        return {
            "ok": False,
            "checked_at": now_iso(),
            "http_status": http_status,
            "error": "浏览器套餐响应不是 JSON 对象",
            "response_preview": response_text[:500],
            "retryable": False,
            "browser_context": True,
            "network_route": "browser",
            "proxy_mode": "browser",
        }
    try:
        result = parse_accounts_check(data, token=token)
    except Exception as exc:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "http_status": http_status,
            "error": f"浏览器套餐响应解析失败: {type(exc).__name__}: {str(exc)[:180]}",
            "response_preview": response_text[:500],
            "retryable": False,
            "browser_context": True,
            "network_route": "browser",
            "proxy_mode": "browser",
        }
    result.update({
        "http_status": http_status,
        "attempt_count": 1,
        "max_attempts": 1,
        "retryable": False,
        "browser_context": True,
        "network_route": "browser",
        "proxy_mode": "browser",
        "timezone_offset_min": str(timezone_offset_min) if timezone_offset_min is not None else raw.get("timezone_offset_min"),
    })
    return result


def check_account_plan_browser(
    browser_page: Any,
    token: str,
    *,
    timezone_offset_min: str | None = None,
    request_timeout: float | None = None,
) -> dict:
    """在已登录的真实浏览器页面中查询套餐，复用 Cookie 和 Cloudflare 验证状态。"""
    token = normalize_token(token)
    if not token:
        return {"ok": False, "checked_at": now_iso(), "error": "token 为空"}
    if browser_page is None:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "error": "缺少真实浏览器页面，无法执行套餐查询",
            "retryable": False,
            "needs_live_check": True,
        }

    raw = None
    route_handler = None
    route_pattern = f"**{ACCOUNTS_CHECK_PATH}*"
    if request_timeout is None:
        try:
            from config import proxy as proxy_cfg

            request_timeout = min(8.0, float(getattr(proxy_cfg, "PLAN_CHECK_TIMEOUT", 8.0) or 8.0))
        except (TypeError, ValueError):
            request_timeout = 8.0
    request_timeout_ms = int(max(1.0, min(10.0, float(request_timeout))) * 1000)
    try:
        is_playwright_page = all(
            callable(getattr(browser_page, name, None))
            for name in ("evaluate", "route", "goto", "unroute")
        )
        if is_playwright_page:
            try:
                browser_context = browser_page.evaluate(
                    """() => {
                      const deviceCookie = document.cookie.split(';')
                        .map(value => value.trim())
                        .find(value => value.startsWith('oai-did='));
                      return {
                        language: String(navigator.language || 'en-US'),
                        timezoneOffset: String(new Date().getTimezoneOffset()),
                        deviceId: deviceCookie
                          ? decodeURIComponent(deviceCookie.slice('oai-did='.length))
                          : '',
                      };
                    }"""
                )
            except Exception:
                browser_context = {}
            if not isinstance(browser_context, dict):
                browser_context = {}
            language = str(browser_context.get("language") or "en-US").strip()[:32]
            device_id = str(browser_context.get("deviceId") or "").strip()[:128]
            timezone_value = str(
                timezone_offset_min
                if timezone_offset_min is not None
                else browser_context.get("timezoneOffset") or "0"
            )
            url = (
                f"https://chatgpt.com{ACCOUNTS_CHECK_PATH}"
                f"?timezone_offset_min={quote(timezone_value)}"
            )

            def route_handler(route):
                headers = dict(getattr(route.request, "headers", {}) or {})
                headers.update({
                    "accept": "application/json",
                    "authorization": f"Bearer {token}",
                    "oai-device-id": device_id,
                    "oai-language": language,
                    "referer": "https://chatgpt.com/",
                    "x-openai-target-path": ACCOUNTS_CHECK_PATH,
                    "x-openai-target-route": ACCOUNTS_CHECK_ROUTE,
                })
                route.continue_(headers=headers)

            browser_page.route(route_pattern, route_handler)
            response = browser_page.goto(url, wait_until="domcontentloaded")
            raw = {
                "status": int(getattr(response, "status", 0) or 0),
                "body": str(response.text() if response is not None else "")[:524288],
                "headers": dict(getattr(response, "headers", {}) or {}),
                "timezone_offset_min": timezone_value,
            }
        elif callable(getattr(browser_page, "execute_async_script", None)):
            raw = browser_page.execute_async_script(
                """const token = arguments[0];
                const path = arguments[1];
                const route = arguments[2];
                const timezoneOffset = arguments[3];
                const requestTimeoutMs = arguments[4];
                const done = arguments[arguments.length - 1];
                (async () => {
                  const cookieValue = (name) => document.cookie.split(';')
                    .map(value => value.trim())
                    .find(value => value.startsWith(`${name}=`))
                    ?.slice(name.length + 1) || '';
                  const timezone = timezoneOffset || String(new Date().getTimezoneOffset());
                  const url = `${path}?timezone_offset_min=${encodeURIComponent(timezone)}`;
                  const controller = new AbortController();
                  const timer = setTimeout(() => controller.abort(), requestTimeoutMs);
                  try {
                    const response = await fetch(url, {
                      method: 'GET',
                      credentials: 'include',
                      signal: controller.signal,
                      headers: {
                        'accept': 'application/json',
                        'authorization': `Bearer ${token}`,
                        'oai-device-id': decodeURIComponent(cookieValue('oai-did')),
                        'oai-language': String(navigator.language || 'en-US'),
                        'x-openai-target-path': path,
                        'x-openai-target-route': route,
                      },
                    });
                    done({
                      status: response.status,
                      body: (await response.text()).slice(0, 524288),
                      headers: {
                        'content-type': response.headers.get('content-type') || '',
                        'cf-mitigated': response.headers.get('cf-mitigated') || '',
                        'server': response.headers.get('server') || '',
                      },
                      timezone_offset_min: timezone,
                    });
                  } finally {
                    clearTimeout(timer);
                  }
                })().catch(error => done({status: 0, body: '', headers: {}, error: String(error)}));""",
                token,
                ACCOUNTS_CHECK_PATH,
                ACCOUNTS_CHECK_ROUTE,
                str(timezone_offset_min or ""),
                request_timeout_ms,
            )
        else:
            return {
                "ok": False,
                "checked_at": now_iso(),
                "error": "浏览器页面不支持脚本请求",
                "retryable": False,
                "needs_live_check": True,
            }
    except Exception as exc:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "error": f"浏览器套餐请求失败: {type(exc).__name__}: {str(exc)[:180]}",
            "retryable": True,
            "needs_live_check": False,
            "browser_context": True,
            "network_route": "browser",
            "proxy_mode": "browser",
        }
    finally:
        if route_handler is not None:
            try:
                browser_page.unroute(route_pattern, route_handler)
            except Exception:
                pass
            try:
                browser_page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
            except Exception:
                logger.warning("浏览器套餐查询完成后恢复 ChatGPT 主页失败", exc_info=True)
    if isinstance(raw, dict) and raw.get("error") and not raw.get("status"):
        return {
            "ok": False,
            "checked_at": now_iso(),
            "error": f"浏览器套餐请求失败: {str(raw.get('error'))[:180]}",
            "retryable": True,
            "needs_live_check": False,
            "browser_context": True,
            "network_route": "browser",
            "proxy_mode": "browser",
        }
    return _browser_plan_request_result(raw, token, timezone_offset_min=timezone_offset_min)


def _pick_account_entry(accounts: dict, claim_account_id: str | None) -> tuple[dict | None, str | None]:
    """从 accounts/check 响应的 accounts 对象选择账号条目。

    优先匹配 JWT claim 里的 account_id，其次 default 条目，最后第一个非 default 条目。
    """
    if claim_account_id and isinstance(accounts.get(claim_account_id), dict):
        return accounts.get(claim_account_id), claim_account_id
    if isinstance(accounts.get("default"), dict):
        item = accounts.get("default")
        account = item.get("account") or {}
        return item, account.get("account_id") or "default"
    for k, v in accounts.items():
        if k != "default" and isinstance(v, dict):
            return v, k
    return None, None


def parse_accounts_check(data: dict, *, token: str = "") -> dict:
    """从 accounts/check 响应提取套餐信息（不含试用资格，资格查询见 parse_accounts_trial）。"""
    claims = token_claims(token) if token else {}
    claim_account_id = claims.get("account_id")
    accounts = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(accounts, dict):
        raise ValueError("响应缺少 accounts 对象")

    item, account_key = _pick_account_entry(accounts, claim_account_id)
    if not isinstance(item, dict):
        raise ValueError("未找到可解析的账号条目")

    account = item.get("account") or {}
    entitlement = item.get("entitlement") or {}
    last_sub = item.get("last_active_subscription") or {}

    plan_type = account.get("plan_type") or claims.get("claim_plan_type") or ""
    subscription_plan = entitlement.get("subscription_plan") or ""
    has_active_subscription = bool(entitlement.get("has_active_subscription"))

    offers = ((item.get("eligible_offers") or {}).get("offers") or [])
    eligible_offer_ids = [o.get("id") for o in offers if isinstance(o, dict) and o.get("id")]

    result = {
        "ok": True,
        "checked_at": now_iso(),
        "account_id": account.get("account_id") or account_key or claim_account_id,
        "account_user_role": account.get("account_user_role"),
        "current_plan_type": plan_type,
        "subscription_plan": subscription_plan,
        "has_active_subscription": has_active_subscription,
        "is_active_subscription_gratis": bool(entitlement.get("is_active_subscription_gratis")),
        "expires_at": entitlement.get("expires_at"),
        "renews_at": entitlement.get("renews_at"),
        "cancels_at": entitlement.get("cancels_at"),
        "billing_period": entitlement.get("billing_period"),
        "billing_currency": entitlement.get("billing_currency"),
        "is_delinquent": bool(entitlement.get("is_delinquent")),
        "discount_type": (entitlement.get("discount") or {}).get("discount_type"),
        "discount_amount": (entitlement.get("discount") or {}).get("amount"),
        "discount_duration_num_periods": (entitlement.get("discount") or {}).get("duration_num_periods"),
        "discount_expires_at": (entitlement.get("discount") or {}).get("discount_expires_at"),
        "discount_cancellation_policy": (entitlement.get("discount") or {}).get("cancellation_policy"),
        "discount_promo_campaign_id": (entitlement.get("discount") or {}).get("promo_campaign_id"),
        "last_purchase_origin_platform": last_sub.get("purchase_origin_platform"),
        "last_will_renew": bool(last_sub.get("will_renew")),
        "eligible_offer_ids": eligible_offer_ids,
        "features_count": len(item.get("features") or []),
        "can_access_with_session": bool(item.get("can_access_with_session")),
        "raw_account_plan_type": account.get("plan_type"),
    }
    result.update({k: v for k, v in claims.items() if k != "payload" and v is not None})
    return result


def normalize_trial_region(region: str) -> str:
    value = str(region or "").strip().lower()
    if value not in TRIAL_REGIONS:
        raise ValueError(f"试用资格地区 {region!r} 无效，可选 {'/'.join(TRIAL_REGIONS)}")
    return value


def parse_accounts_trial(data: dict, *, token: str = "") -> dict:
    """从 accounts/check 响应提取 Plus 试用资格。

    资格由服务端按请求出口地区下发，地区差异由调用方选择的代理决定，
    本函数只负责解析响应中的 eligible_promo_campaigns。
    """
    claims = token_claims(token) if token else {}
    claim_account_id = claims.get("account_id")
    accounts = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(accounts, dict):
        raise ValueError("响应缺少 accounts 对象")

    item, account_key = _pick_account_entry(accounts, claim_account_id)
    if not isinstance(item, dict):
        raise ValueError("未找到可解析的账号条目")

    account = item.get("account") or {}
    entitlement = item.get("entitlement") or {}
    eligible_promo_campaigns = item.get("eligible_promo_campaigns") or {}
    plus_campaign = eligible_promo_campaigns.get("plus") if isinstance(eligible_promo_campaigns, dict) else None
    plus_meta = (plus_campaign or {}).get("metadata") or {}
    discount = plus_meta.get("discount") or {}
    duration = plus_meta.get("duration") or {}

    plan_type = account.get("plan_type") or claims.get("claim_plan_type") or ""
    subscription_plan = entitlement.get("subscription_plan") or ""
    is_free = str(plan_type).lower() == "free" or str(subscription_plan).lower() == "chatgptfreeplan"

    return {
        "ok": True,
        "checked_at": now_iso(),
        "account_id": account.get("account_id") or account_key or claim_account_id,
        "current_plan_type": plan_type,
        "has_active_subscription": bool(entitlement.get("has_active_subscription")),
        "trial_eligible": bool(is_free and plus_campaign),
        "campaign_id": (plus_campaign or {}).get("id"),
        "title": plus_meta.get("title"),
        "summary": plus_meta.get("summary"),
        "discount_percentage": discount.get("percentage"),
        "duration_num_periods": duration.get("num_periods"),
        "duration_period": duration.get("period"),
        "promotion_type_label": plus_meta.get("promotion_type_label"),
    }


def _plan_check_settings(
    timeout: float | None,
    max_attempts: int | None,
    retry_delay: float | None,
) -> tuple[float, int, float]:
    from config import proxy as proxy_cfg

    timeout_value = timeout if timeout is not None else getattr(proxy_cfg, "PLAN_CHECK_TIMEOUT", 15.0)
    attempts_value = max_attempts if max_attempts is not None else getattr(proxy_cfg, "PLAN_CHECK_MAX_ATTEMPTS", 2)
    delay_value = retry_delay if retry_delay is not None else getattr(proxy_cfg, "PLAN_CHECK_RETRY_DELAY", 1.5)
    return (
        max(1.0, min(60.0, float(timeout_value or 15.0))),
        max(1, min(4, int(attempts_value or 1))),
        max(0.0, min(30.0, float(delay_value or 0.0))),
    )


def _retryable_plan_error(http_status: int | None, *, retry_forbidden: bool = False) -> bool:
    if http_status is None:
        return True
    return (
        http_status in {408, 409, 425, 429}
        or (retry_forbidden and http_status == 403)
        or http_status >= 500
    )


def _is_cloudflare_challenge(resp: Any, response_text: str = "") -> bool:
    headers = getattr(resp, "headers", {}) or {}
    normalized = {str(key).lower(): str(value).lower() for key, value in headers.items()}
    if normalized.get("cf-mitigated") == "challenge":
        return True
    body = str(response_text or "").lower()
    return "enable javascript and cookies to continue" in body or "challenge-platform" in body


def _retry_wait_seconds(resp: Any, base_delay: float, attempt: int) -> float:
    try:
        retry_after = (getattr(resp, "headers", {}) or {}).get("retry-after")
        if retry_after is not None:
            return max(0.0, min(30.0, float(retry_after)))
    except (TypeError, ValueError):
        pass
    return max(0.0, min(30.0, base_delay * attempt))


def _request_accounts_check(
    token: str,
    claims: dict,
    route: dict,
    *,
    timezone_offset_min: str,
    timeout: float | None,
    max_attempts: int | None,
    retry_delay: float | None,
    parse,
    log_label: str,
    route_provider=None,
    retry_forbidden: bool = False,
) -> dict:
    """请求 accounts/check 并按 parse 解析；带重试。parse(data, token=token) 解析失败按可重试处理。"""
    route_meta = {k: v for k, v in route.items() if k != "proxy"}
    url = f"https://chatgpt.com{ACCOUNTS_CHECK_PATH}?timezone_offset_min={quote(str(timezone_offset_min))}"
    try:
        timeout_seconds, attempts, base_delay = _plan_check_settings(timeout, max_attempts, retry_delay)
    except Exception as exc:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "http_status": None,
            "error": f"{log_label}重试配置错误: {exc}",
            "retryable": False,
            **route_meta,
            **{k: v for k, v in claims.items() if k != "payload"},
        }

    last_result: dict | None = None
    for attempt in range(1, attempts + 1):
        env = None
        resp = None
        attempt_route = route
        try:
            if route_provider is not None:
                attempt_route = route_provider()
            route_meta = {k: v for k, v in attempt_route.items() if k != "proxy"}
            # 查询只需要稳定的请求头，不需要额外访问 IP 地理信息接口。
            env = BrowserSession(proxy=attempt_route["proxy"], detect_exit_geo=False)
            resp = env.get(
                url,
                headers=_common_headers(env, token, claims),
                allow_redirects=False,
                timeout=timeout_seconds,
            )
            response_text = resp.text or ""
            http_status = int(resp.status_code)
            if not (200 <= http_status < 300):
                is_auth_expired = http_status == 401
                is_cf_challenge = http_status == 403 and _is_cloudflare_challenge(resp, response_text)
                if is_auth_expired:
                    error = "AT已过期/失效，请手动查活刷新"
                elif is_cf_challenge:
                    error = "Cloudflare Challenge：当前 HTTP 会话无法完成浏览器验证，请使用真实浏览器会话查询"
                else:
                    error = f"HTTP {http_status}"
                last_result = {
                    "ok": False,
                    "checked_at": now_iso(),
                    "http_status": http_status,
                    "error": error,
                    "response_preview": response_text[:500],
                    "retryable": _retryable_plan_error(
                        http_status,
                        retry_forbidden=retry_forbidden,
                    ),
                    "cloudflare_challenge": is_cf_challenge,
                    "token_expired": True if is_auth_expired else claims.get("token_expired"),
                    "needs_live_check": True if is_auth_expired else False,
                }
            else:
                try:
                    data: Any = resp.json()
                except Exception:
                    data = json.loads(response_text) if response_text.strip().startswith(("{", "[")) else None
                if not isinstance(data, dict):
                    last_result = {
                        "ok": False,
                        "checked_at": now_iso(),
                        "http_status": http_status,
                        "error": "响应不是 JSON 对象",
                        "response_preview": response_text[:500],
                        "retryable": True,
                    }
                else:
                    parsed = parse(data, token=token)
                    parsed["http_status"] = http_status
                    parsed["attempt_count"] = attempt
                    parsed["max_attempts"] = attempts
                    parsed["request_timeout"] = timeout_seconds
                    parsed["retryable"] = False
                    parsed.update(route_meta)
                    return parsed
        except Exception as exc:
            logger.debug("%s失败: %s: %s", log_label, type(exc).__name__, exc, exc_info=True)
            last_result = {
                "ok": False,
                "checked_at": now_iso(),
                "http_status": int(resp.status_code) if resp is not None and getattr(resp, "status_code", None) else None,
                "error": f"{type(exc).__name__}: {exc}",
                "retryable": True,
            }
        finally:
            if env is not None:
                try:
                    env.session.close()
                except Exception:
                    pass

        last_result = last_result or {"ok": False, "checked_at": now_iso(), "error": "未知错误", "retryable": True}
        last_result.update({
            "attempt_count": attempt,
            "max_attempts": attempts,
            "request_timeout": timeout_seconds,
            **route_meta,
            **{k: v for k, v in claims.items() if k != "payload"},
        })
        if not last_result.get("retryable") or attempt >= attempts:
            return last_result

        wait_seconds = _retry_wait_seconds(resp, base_delay, attempt)
        logger.warning(
            "%s临时失败，第 %s/%s 次，%.1fs 后重试: %s",
            log_label,
            attempt,
            attempts,
            wait_seconds,
            last_result.get("error"),
        )
        if wait_seconds > 0:
            time.sleep(wait_seconds)

    return last_result or {
        "ok": False,
        "checked_at": now_iso(),
        "http_status": None,
        "error": f"{log_label}未执行",
        "retryable": False,
        **route_meta,
        **{k: v for k, v in claims.items() if k != "payload"},
    }


def check_account_plan(
    token: str,
    *,
    proxy: Optional[str] = None,
    timezone_offset_min: str = "-480",
    timeout: float | None = None,
    max_attempts: int | None = None,
    retry_delay: float | None = None,
) -> dict:
    token = normalize_token(token)
    if not token:
        return {"ok": False, "checked_at": now_iso(), "error": "token 为空"}
    claims = token_claims(token)
    if claims.get("token_expired") is True:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "http_status": None,
            "error": "AT已过期/失效，请手动查活刷新",
            "needs_live_check": True,
            **{k: v for k, v in claims.items() if k != "payload"},
        }

    try:
        route = resolve_plan_check_route(proxy)
    except Exception as exc:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "http_status": None,
            "error": f"套餐查询网络配置错误: {exc}",
            **{k: v for k, v in claims.items() if k != "payload"},
        }
    return _request_accounts_check(
        token,
        claims,
        route,
        timezone_offset_min=timezone_offset_min,
        timeout=timeout,
        max_attempts=max_attempts,
        retry_delay=retry_delay,
        parse=parse_accounts_check,
        log_label="套餐查询",
    )


def resolve_trial_check_route(region: str, explicit_proxy: Optional[str] = None) -> dict:
    """解析查试用资格的网络路径：显式代理或对应地区试用代理池。

    试用资格按请求出口地区下发，直连或错区代理会得出错误结论，
    因此池为空时直接抛错，不做直连降级。
    """
    region = normalize_trial_region(region)
    if explicit_proxy is not None:
        selected = str(explicit_proxy or "").strip()
        if not selected:
            raise ValueError(f"查询{region.upper()}试用资格必须使用{region.upper()}出口代理，不支持直连")
        return {
            "proxy": selected,
            "proxy_source": "request",
            "proxy_used": _mask_proxy(selected) or None,
            "proxy_username": proxy_username(selected) or None,
        }

    from config import proxy as proxy_cfg

    selected = str(proxy_cfg.pick_trial_proxy(region) or "").strip()
    if not selected:
        pool_name = TRIAL_PROXY_POOL_NAMES[region]
        raise ValueError(f"未配置{region.upper()}试用查询代理池（{pool_name}），无法查询{region.upper()}资格")
    return {
        "proxy": selected,
        "proxy_source": "trial_pool",
        "proxy_used": _mask_proxy(selected),
        "proxy_username": proxy_username(selected) or None,
    }


def check_account_trial(
    token: str,
    region: str,
    *,
    proxy: Optional[str] = None,
    timezone_offset_min: str | None = None,
    timeout: float | None = None,
    max_attempts: int | None = None,
    retry_delay: float | None = None,
) -> dict:
    """经指定地区代理查询 Plus 试用资格；地区由代理出口决定。"""
    token = normalize_token(token)
    if not token:
        return {"ok": False, "checked_at": now_iso(), "error": "token 为空"}
    claims = token_claims(token)
    if claims.get("token_expired") is True:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "http_status": None,
            "error": "AT已过期/失效，请手动查活刷新",
            "needs_live_check": True,
            **{k: v for k, v in claims.items() if k != "payload"},
        }

    try:
        region = normalize_trial_region(region)
        if timezone_offset_min in (None, ""):
            timezone_offset_min = trial_timezone_offset_min(region)
        route = resolve_trial_check_route(region, proxy)
    except Exception as exc:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "http_status": None,
            "error": f"试用资格查询配置错误: {exc}",
            **{k: v for k, v in claims.items() if k != "payload"},
        }
    if proxy is None:
        first_route = route
        first_attempt = True

        def route_provider():
            nonlocal first_attempt
            if first_attempt:
                first_attempt = False
                return first_route
            return resolve_trial_check_route(region)
    else:
        route_provider = None

    result = _request_accounts_check(
        token,
        claims,
        route,
        timezone_offset_min=timezone_offset_min,
        timeout=timeout,
        max_attempts=max_attempts,
        retry_delay=retry_delay,
        parse=parse_accounts_trial,
        log_label="试用资格查询",
        route_provider=route_provider,
        retry_forbidden=proxy is None,
    )
    result["region"] = region
    return result
