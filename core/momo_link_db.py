# -*- coding: utf-8 -*-
"""MoMo 提链独立数据存储。

提链记录存在 JSON 文件 momo_link_records.json；代理池缓存到 momo_link_proxy_cache.json（重启后保留）。
不依赖账号表，和账号页面完全分离。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_RECORDS_FILE = Path(__file__).resolve().parent.parent / "momo_link_records.json"
_PROXY_CACHE_FILE = Path(__file__).resolve().parent.parent / "momo_link_proxy_cache.json"

# 并发控制：记录正在提链中的 record_id，避免重复入队
_BUSY: set[str] = set()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _load() -> list[dict[str, Any]]:
    if not _RECORDS_FILE.exists():
        return []
    try:
        data = json.loads(_RECORDS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(records: list[dict[str, Any]]) -> None:
    _RECORDS_FILE.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def list_records() -> list[dict[str, Any]]:
    """返回全部提链记录（不含 token）。"""
    with _LOCK:
        records = _load()
    out = []
    for r in records:
        item = {k: v for k, v in r.items() if k != "access_token"}
        out.append(item)
    return out


def get_record(record_id: str) -> dict[str, Any] | None:
    """返回单条记录（含 token，供提链使用）。"""
    with _LOCK:
        records = _load()
    return next((r for r in records if str(r.get("id")) == str(record_id)), None)


def add_records(accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """批量添加提链记录。accounts: [{id/email/plan/access_token}]。
    已存在同邮箱的记录会跳过。返回新增的记录列表（不含 token）。"""
    added: list[dict[str, Any]] = []
    with _LOCK:
        records = _load()
        existing_emails = {str(r.get("email") or "").lower() for r in records}
        for acc in accounts:
            email = str(acc.get("email") or "").strip()
            if not email or email.lower() in existing_emails:
                continue
            record = {
                "id": f"ml_{int(time.time() * 1000)}_{len(records)}",
                "account_id": acc.get("id"),
                "email": email,
                "plan": acc.get("plan") or acc.get("current_plan_type") or acc.get("plan_type") or "",
                "access_token": str(acc.get("access_token") or "").strip(),
                "status": "pending",  # pending / running / waiting_scan / paid / failed / timeout
                "redirect_url": "",
                "qr_urls": [],
                "qr_data": "",
                "proxy_used": "",
                "plan_type": "",
                "message": "",
                "error": "",
                "created_at": _now(),
                "updated_at": _now(),
            }
            records.append(record)
            existing_emails.add(email.lower())
            added.append({k: v for k, v in record.items() if k != "access_token"})
        _save(records)
    return added


def update_record(record_id: str, updates: dict[str, Any]) -> bool:
    """更新单条记录的字段。"""
    with _LOCK:
        records = _load()
        for r in records:
            if str(r.get("id")) == str(record_id):
                r.update(updates)
                r["updated_at"] = _now()
                _save(records)
                return True
        return False


def delete_records(record_ids: list[str]) -> int:
    """删除指定记录，返回删除数量。"""
    ids = {str(rid) for rid in record_ids}
    with _LOCK:
        records = _load()
        before = len(records)
        records = [r for r in records if str(r.get("id")) not in ids]
        after = len(records)
        _save(records)
    return before - after


def clear_records() -> int:
    """清空全部记录。"""
    with _LOCK:
        records = _load()
        count = len(records)
        _save([])
    return count


def is_busy(record_id: str) -> bool:
    with _LOCK:
        return str(record_id) in _BUSY


def mark_busy(record_id: str, busy: bool) -> None:
    with _LOCK:
        if busy:
            _BUSY.add(str(record_id))
        else:
            _BUSY.discard(str(record_id))


# ==================== 代理池（缓存文件，重启保留） ==================== #
def _load_proxy_cache() -> list[str]:
    """从缓存文件读取代理池。"""
    if not _PROXY_CACHE_FILE.exists():
        return []
    try:
        data = json.loads(_PROXY_CACHE_FILE.read_text(encoding="utf-8"))
        return [str(p) for p in data] if isinstance(data, list) else []
    except Exception:
        return []


def _save_proxy_cache(proxies: list[str]) -> None:
    """代理池写入缓存文件。"""
    _PROXY_CACHE_FILE.write_text(json.dumps(proxies, ensure_ascii=False, indent=2), encoding="utf-8")


def get_proxy_pool() -> list[str]:
    with _LOCK:
        return _load_proxy_cache()


def set_proxy_pool(text: str) -> list[str]:
    """从多行文本解析代理池并存入缓存文件。每行一条，自动归一化。"""
    from config.proxy import normalize_proxy
    raw_lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    normalized = [normalize_proxy(line) for line in raw_lines]
    valid = [p for p in normalized if p]
    with _LOCK:
        _save_proxy_cache(valid)
    return valid


def pick_proxy() -> str:
    """从提链代理池随机取一个；空返回 ""（直连）。"""
    import random
    with _LOCK:
        pool = _load_proxy_cache()
    if not pool:
        return ""
    return random.choice(pool)
