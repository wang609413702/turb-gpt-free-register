# -*- coding: utf-8 -*-
import json
import sqlite3
from pathlib import Path

from tools import recover_historic_accounts as recovery


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _create_database(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY,
                email TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT '',
                archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO accounts VALUES(1,'live@example.test','success',0,'now','now','{"
            "\"id\":1,\"email\":\"live@example.test\",\"access_token\":\"live\"}')"
        )


def test_recovery_builds_shared_and_batch_only_records(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery, "PROJECT_ROOT", tmp_path)
    export = tmp_path / "export.json"
    batches = tmp_path / "accounts"
    _write_json(export, [{"id": 20, "email": "shared@example.test", "access_token": "export-token"}])
    _write_json(
        batches / "20260101-1" / "注册成功账号.json",
        [{"id": 20, "email": "shared@example.test", "access_token": "batch-token", "row": {"email": "shared@example.test", "access_token": "batch-token", "created_at": "2026-01-01"}}],
    )
    _write_json(
        batches / "20260102-1" / "注册成功账号.json",
        [{"id": 20, "email": "batch-only@example.test", "access_token": "batch-only", "row": {"email": "batch-only@example.test", "access_token": "batch-only"}}],
    )

    records = recovery.build_recovery_set(export, batches)

    assert [record.email for record in records] == ["batch-only@example.test", "shared@example.test"]
    shared = next(record for record in records if record.email == "shared@example.test")
    assert shared.payload["access_token"] == "export-token"
    assert shared.payload["recovery_provenance"]["legacy_batch_id"] == 20
    assert recovery.inspect_recovery(records)["batch_only_count"] == 1


def test_recovery_apply_preserves_live_rows_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery, "PROJECT_ROOT", tmp_path)
    export = tmp_path / "export.json"
    batches = tmp_path / "accounts"
    database = tmp_path / "turb.sqlite3"
    _create_database(database)
    _write_json(export, [{"id": 20, "email": "shared@example.test", "access_token": "export-token"}])
    _write_json(
        batches / "20260101-1" / "注册成功账号.json",
        [{"id": 20, "email": "shared@example.test", "access_token": "batch-token", "row": {"email": "shared@example.test", "access_token": "batch-token"}}],
    )
    _write_json(
        batches / "20260102-1" / "注册成功账号.json",
        [{"id": 20, "email": "batch-only@example.test", "access_token": "batch-only", "row": {"email": "batch-only@example.test", "access_token": "batch-only"}}],
    )
    records = recovery.build_recovery_set(export, batches)

    first = recovery.apply_recovery(database, records)
    second = recovery.apply_recovery(database, records)

    assert first["inserted_count"] == 2
    assert first["skipped_existing_count"] == 0
    assert second["already_applied"] == 1
    with sqlite3.connect(database) as conn:
        accounts = conn.execute("SELECT id,email,payload FROM accounts ORDER BY id").fetchall()
        assert [row[1] for row in accounts] == ["live@example.test", "batch-only@example.test", "shared@example.test"]
        assert json.loads(accounts[0][2])["access_token"] == "live"
        assert conn.execute("SELECT COUNT(*) FROM account_recovery_provenance").fetchone()[0] == 2
