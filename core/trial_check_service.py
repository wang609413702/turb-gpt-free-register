# -*- coding: utf-8 -*-
"""按地区（JP/GB/DE/BR/TH/PH）的 Plus 试用资格查询后台队列。"""
from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from config import proxy as proxy_cfg
from config.trial import TRIAL_PROXY_POOL_NAMES, trial_timezone_offset_min
from core import db
from core.chatgpt_plan import check_account_trial, normalize_trial_region, token_claims

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


_WORKERS = _int_setting("TRIAL_CHECK_WORKERS", 3, 1, 16)
_QUEUE_LIMIT = _int_setting("TRIAL_CHECK_QUEUE_LIMIT", 200, _WORKERS, 5000)
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="trial-check")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_RATE_LOCK = threading.Lock()
_NEXT_REQUEST_AT = 0.0


def _wait_for_rate_slot() -> None:
    """为所有查询线程分配错开的请求启动时间。"""
    global _NEXT_REQUEST_AT
    min_interval = _float_setting("TRIAL_CHECK_MIN_INTERVAL", 0.4, 0.0, 30.0)
    jitter = _float_setting("TRIAL_CHECK_JITTER", 0.3, 0.0, 30.0)
    with _RATE_LOCK:
        now = time.monotonic()
        scheduled = max(now, _NEXT_REQUEST_AT) + (random.uniform(0.0, jitter) if jitter else 0.0)
        _NEXT_REQUEST_AT = scheduled + min_interval
    wait_seconds = scheduled - now
    if wait_seconds > 0:
        time.sleep(wait_seconds)


def _registration_recheck_delay() -> float:
    return _float_setting("PLAN_CHECK_REGISTRATION_RECHECK_DELAY", 2.0, 0.0, 30.0)


def _run_account_trial_check(
    *,
    account_id: int,
    email: str,
    access_token: str,
    region: str,
    trigger: str,
    proxy: str | None,
    timezone_offset_min: str,
    claim_id: str,
) -> dict:
    try:
        if not db.mark_account_trial_check_running(account_id, claim_id=claim_id):
            return {"ok": False, "error": "账号已删除或试用资格查询状态已被重置"}

        _wait_for_rate_slot()
        result = check_account_trial(
            access_token,
            region,
            proxy=proxy,
            timezone_offset_min=timezone_offset_min,
        )

        recheck_delay = _registration_recheck_delay()
        first_ok = bool(result.get("ok"))
        free_without_trial = (
            first_ok
            and str(result.get("current_plan_type") or "").lower() == "free"
            and not bool(result.get("trial_eligible"))
        )
        retryable_failure = (
            not first_ok
            and bool(result.get("retryable"))
            and not bool(result.get("needs_live_check"))
        )
        should_recheck = (
            trigger == "registration_auto"
            and recheck_delay > 0
            and (free_without_trial or retryable_failure)
        )
        if should_recheck:
            reason = "暂未发现资格" if first_ok else "首次查询临时失败"
            logger.info(
                "[Trial] 新账号 %s %s，%.1fs 后复查一次: %s",
                region.upper(),
                reason,
                recheck_delay,
                email,
            )
            time.sleep(recheck_delay)
            _wait_for_rate_slot()
            recheck_result = check_account_trial(
                access_token,
                region,
                proxy=proxy,
                timezone_offset_min=timezone_offset_min,
                max_attempts=1,
            )
            if recheck_result.get("ok") or not first_ok:
                result = recheck_result
            else:
                logger.warning(
                    "[Trial] 新账号资格复查失败，保留首次成功结果: %s, %s",
                    email,
                    recheck_result.get("error") or "未知错误",
                )

        db.update_account_trial_check(acc_id=account_id, result=result, claim_id=claim_id)
        if result.get("ok"):
            logger.info(
                "[Trial] 后台查询成功: %s, region=%s, eligible=%s, trigger=%s",
                email,
                result.get("region") or region,
                bool(result.get("trial_eligible")),
                trigger,
            )
        else:
            logger.warning(
                "[Trial] 后台查询失败: %s, region=%s, trigger=%s, error=%s",
                email,
                result.get("region") or region,
                trigger,
                result.get("error") or "未知错误",
            )
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "region": region,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {str(exc)[:180]}",
        }
        try:
            db.update_account_trial_check(acc_id=account_id, result=result, claim_id=claim_id)
        except Exception:
            logger.exception("[Trial] 写入后台查询异常状态失败: account_id=%s", account_id)
        logger.exception("[Trial] 后台查询异常: %s, region=%s", email, region)
        return result
    finally:
        _QUEUE_SLOTS.release()


def enqueue_account_trial_check(
    *,
    account_id: int,
    email: str,
    access_token: str,
    region: str,
    trigger: str,
    proxy: str | None = None,
    timezone_offset_min: str | None = None,
) -> dict:
    """把地区资格查询放入统一线程池；重复查询或队列满时不提交。"""
    account_id = int(account_id)
    email = str(email or "").strip()
    access_token = str(access_token or "").strip()
    if not access_token:
        return {"accepted": False, "busy": False, "error": "账号缺少 access_token"}
    if token_claims(access_token).get("token_expired") is True:
        return {
            "accepted": False,
            "busy": False,
            "needs_live_check": True,
            "error": "AT已过期/失效，请先查活刷新后再查询资格",
        }
    try:
        region = normalize_trial_region(region)
    except ValueError as exc:
        return {"accepted": False, "busy": False, "error": str(exc)}

    if proxy is None:
        from config.proxy import pick_trial_proxy

        if not str(pick_trial_proxy(region) or "").strip():
            pool_name = TRIAL_PROXY_POOL_NAMES[region]
            return {
                "accepted": False,
                "busy": False,
                "error": f"未配置{region.upper()}试用查询代理池（{pool_name}），请先在配置页填写",
            }

    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "试用资格查询队列已满，请稍后重试"}

    claim_id = f"trial-{account_id}-{time.time_ns()}"
    if not db.claim_account_trial_check(
        acc_id=account_id,
        trigger=trigger,
        region=region,
        claim_id=claim_id,
    ):
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在查询试用资格"}

    try:
        _EXECUTOR.submit(
            _run_account_trial_check,
            account_id=account_id,
            email=email,
            access_token=access_token,
            region=region,
            trigger=str(trigger or "manual"),
            proxy=proxy,
            timezone_offset_min=str(
                timezone_offset_min
                if timezone_offset_min not in (None, "")
                else trial_timezone_offset_min(region)
            ),
            claim_id=claim_id,
        )
    except Exception as exc:
        _QUEUE_SLOTS.release()
        result = {
            "ok": False,
            "region": region,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"试用资格查询入队失败: {type(exc).__name__}: {str(exc)[:160]}",
        }
        db.update_account_trial_check(acc_id=account_id, result=result, claim_id=claim_id)
        return {"accepted": False, "busy": False, "error": result["error"]}

    return {
        "accepted": True,
        "busy": False,
        "account_id": account_id,
        "email": email,
        "status": "queued",
        "region": region,
        "trigger": str(trigger or "manual"),
    }


def queue_settings() -> dict:
    return {
        "workers": _WORKERS,
        "queue_limit": _QUEUE_LIMIT,
        "min_interval": _float_setting("TRIAL_CHECK_MIN_INTERVAL", 0.4, 0.0, 30.0),
        "jitter": _float_setting("TRIAL_CHECK_JITTER", 0.3, 0.0, 30.0),
    }
