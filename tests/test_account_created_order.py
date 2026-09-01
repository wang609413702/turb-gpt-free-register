# -*- coding: utf-8 -*-
from core import db


def test_account_list_is_sorted_by_creation_time_descending():
    first_id = db.insert_account(email="older@example.test", access_token="older-token")
    second_id = db.insert_account(email="newer@example.test", access_token="newer-token")
    rows = db._load_accounts()
    for row in rows:
        row["created_at"] = "2026-01-01T00:00:00" if row["id"] == first_id else "2026-02-01T00:00:00"
    db._save_accounts(rows)

    listed = db.list_accounts(limit=10)

    assert [row["id"] for row in listed] == [second_id, first_id]
