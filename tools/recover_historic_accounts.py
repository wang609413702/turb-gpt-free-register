# -*- coding: utf-8 -*-
"""Recover historic account archives into a SQLite account store.

The tool is intentionally explicit: without ``--apply`` it only validates sources and
writes a redacted report. Applying the same source manifest twice is idempotent.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXPORT = PROJECT_ROOT / "注册成功的邮箱.json"
DEFAULT_BATCH_DIR = PROJECT_ROOT / "accounts"


class RecoveryError(RuntimeError):
    """Raised when source archives are incomplete or internally inconsistent."""


@dataclass(frozen=True)
class SourceRecord:
    email: str
    payload: dict[str, Any]
    source_key: str
    source_digest: str
    source_paths: tuple[str, ...]
    shared_export: bool


def _normalized_email(value: object) -> str:
    email = str(value or "").strip().lower()
    if not email or "@" not in email or len(email) > 320:
        raise RecoveryError("发现缺少或无效邮箱的恢复记录")
    return email


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _read_export(path: Path) -> dict[str, dict[str, Any]]:
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryError(f"无法读取汇总导出: {path}") from exc
    if not isinstance(records, list):
        raise RecoveryError("汇总导出必须是 JSON 数组")

    result: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            raise RecoveryError("汇总导出包含非对象记录")
        email = _normalized_email(record.get("email"))
        if email in result:
            raise RecoveryError("汇总导出包含重复邮箱")
        item = dict(record)
        item["email"] = email
        result[email] = item
    return result


def _read_batch_archives(directory: Path) -> dict[str, tuple[dict[str, Any], Path]]:
    result: dict[str, tuple[dict[str, Any], Path]] = {}
    paths = sorted(directory.glob("*/注册成功账号.json"))
    if not paths:
        raise RecoveryError(f"未找到批次账号档案: {directory}")
    for path in paths:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecoveryError(f"无法读取批次档案: {path}") from exc
        records = document if isinstance(document, list) else [document]
        if len(records) != 1 or not isinstance(records[0], dict):
            raise RecoveryError(f"批次档案必须包含恰好一条账号记录: {path}")
        envelope = records[0]
        row = envelope.get("row")
        row = dict(row) if isinstance(row, dict) else {}
        candidate = dict(row)
        for key, value in envelope.items():
            if key != "row" and (_is_blank(candidate.get(key)) or key not in candidate):
                candidate[key] = value
        email = _normalized_email(envelope.get("email") or candidate.get("email"))
        nested_email = candidate.get("email")
        if nested_email and _normalized_email(nested_email) != email:
            raise RecoveryError(f"批次档案的嵌套邮箱不一致: {path}")
        if not str(candidate.get("access_token") or "").strip():
            raise RecoveryError(f"批次档案缺少 access_token: {path}")
        if email in result:
            raise RecoveryError("批次档案包含重复邮箱")
        candidate["email"] = email
        result[email] = (candidate, path)
    return result


def _merge_records(
    exports: dict[str, dict[str, Any]],
    batches: dict[str, tuple[dict[str, Any], Path]],
) -> list[SourceRecord]:
    if not set(exports).issubset(batches):
        raise RecoveryError("汇总导出存在未找到对应批次档案的账号")

    records: list[SourceRecord] = []
    for email in sorted(batches):
        batch, batch_path = batches[email]
        export = exports.get(email)
        payload = dict(export) if export else dict(batch)
        if export:
            for key, value in batch.items():
                if _is_blank(payload.get(key)):
                    payload[key] = value
        payload["email"] = email
        if not str(payload.get("access_token") or "").strip():
            raise RecoveryError("恢复记录缺少 access_token")
        provenance = {
            "export": bool(export),
            "batch_path": str(batch_path.relative_to(PROJECT_ROOT)),
            "batch_digest": _digest(batch),
            "legacy_export_id": export.get("id") if export else None,
            "legacy_batch_id": batch.get("id"),
        }
        payload["recovery_provenance"] = provenance
        source_key = hashlib.sha256(email.encode("utf-8")).hexdigest()
        records.append(
            SourceRecord(
                email=email,
                payload=payload,
                source_key=source_key,
                source_digest=_digest(payload),
                source_paths=(str(batch_path.relative_to(PROJECT_ROOT)),),
                shared_export=export is not None,
            )
        )
    return records


def build_recovery_set(export_path: Path = DEFAULT_EXPORT, batch_dir: Path = DEFAULT_BATCH_DIR) -> list[SourceRecord]:
    return _merge_records(_read_export(export_path), _read_batch_archives(batch_dir))


def _manifest_hash(records: list[SourceRecord]) -> str:
    return _digest([
        {"source_key": record.source_key, "source_digest": record.source_digest}
        for record in records
    ])


def _ensure_audit_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS recovery_runs (
            manifest_sha256 TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL,
            source_count INTEGER NOT NULL,
            inserted_count INTEGER NOT NULL,
            skipped_existing_count INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS account_recovery_provenance (
            source_key TEXT PRIMARY KEY,
            account_id INTEGER NOT NULL,
            source_digest TEXT NOT NULL,
            manifest_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )


def _account_values(account_id: int, record: SourceRecord) -> tuple[object, ...]:
    payload = dict(record.payload)
    payload["id"] = account_id
    payload["email"] = record.email
    created_at = str(payload.get("created_at") or payload.get("imported_at") or "")
    updated_at = str(payload.get("updated_at") or created_at)
    return (
        account_id,
        record.email,
        str(payload.get("status") or "success"),
        int(bool(payload.get("archived"))),
        created_at,
        updated_at,
        _canonical_json(payload),
    )


def inspect_recovery(
    records: list[SourceRecord], database_path: Path | None = None,
) -> dict[str, int | str]:
    shared = sum(record.shared_export for record in records)
    report: dict[str, int | str] = {
        "candidate_count": len(records),
        "shared_export_count": shared,
        "batch_only_count": len(records) - shared,
        "manifest_sha256": _manifest_hash(records),
    }
    if database_path and database_path.exists():
        with sqlite3.connect(database_path) as conn:
            live_emails = {str(row[0]).strip().lower() for row in conn.execute("SELECT email FROM accounts")}
        report["existing_live_count"] = len(live_emails)
        report["would_insert_count"] = sum(record.email not in live_emails for record in records)
        report["would_skip_existing_count"] = sum(record.email in live_emails for record in records)
    return report


def apply_recovery(database_path: Path, records: list[SourceRecord]) -> dict[str, int | str]:
    if not database_path.exists():
        raise RecoveryError(f"目标数据库不存在: {database_path}")
    manifest = _manifest_hash(records)
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(database_path, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        _ensure_audit_schema(conn)
        prior = conn.execute(
            "SELECT inserted_count, skipped_existing_count FROM recovery_runs WHERE manifest_sha256=?",
            (manifest,),
        ).fetchone()
        if prior:
            conn.rollback()
            return {
                "candidate_count": len(records),
                "inserted_count": int(prior[0]),
                "skipped_existing_count": int(prior[1]),
                "already_applied": 1,
                "manifest_sha256": manifest,
            }

        existing = {
            str(row[0]).strip().lower()
            for row in conn.execute("SELECT email FROM accounts")
        }
        next_id = int(conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM accounts").fetchone()[0])
        inserted = 0
        skipped = 0
        for record in records:
            if record.email in existing:
                skipped += 1
                continue
            account_id = next_id
            next_id += 1
            conn.execute(
                "INSERT INTO accounts(id,email,status,archived,created_at,updated_at,payload) VALUES(?,?,?,?,?,?,?)",
                _account_values(account_id, record),
            )
            conn.execute(
                "INSERT INTO account_recovery_provenance(source_key,account_id,source_digest,manifest_sha256,created_at) VALUES(?,?,?,?,?)",
                (record.source_key, account_id, record.source_digest, manifest, now),
            )
            inserted += 1

        expected_total = len(existing) + inserted
        actual_total = int(conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])
        if actual_total != expected_total:
            raise RecoveryError("恢复后账号计数校验失败")
        invalid_payloads = conn.execute(
            "SELECT COUNT(*) FROM accounts WHERE json_valid(payload)=0"
        ).fetchone()[0]
        if invalid_payloads:
            raise RecoveryError("恢复后发现无效账号 payload")
        conn.execute(
            "INSERT INTO recovery_runs(manifest_sha256,applied_at,source_count,inserted_count,skipped_existing_count) VALUES(?,?,?,?,?)",
            (manifest, now, len(records), inserted, skipped),
        )
        conn.commit()
        return {
            "candidate_count": len(records),
            "inserted_count": inserted,
            "skipped_existing_count": skipped,
            "already_applied": 0,
            "manifest_sha256": manifest,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="恢复历史账号批次档案")
    parser.add_argument("--database", type=Path, default=PROJECT_ROOT / "turb.sqlite3")
    parser.add_argument("--export", type=Path, default=DEFAULT_EXPORT)
    parser.add_argument("--batch-dir", type=Path, default=DEFAULT_BATCH_DIR)
    parser.add_argument("--report", type=Path, required=True, help="输出不含账号内容的审计报告 JSON")
    parser.add_argument("--apply", action="store_true", help="在指定 SQLite 数据库中执行恢复")
    parser.add_argument("--expect-count", type=int, default=666)
    args = parser.parse_args(argv)

    try:
        records = build_recovery_set(args.export, args.batch_dir)
        if len(records) != args.expect_count:
            raise RecoveryError(f"恢复候选数量异常: {len(records)} != {args.expect_count}")
        report = inspect_recovery(records, args.database)
        report["mode"] = "apply" if args.apply else "dry_run"
        if args.apply:
            report.update(apply_recovery(args.database, records))
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except RecoveryError as exc:
        print(f"恢复失败: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
