# -*- coding: utf-8 -*-
"""收件来源解析测试：弃用的 cloudflare_domain 历史账号改走 cloudmail 收件。"""
from unittest.mock import patch

from config import email as email_config
from core import db, email_provider


def test_legacy_cloudflare_domain_account_resolves_to_cloudmail():
    db.insert_account(email="legacy@example.test", access_token="tok", email_source="cloudflare_domain")

    assert email_provider.resolve_email_source("legacy@example.test") == "cloudmail"


def test_cloudmail_account_resolves_to_cloudmail_without_pool_record():
    """CloudMail 邮箱不落本地池：重启后必须按账号登记来源路由，而不是兜底到 EMAIL_SOURCE。"""
    with patch.object(email_config, "EMAIL_SOURCE", "remail", create=True):
        db.insert_account(email="cm@example.test", access_token="tok", email_source="cloudmail")

        assert email_provider.resolve_email_source("cm@example.test") == "cloudmail"


def test_pool_membership_takes_precedence_over_account_source():
    db.import_outlook_accounts([{
        "email": "pool@example.test",
        "password": "pwd",
        "client_id": "cid",
        "refresh_token": "rt",
    }])
    db.insert_account(email="pool@example.test", access_token="tok", email_source="cloudflare_domain")

    assert email_provider.resolve_email_source("pool@example.test") == "outlook"


def test_account_with_other_source_keeps_fallback_resolution():
    db.insert_account(email="plain@example.test", access_token="tok", email_source="remail")

    with patch.object(email_config, "EMAIL_SOURCE", "outlook,generic_api", create=True):
        assert email_provider.resolve_email_source("plain@example.test") == "outlook"


def test_unknown_email_falls_back_to_first_configured_source():
    with patch.object(email_config, "EMAIL_SOURCE", "generic_api", create=True):
        assert email_provider.resolve_email_source("unknown@example.test") == "generic_api"
