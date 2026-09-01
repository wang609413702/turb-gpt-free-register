# -*- coding: utf-8 -*-
"""pytest 全局隔离夹具。

所有测试使用独立 SQLite 与迁移来源，避免后台任务、WebUI 初始化或邮箱复用
逻辑访问项目根目录中的真实账号数据和 Remail 订单队列。
"""
from contextlib import ExitStack
from unittest.mock import patch

import pytest

from core import db, remail_client


@pytest.fixture(autouse=True)
def _isolate_runtime_storage(tmp_path):
    database_path = tmp_path / "turb.sqlite3"
    missing_source = tmp_path / "missing"
    missing_source.mkdir()
    with ExitStack() as stack:
        for patcher in (
            patch.object(remail_client, "_REUSE_QUEUE_PATH", tmp_path / "remail_reuse_queue.json"),
            patch.object(db, "_SQLITE_PATH", database_path),
            patch.object(db, "_DEFAULT_SQLITE_PATH", database_path),
            patch.object(db, "_ACCOUNTS_JSON", missing_source / "accounts.json"),
            patch.object(db, "_OUTLOOK_JSON", missing_source / "outlook.json"),
            patch.object(db, "_JOBS_JSON", missing_source / "jobs.json"),
            patch.object(db, "_LEGACY_SQLITE", missing_source / "registrations.db"),
            patch.object(db, "_LEGACY_OUTLOOK_JSON", missing_source / "outlook_accounts.json"),
            patch.object(db, "_LEGACY_ACCOUNTS_JSON", missing_source / "registered_accounts.json"),
            patch.object(db, "_LEGACY_JOBS_JSON", missing_source / "registration_jobs.json"),
            patch.object(db, "_GENERIC_API_EMAIL_JSON", missing_source / "generic_api_emails.json"),
            patch.object(db, "_DOMAIN_EMAIL_JSON", missing_source / "domain_emails.json"),
            patch.object(db, "_REBIND_EMAIL_JSON", missing_source / "rebind_emails.json"),
            patch.object(db, "_LEGACY_CODEX_EXPORT_STATE", missing_source / "codex_export_state.json"),
            patch.object(db, "_CODEX_DIR", missing_source / "codex_accounts"),
            patch.object(db, "_CODEX_AGENT_DIR", missing_source / "codex_agent_accounts"),
            patch.object(db, "_SQLITE_READY", False),
            patch.object(db, "_SQLITE_READY_PATH", None),
        ):
            stack.enter_context(patcher)
        yield
