# -*- coding: utf-8 -*-
"""Remail 开放 API 邮箱客户端。

Remail 的开放 API 与本项目已有的“生成随机邮箱”类服务不同：

1. 先用 API Key 按项目下一个 ``code`` 或 ``purchase`` 订单；
2. 订单返回交付邮箱和只属于该订单的 service token；
3. 取码时使用 ``/v1/pickup``，不再携带 API Key，只携带邮箱和 service token。

因此 service token 必须和邮箱一起保存在当前进程上下文中，不能只根据邮箱地址
重新拼接取件请求。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

from config import email as _email_cfg
from core.otp_utils import extract_otp

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_API_BASE = "https://remail.aishop6.com"
REQUEST_TIMEOUT = 20
_CODE_RE = re.compile(r"\b(\d{6})\b")
_FINAL_ORDER_STATUSES = {"failed", "refunded", "closed"}


class RemailError(RuntimeError):
    """Remail API 请求、下单或取码失败。"""


# 兼容调用方可能使用的命名。
RemailClientError = RemailError


@dataclass
class RemailAccount:
    """一次 Remail 订单的取件上下文。"""

    email: str
    service_token: str
    order_no: str
    project_id: int
    email_suffix: str


_CONTEXT_CACHE: dict[str, RemailAccount] = {}
_CONTEXT_LOCK = threading.RLock()


def _cache_key(email: str) -> str:
    return str(email or "").strip().lower()


def _base_url(value: str | None = None) -> str:
    """返回 API 根地址，也兼容用户误填文档地址 ``.../docs``。"""
    raw = str(
        value if value is not None else getattr(_email_cfg, "REMAIL_API_BASE", DEFAULT_API_BASE) or DEFAULT_API_BASE
    ).strip()
    if not raw:
        raw = DEFAULT_API_BASE
    if not re.match(r"^https?://", raw, re.IGNORECASE):
        raw = "https://" + raw

    parsed = urlsplit(raw.rstrip("/"))
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise RemailError("Remail API 地址无效，请填写 https://remail.aishop6.com（不要填写接口路径）")

    path = parsed.path.rstrip("/")
    # 文档链接可直接粘贴到配置页；API 实际位于同一域名根路径。
    if path.lower() == "/docs":
        path = ""
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")


def _request_timeout() -> int:
    try:
        value = int(getattr(_email_cfg, "REMAIL_REQUEST_TIMEOUT", REQUEST_TIMEOUT) or REQUEST_TIMEOUT)
    except (TypeError, ValueError):
        value = REQUEST_TIMEOUT
    return max(1, min(120, value))


def _api_key() -> str:
    value = str(getattr(_email_cfg, "REMAIL_API_KEY", "") or "").strip()
    if not value:
        raise RemailError("Remail API Key 未配置，请在配置 → 邮箱 / OTP 填写 REMAIL_API_KEY")
    return value


def _auth_headers() -> dict[str, str]:
    return {"Accept": "application/json", "Authorization": f"Bearer {_api_key()}"}


def _error_message(payload, response) -> str:
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("error") or payload.get("detail")
        request_id = payload.get("requestId") or payload.get("request_id")
        if message:
            return f"{message} (requestId={request_id})" if request_id else str(message)
    text = str(getattr(response, "text", "") or "").strip()
    return text[:240] if text else "服务端未返回错误信息"


def _request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    headers: dict[str, str] | None = None,
    authenticated: bool = True,
):
    """调用 Remail API 并返回 JSON payload。

    ``authenticated=False`` 仅用于 pickup 接口。服务 token 不写入日志和异常文本。
    """
    request_headers = {"Accept": "application/json"}
    if authenticated:
        request_headers.update(_auth_headers())
    if headers:
        request_headers.update(headers)

    url = _base_url() + (path if str(path).startswith("/") else f"/{path}")
    try:
        response = requests.request(
            method.upper(),
            url,
            params=params,
            json=json_body,
            headers=request_headers,
            timeout=_request_timeout(),
        )
    except requests.RequestException as exc:
        raise RemailError(f"Remail 请求失败 ({method.upper()} {path}): {type(exc).__name__}: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        if response.status_code >= 400:
            raise RemailError(
                f"Remail 请求失败 ({method.upper()} {path}): HTTP {response.status_code}; "
                f"{_error_message(None, response)}"
            ) from exc
        raise RemailError(f"Remail 响应不是 JSON ({method.upper()} {path})") from exc

    if response.status_code >= 400:
        if response.status_code == 401 and authenticated:
            raise RemailError(f"Remail API Key 无效或已失效 ({path})")
        raise RemailError(
            f"Remail 请求失败 ({method.upper()} {path}): HTTP {response.status_code}; "
            f"{_error_message(payload, response)}"
        )
    return payload


def _first_value(data: dict, *keys: str):
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return value
    return None


def _unwrap_order(payload) -> dict:
    """读取 OpenAPI 定义的 Order，并兼容少数网关包裹 data/order 的响应。"""
    if not isinstance(payload, dict):
        raise RemailError("Remail 下单响应不是对象")
    if any(
        k in payload
        for k in ("orderNo", "order_no", "deliveryEmail", "delivery_email", "serviceToken", "service_token")
    ):
        return payload
    for key in ("data", "order"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    raise RemailError("Remail 下单响应缺少订单数据")


def _project_id() -> int:
    raw = getattr(_email_cfg, "REMAIL_PROJECT_ID", 2)
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        raise RemailError(
            "Remail 项目 ID 未配置，请先通过 Remail API 查询项目后填写 REMAIL_PROJECT_ID"
        )
    return value


def _email_suffix() -> str:
    suffix = str(getattr(_email_cfg, "REMAIL_EMAIL_SUFFIX", "outlook.com") or "").strip().lstrip("@")
    if not suffix or "@" in suffix or any(ch.isspace() for ch in suffix):
        raise RemailError("Remail 邮箱后缀无效，请填写 outlook.com 等域名（不要填写完整邮箱）")
    return suffix


def _supply_policy() -> str:
    value = str(getattr(_email_cfg, "REMAIL_SUPPLY_POLICY", "public_only") or "public_only").strip().lower()
    if value not in {"private_first", "public_only"}:
        raise RemailError("Remail 库存策略无效，只支持 private_first 或 public_only")
    return value


def _service_mode() -> str:
    value = str(getattr(_email_cfg, "REMAIL_SERVICE_MODE", "purchase") or "purchase").strip().lower()
    if value not in {"code", "purchase"}:
        raise RemailError("Remail 服务模式无效，只支持 code 或 purchase")
    return value


def _order_wait_seconds() -> int:
    try:
        value = int(getattr(_email_cfg, "REMAIL_ORDER_WAIT_SECONDS", 30) or 30)
    except (TypeError, ValueError):
        value = 30
    return max(0, min(180, value))


def _order_credentials(order: dict) -> tuple[str, str, str] | None:
    email = str(_first_value(order, "deliveryEmail", "delivery_email") or "").strip()
    token = str(_first_value(order, "serviceToken", "service_token") or "").strip()
    order_no = str(_first_value(order, "orderNo", "order_no") or "").strip()
    if email and "@" in email and token:
        return email, token, order_no
    return None


def _order_status_error(order: dict) -> str | None:
    status = str(order.get("status") or "").strip().lower()
    if status in _FINAL_ORDER_STATUSES:
        failure = str(order.get("failureCode") or order.get("failure_code") or "").strip()
        return f"Remail 订单未就绪: status={status}" + (f", failure={failure}" if failure else "")
    return None


def _wait_for_order_credentials(order: dict) -> tuple[str, str, str]:
    credentials = _order_credentials(order)
    if credentials:
        return credentials

    order_no = str(_first_value(order, "orderNo", "order_no") or "").strip()
    if not order_no:
        raise RemailError("Remail 下单成功但响应缺少 service token 或订单号")

    error = _order_status_error(order)
    if error:
        raise RemailError(error)

    deadline = time.monotonic() + _order_wait_seconds()
    latest = order
    while time.monotonic() <= deadline:
        try:
            latest = _unwrap_order(_request("GET", f"/v1/open/orders/{order_no}"))
        except RemailError:
            # 订单已创建，短暂的详情接口错误不应重新下单，继续等待到截止时间。
            if time.monotonic() >= deadline:
                raise
        else:
            credentials = _order_credentials(latest)
            if credentials:
                return credentials
            error = _order_status_error(latest)
            if error:
                raise RemailError(error)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(1, remaining))

    status = str(latest.get("status") or "unknown")
    raise RemailError(f"Remail 订单等待 service token 超时: order={order_no}, status={status}")


# ---- 失败邮箱复用队列 -------------------------------------------------
#
# 注册失败但订单仍在服务端有效的邮箱，把 {email, order_no} 写入项目根
# remail_reuse_queue.json；后续 pick_account 优先按订单号恢复取件上下文
# （service token 只存在服务端订单里，进程重启后必须按订单号取回），
# 复用完队列再恢复正常下单。

_REUSE_QUEUE_PATH = _PROJECT_ROOT / "remail_reuse_queue.json"
_REUSE_QUEUE_LOCK = threading.Lock()


def _load_reuse_queue() -> list[dict]:
    try:
        data = json.loads(_REUSE_QUEUE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception as exc:
        logger.warning("[Remail] 复用队列读取失败，按空队列处理：%s", exc)
        return []
    return data if isinstance(data, list) else []


def _save_reuse_queue(items: list[dict]) -> None:
    try:
        _REUSE_QUEUE_PATH.write_text(
            json.dumps(items, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("[Remail] 复用队列写入失败：%s", exc)


def restore_order_context(order_no: str, email_hint: str = "") -> RemailAccount | None:
    """按订单号重建取件上下文；订单已失效/查询不到时返回 None。"""
    no = str(order_no or "").strip()
    if not no:
        return None
    try:
        order = _unwrap_order(_request("GET", f"/v1/open/orders/{no}"))
    except RemailError as exc:
        logger.warning("[Remail] 复用订单 %s 查询失败：%s", no, str(exc)[:160])
        return None
    except Exception as exc:
        logger.warning("[Remail] 复用订单 %s 查询异常：%s: %s", no, type(exc).__name__, str(exc)[:160])
        return None
    credentials = _order_credentials(order)
    if not credentials:
        logger.warning(
            "[Remail] 复用订单 %s 不可用（缺少 service token，status=%s）",
            no,
            str(order.get("status") or "unknown"),
        )
        return None
    email, token, resolved_no = credentials
    if email_hint and email_hint.lower() != email.lower():
        logger.info("[Remail] 订单 %s 交付邮箱与记录不一致：记录=%s 实际=%s（以服务端为准）", no, email_hint, email)
    account = RemailAccount(
        email=email,
        service_token=token,
        order_no=resolved_no or no,
        project_id=_project_id(),
        email_suffix=_email_suffix(),
    )
    with _CONTEXT_LOCK:
        _CONTEXT_CACHE[_cache_key(email)] = account
    logger.info("[Remail] 已按订单恢复取件上下文：%s order=%s", email, account.order_no)
    return account


def _pop_reuse_account() -> RemailAccount | None:
    """从复用队列弹出一个可用的失败邮箱；恢复失败的条目重试一次后淘汰。"""
    try:
        enabled = bool(getattr(_email_cfg, "REMAIL_REUSE_FAILED_EMAILS", False))
    except Exception:
        enabled = False
    if not enabled:
        return None
    with _REUSE_QUEUE_LOCK:
        queue = _load_reuse_queue()
        while queue:
            entry = queue.pop(0)
            try:
                attempts = int(entry.get("attempts") or 0)
            except (TypeError, ValueError):
                attempts = 0
            if attempts >= 2:
                logger.warning(
                    "[Remail] 复用条目多次恢复失败，已丢弃：%s order=%s",
                    entry.get("email") or "-",
                    entry.get("order_no") or "-",
                )
                _save_reuse_queue(queue)
                continue
            account = restore_order_context(str(entry.get("order_no") or ""), str(entry.get("email") or ""))
            if account is not None:
                _save_reuse_queue(queue)
                return account
            # 恢复失败可能是网络抖动，也可能是订单已失效；记一次失败放回队列，
            # 留给下一次注册再试，累计两次失败即淘汰，避免坏条目长期占位。
            entry["attempts"] = attempts + 1
            queue.append(entry)
            _save_reuse_queue(queue)
            break
    return None


def pick_account() -> RemailAccount:
    """按配置创建一个 Remail 接码/长效购买订单并返回交付邮箱。

    项目根存在 remail_reuse_queue.json（失败邮箱复用队列）时，优先按订单号
    恢复队列中邮箱的取件上下文并复用；队列清空后恢复正常下单。
    """
    reused = _pop_reuse_account()
    if reused is not None:
        logger.info("[Remail] 复用队列命中，使用已有邮箱：%s order=%s", reused.email, reused.order_no or "-")
        return reused
    project_id = _project_id()
    email_suffix = _email_suffix()
    service_mode = _service_mode()
    supply = _supply_policy()
    idempotency_key = f"turb-gpt-free-register-{uuid.uuid4()}"

    payload = _request(
        "POST",
        "/v1/open/orders",
        params={"serviceMode": service_mode, "supply": supply},
        json_body={"projectId": project_id, "emailSuffix": email_suffix},
        headers={"Idempotency-Key": idempotency_key},
    )
    order = _unwrap_order(payload)
    email, service_token, order_no = _wait_for_order_credentials(order)
    account = RemailAccount(
        email=email,
        service_token=service_token,
        order_no=order_no,
        project_id=project_id,
        email_suffix=email_suffix,
    )
    with _CONTEXT_LOCK:
        _CONTEXT_CACHE[_cache_key(email)] = account
    logger.info("[Remail] 已创建邮箱订单: %s order=%s project=%s", email, order_no or "-", project_id)
    return account


def get_email() -> str:
    """兼容其他临时邮箱客户端的旧入口。"""
    return pick_account().email


def get_account_context(email: str) -> RemailAccount | None:
    with _CONTEXT_LOCK:
        return _CONTEXT_CACHE.get(_cache_key(email))


def _requeue_account(account: RemailAccount, note: str | None = None) -> None:
    """把可重试失败的已领取订单放回队首，供下一次注册优先恢复。"""
    if not account.order_no:
        return
    entry = {
        "email": account.email,
        "order_no": account.order_no,
        "attempts": 0,
    }
    with _REUSE_QUEUE_LOCK:
        queue = _load_reuse_queue()
        queue = [
            item for item in queue
            if str(item.get("order_no") or "").strip() != account.order_no
            and _cache_key(str(item.get("email") or "")) != _cache_key(account.email)
        ]
        queue.insert(0, entry)
        _save_reuse_queue(queue)
    logger.info(
        "[Remail] 已把可重试订单放回复用队列首位：%s order=%s note=%s",
        account.email,
        account.order_no,
        note or "",
    )


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    """释放本地取件上下文；可重试失败的订单会重新进入复用队列。"""
    with _CONTEXT_LOCK:
        account = _CONTEXT_CACHE.pop(_cache_key(email), None)
    if account:
        if str(status or "").strip().lower() == "available":
            _requeue_account(account, note=note)
        logger.info(
            "[Remail] 已释放取件上下文: %s order=%s status=%s note=%s",
            email,
            account.order_no or "-",
            status,
            note or "",
        )


def _parse_timestamp(raw) -> float | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value / 1000.0 if value > 10_000_000_000 else value

    text = str(raw).strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        value = float(text)
        return value / 1000.0 if value > 10_000_000_000 else value
    try:
        iso = text[:-1] + "+00:00" if text.endswith("Z") else text
        parsed = datetime.fromisoformat(iso)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _pickup_items(payload) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, dict):
        payload = data
    elif isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    items = payload.get("items")
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _message_code(message: dict) -> str | None:
    direct = _first_value(message, "verificationCode", "verification_code", "code", "otp")
    if direct is not None:
        match = _CODE_RE.search(str(direct))
        if match:
            return match.group(1)

    preview = str(_first_value(message, "bodyPreview", "body_preview", "body", "text", "content") or "")
    return extract_otp(
        {
            "from": str(_first_value(message, "sender", "from", "fromEmail") or ""),
            "subject": str(message.get("subject") or ""),
            "text": preview,
            "content": preview,
        }
    )


def fetch_latest_otp(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
) -> str:
    """轮询 Remail pickup，返回领取时间之后最新的六位验证码。"""
    target = str(email or "").strip()
    if not target:
        raise RemailError("Remail 取码缺少邮箱地址")
    account = get_account_context(target)
    if account is None:
        raise RemailError("Remail 找不到该邮箱的 service token，请在同一进程中先领取邮箱")

    try:
        wait_seconds = int(max_wait if max_wait is not None else getattr(_email_cfg, "OTP_MAX_WAIT", 90))
    except (TypeError, ValueError):
        wait_seconds = 90
    try:
        interval = int(poll_interval if poll_interval is not None else getattr(_email_cfg, "OTP_POLL_INTERVAL", 3))
    except (TypeError, ValueError):
        interval = 3
    try:
        settle = int(settle_seconds if settle_seconds is not None else getattr(_email_cfg, "OTP_SETTLE_SECONDS", 5))
    except (TypeError, ValueError):
        settle = 5
    interval = max(1, interval)
    settle = max(0, settle)
    deadline = time.monotonic() + max(0, wait_seconds)
    best_otp: str | None = None
    best_timestamp = float("-inf")
    settle_until: float | None = None
    last_error = "收件箱为空或尚未出现新的验证码"

    logger.info("[Remail] 开始轮询邮箱 %s，最长 %ss", target, wait_seconds)
    while time.monotonic() <= deadline:
        try:
            payload = _request(
                "GET",
                "/v1/pickup",
                params={"email": target, "token": account.service_token},
                authenticated=False,
            )
            items = _pickup_items(payload)
            messages = []
            for message in items:
                received_at = _first_value(
                    message, "receivedAt", "received_at", "timestamp", "createdAt", "created_at"
                )
                timestamp = _parse_timestamp(received_at)
                if after_ts is not None and timestamp is not None and timestamp < after_ts - 30:
                    continue
                code = _message_code(message)
                if code:
                    messages.append((timestamp, code))

            messages.sort(
                key=lambda value: value[0] if value[0] is not None else float("-inf"),
                reverse=True,
            )
            for timestamp, code in messages:
                candidate_time = float("-inf") if timestamp is None else timestamp
                if (
                    best_otp is None
                    or candidate_time > best_timestamp
                    or (candidate_time == best_timestamp and code != best_otp)
                ):
                    best_otp = code
                    best_timestamp = candidate_time
                    settle_until = time.monotonic() + settle
                    logger.info("[Remail] 锁定 OTP 候选，等待 %ss 确认", settle)

            if best_otp and settle_until is not None and time.monotonic() >= settle_until:
                return best_otp
        except RemailError as exc:
            last_error = str(exc)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))

    if best_otp:
        return best_otp
    raise RemailError(f"等待 Remail 验证码超时: {target}; {last_error}")


def _product_type_for_suffix(suffix: str) -> str:
    value = suffix.strip().lstrip("@").lower()
    if value == "gmail.com":
        return "gmail"
    if value == "gmail_variant":
        return "gmail_variant"
    if value == "icloud.com":
        return "icloud"
    if value == "domain":
        return "domain"
    return "microsoft"


def inventory_query_seconds() -> int:
    """注册页库存轮询间隔；供后端缓存 TTL 与前端定时器共用。"""
    try:
        value = int(getattr(_email_cfg, "REMAIL_INVENTORY_QUERY_SECONDS", 10) or 10)
    except (TypeError, ValueError):
        value = 10
    return max(3, min(600, value))


def _suffix_inventory_entry(product: dict, suffix: str) -> dict | None:
    for entry in product.get("suffixes") or []:
        if str(entry.get("suffix") or "").strip().lstrip("@").lower() == suffix:
            return entry
    return None


_INVENTORY_CACHE: dict = {}
_INVENTORY_CACHE_LOCK = threading.Lock()


def fetch_suffix_inventory(suffix: str | None = None, *, force: bool = False) -> dict:
    """查询项目详情，返回配置邮箱后缀的实时库存。

    返回 {suffix, product_type, total_available, public_available, available}；
    available 按库存策略取值：public_only 取公开库存，private_first 取总库存。
    结果短 TTL 缓存，避免概览与前端轮询同时打到 Remail API。
    """
    target = str(suffix if suffix is not None else _email_suffix()).strip().lstrip("@").lower()
    if not target:
        raise RemailError("Remail 邮箱后缀无效")
    ttl = max(3, min(inventory_query_seconds() // 2, 30))
    now = time.monotonic()
    with _INVENTORY_CACHE_LOCK:
        cached = _INVENTORY_CACHE.get(target)
        if cached and not force and now - cached[0] < ttl:
            return cached[1]

    detail = _request("GET", f"/v1/open/projects/{_project_id()}")
    if isinstance(detail, dict) and isinstance(detail.get("data"), dict):
        detail = detail["data"]
    products = detail.get("products") if isinstance(detail, dict) else None
    if not isinstance(products, list):
        raise RemailError("Remail 项目详情响应缺少商品列表")

    matched_type = _product_type_for_suffix(target)
    type_product = None
    for product in products:
        entry = _suffix_inventory_entry(product, target)
        if entry is not None:
            result = {
                "suffix": target,
                "product_type": str(product.get("type") or matched_type),
                "total_available": int(entry.get("totalAvailable") or 0),
                "public_available": int(entry.get("publicAvailable") or 0),
            }
            break
        if str(product.get("type") or "") == matched_type and type_product is None:
            type_product = product
    else:
        if type_product is None:
            raise RemailError(f"Remail 项目商品中找不到后缀 {target} 的库存信息")
        result = {
            "suffix": target,
            "product_type": matched_type,
            "total_available": int(type_product.get("totalAvailable") or 0),
            "public_available": int(type_product.get("publicAvailable") or 0),
        }

    result["available"] = (
        result["total_available"]
        if _supply_policy() == "private_first"
        else result["public_available"]
    )
    with _INVENTORY_CACHE_LOCK:
        _INVENTORY_CACHE[target] = (time.monotonic(), result)
    return result


def list_projects(*, search: str | None = None, product_type: str | None = "microsoft") -> list[dict]:
    """查询当前 API Key 可见项目，供配置/诊断使用。"""
    params = {"offset": 0, "limit": 100}
    if search:
        params["search"] = str(search).strip()
    if product_type:
        params["productType"] = str(product_type).strip()
    payload = _request("GET", "/v1/open/projects", params=params)
    if isinstance(payload, dict):
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        items = data.get("items") if isinstance(data, dict) else None
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []
