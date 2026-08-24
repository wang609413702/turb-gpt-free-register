# -*- coding: utf-8 -*-
"""MoMo 提链后台队列服务。

镜像 extract_link_service：模块级线程池 + 信号量限流，
从独立记录存储读取 token，执行 extract_momo_link，结果写回记录。
整个提链流程使用 momo_link_db 缓存代理池。
每条记录的提链日志写入独立文件，前端可实时查看。
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from core import momo_link_db
from core.momo_link import extract_momo_link, monitor_payment_qr

logger = logging.getLogger(__name__)

_WORKERS = 3
_QUEUE_LIMIT = 100
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="momo-link")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)

_LOG_DIR = Path(__file__).resolve().parent.parent / "注册日志"
_RUNNING: set[str] = set()
_RUNNING_LOCK = threading.Lock()


def log_path(record_id: str) -> Path:
    """返回某条记录的提链日志文件路径。"""
    safe = str(record_id or "").replace("/", "_").replace("\\", "_").replace(":", "_")
    return _LOG_DIR / f"momo-link-{safe}.log"


def is_running(record_id: str) -> bool:
    with _RUNNING_LOCK:
        return str(record_id) in _RUNNING


def _run_momo_link(record_id: str) -> None:
    """单条记录的提链 worker。"""
    try:
        record = momo_link_db.get_record(record_id)
        if not record:
            logger.warning("[MoMoLink] 记录不存在: %s", record_id)
            return
        token = str(record.get("access_token") or "").strip()
        if not token:
            momo_link_db.update_record(record_id, {"status": "failed", "error": "缺少 access_token", "message": "缺少 access_token"})
            return

        # 从缓存代理池取代理（空则直连）
        proxy = momo_link_db.pick_proxy()

        # 准备日志文件：清空旧日志，建立 log_fn 回调
        log_file = log_path(record_id)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_lines: list[str] = []

        def _log_writer(msg: str) -> None:
            stamp = datetime.now().strftime("%H:%M:%S")
            line = f"{stamp} {msg}"
            log_lines.append(line)
            # 追加写入文件（前端轮询读取）
            try:
                with log_file.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass

        # 清空日志文件
        log_file.write_text("", encoding="utf-8")
        _log_writer(f"=== 开始提链: {record.get('email')} ===")

        with _RUNNING_LOCK:
            _RUNNING.add(str(record_id))

        momo_link_db.update_record(record_id, {"status": "running", "message": "提链中...", "proxy_used": proxy})
        result = extract_momo_link(token, proxy=proxy, log_fn=_log_writer,
                                   pick_proxy=momo_link_db.pick_proxy)

        if result.get("ok"):
            redirect_url = result.get("redirect_url") or ""
            momo_link_db.update_record(record_id, {
                "status": "waiting_scan",
                "redirect_url": redirect_url,
                "qr_urls": result.get("qr_urls") or [],
                "qr_data": "",
                "proxy_used": proxy,
                "message": "提链成功，等待扫码支付...",
                "error": "",
            })
            _log_writer(f"=== 提链成功: {redirect_url[:80]} ===")
            logger.info("[MoMoLink] 提链成功: %s -> %s", record.get("email"), redirect_url[:80])

            # ===== 保持会话监测扫码支付（任务不结束） =====
            def _on_qr(qr_data: str) -> None:
                """提取到二维码后立即写回记录，前端轮询展示。"""
                momo_link_db.update_record(record_id, {
                    "qr_data": qr_data,
                    "message": "请用手机扫码完成支付，支付完成后自动确认",
                })
                _log_writer("=== 二维码已就绪，等待扫码... ===")

            monitor_res = monitor_payment_qr(
                redirect_url,
                proxy=proxy,
                access_token=token,
                cs_id=result.get("cs_id") or "",
                processor_entity=result.get("processor_entity") or "openai_ie",
                log_fn=_log_writer,
                on_qr=_on_qr,
                is_cancelled=lambda: momo_link_db.get_record(record_id) is None,
            )
            if monitor_res.get("status") == "paid":
                momo_link_db.update_record(record_id, {
                    "status": "paid",
                    "qr_data": monitor_res.get("qr_data") or "",
                    "plan_type": monitor_res.get("plan_type") or "",
                    "message": "支付成功，已到账",
                    "error": "",
                })
                _log_writer(f"=== 支付成功: {monitor_res.get('reason') or 'planType 已变为 plus'} ===")
                logger.info("[MoMoLink] 支付成功: %s planType=%s",
                            record.get("email"), monitor_res.get("plan_type"))
            else:
                error = monitor_res.get("error") or "扫码支付未完成"
                if monitor_res.get("status") == "cancelled":
                    momo_link_db.update_record(record_id, {
                        "status": "pending",
                        "qr_data": monitor_res.get("qr_data") or "",
                        "message": "监测已取消",
                        "error": "",
                    })
                    _log_writer("=== 监测已取消 ===")
                else:
                    momo_link_db.update_record(record_id, {
                        "status": "timeout" if monitor_res.get("status") == "timeout" else "failed",
                        "qr_data": monitor_res.get("qr_data") or "",
                        "message": "扫码支付超时" if monitor_res.get("status") == "timeout" else "扫码支付异常",
                        "error": error,
                    })
                    _log_writer(f"=== 扫码支付未完成: {error} ===")
                    logger.warning("[MoMoLink] 扫码支付未完成: %s, %s", record.get("email"), error)
        else:
            momo_link_db.update_record(record_id, {
                "status": "failed",
                "message": "提链失败",
                "error": result.get("error") or "未知错误",
            })
            _log_writer(f"=== 提链失败: {result.get('error') or '未知错误'} ===")
            logger.warning("[MoMoLink] 提链失败: %s, %s", record.get("email"), result.get("error"))
    except Exception as exc:
        momo_link_db.update_record(record_id, {
            "status": "failed",
            "message": "提链异常",
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
        })
        logger.exception("[MoMoLink] 提链异常: %s", record_id)
    finally:
        with _RUNNING_LOCK:
            _RUNNING.discard(str(record_id))
        momo_link_db.mark_busy(record_id, False)
        _QUEUE_SLOTS.release()


def enqueue_momo_link(record_id: str) -> dict:
    """把单条记录的提链放入后台队列。"""
    if momo_link_db.is_busy(record_id):
        return {"accepted": False, "busy": True, "error": "该记录正在提链中"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "error": "提链队列已满，请稍后重试"}

    record = momo_link_db.get_record(record_id)
    if not record:
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": False, "error": "记录不存在"}

    momo_link_db.mark_busy(record_id, True)
    try:
        _EXECUTOR.submit(_run_momo_link, record_id)
    except Exception as exc:
        momo_link_db.mark_busy(record_id, False)
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": False, "error": f"入队失败: {exc}"}

    return {"accepted": True, "busy": False, "record_id": record_id, "status": "queued"}


def enqueue_momo_link_bulk(record_ids: list[str]) -> dict:
    """批量入队。返回 {started, busy, failed}。"""
    started, busy, failed = [], [], []
    for rid in record_ids:
        result = enqueue_momo_link(str(rid))
        item = {"record_id": str(rid), **result}
        if result.get("accepted"):
            started.append(item)
        elif result.get("busy"):
            busy.append(item)
        else:
            failed.append(item)
    return {
        "started": started, "started_count": len(started),
        "busy": busy, "busy_count": len(busy),
        "failed": failed, "failed_count": len(failed),
    }
