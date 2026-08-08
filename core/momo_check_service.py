# -*- coding: utf-8 -*-
"""MoMo 支付支持检测后台队列。

完全镜像 core/plan_check_service.py：模块级线程池 + 信号量限流 + 原子占用，
单账号入队执行 check_account_momo 并把结果写回 db。
"""
from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from config import proxy as proxy_cfg
from config.proxy import pick_momo_proxy
from core import db
from core.chatgpt_momo import check_account_momo

logger = logging.getLogger(__name__)


def _int_setting(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(getattr(proxy_cfg, name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


def _float_setting(name: str, default: float, lower: float, upper: float) -> float:
    try:
        value = float(getattr(proxy_cfg, name, default) or 0.0)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


_WORKERS = _int_setting("MOMO_CHECK_WORKERS", 3, 1, 16)
_QUEUE_LIMIT = _int_setting("MOMO_CHECK_QUEUE_LIMIT", 500, _WORKERS, 5000)
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="momo-check")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_RATE_LOCK = threading.Lock()
_NEXT_REQUEST_AT = 0.0


def _wait_for_rate_slot() -> None:
    """为所有检测线程分配错开的请求启动时间。"""
    global _NEXT_REQUEST_AT
    min_interval = _float_setting("MOMO_CHECK_MIN_INTERVAL", 0.4, 0.0, 30.0)
    jitter = _float_setting("MOMO_CHECK_JITTER", 0.3, 0.0, 30.0)
    with _RATE_LOCK:
        now = time.monotonic()
        scheduled = max(now, _NEXT_REQUEST_AT) + (random.uniform(0.0, jitter) if jitter else 0.0)
        _NEXT_REQUEST_AT = scheduled + min_interval
    wait_seconds = scheduled - now
    if wait_seconds > 0:
        time.sleep(wait_seconds)


def _run_account_momo_check(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str,
    proxy: str | None,
) -> dict:
    try:
        if not db.mark_account_momo_check_running(account_id):
            return {"ok": False, "error": "账号已删除或 MoMo 检测状态已被重置"}

        # 前端未显式覆盖时，MoMo 检测单独使用专用代理池轮换；池为空则直连。
        # 传非 None 的 proxy 让 resolve_plan_check_route 走显式覆盖分支，
        # 绕开 PLAN_CHECK 网络配置，只用 MoMo 池。
        if proxy is None:
            proxy = pick_momo_proxy()

        _wait_for_rate_slot()
        result = check_account_momo(access_token, proxy=proxy)

        db.update_account_momo_check(acc_id=account_id, result=result)
        if result.get("ok"):
            has_momo = result.get("has_momo")
            logger.info(
                "[MoMo] 后台检测成功: %s, decision=%s, has_momo=%s, trigger=%s",
                email,
                result.get("decision"),
                has_momo,
                trigger,
            )
        else:
            logger.warning(
                "[MoMo] 后台检测失败: %s, trigger=%s, error=%s",
                email,
                trigger,
                result.get("error") or "未知错误",
            )
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {str(exc)[:180]}",
            "decision": "checkout_failed",
        }
        try:
            db.update_account_momo_check(acc_id=account_id, result=result)
        except Exception:
            logger.exception("[MoMo] 写入后台检测异常状态失败: account_id=%s", account_id)
        logger.exception("[MoMo] 后台检测异常: %s", email)
        return result
    finally:
        _QUEUE_SLOTS.release()


def enqueue_account_momo_check(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str,
    proxy: str | None = None,
) -> dict:
    """把检测放入统一线程池；重复检测或队列满时不提交。"""
    account_id = int(account_id)
    email = str(email or "").strip()
    access_token = str(access_token or "").strip()
    if not access_token:
        return {"accepted": False, "busy": False, "error": "账号缺少 access_token"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "MoMo 检测队列已满，请稍后重试"}

    if not db.claim_account_momo_check(acc_id=account_id, trigger=trigger):
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在检测 MoMo"}

    try:
        _EXECUTOR.submit(
            _run_account_momo_check,
            account_id=account_id,
            email=email,
            access_token=access_token,
            trigger=str(trigger or "manual"),
            proxy=proxy,
        )
    except Exception as exc:
        _QUEUE_SLOTS.release()
        result = {
            "ok": False,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"MoMo 检测入队失败: {type(exc).__name__}: {str(exc)[:160]}",
            "decision": "checkout_failed",
        }
        db.update_account_momo_check(acc_id=account_id, result=result)
        return {"accepted": False, "busy": False, "error": result["error"]}

    return {
        "accepted": True,
        "busy": False,
        "account_id": account_id,
        "email": email,
        "status": "queued",
        "trigger": str(trigger or "manual"),
    }


def queue_settings() -> dict:
    return {
        "workers": _WORKERS,
        "queue_limit": _QUEUE_LIMIT,
        "min_interval": _float_setting("MOMO_CHECK_MIN_INTERVAL", 0.4, 0.0, 30.0),
        "jitter": _float_setting("MOMO_CHECK_JITTER", 0.3, 0.0, 30.0),
    }
