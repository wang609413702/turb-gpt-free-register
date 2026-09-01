# -*- coding: utf-8 -*-
from core import db


def test_account_update_preserves_rows_omitted_from_save_snapshot():
    first_id = db.insert_account(email="first@example.test", access_token="first-token")
    second_id = db.insert_account(email="second@example.test", access_token="second-token")

    first = db.get_account(first_id)
    first["note"] = "updated"
    db._save_accounts([first])

    assert db.get_account(first_id)["note"] == "updated"
    assert db.get_account(second_id)["access_token"] == "second-token"
    assert db.count_accounts() == 2


def test_bulk_delete_removes_only_requested_accounts():
    first_id = db.insert_account(email="first@example.test", access_token="first-token")
    second_id = db.insert_account(email="second@example.test", access_token="second-token")

    deleted, skipped = db.delete_accounts(account_ids=[first_id, 999])

    assert deleted == [{"id": first_id, "email": "first@example.test"}]
    assert skipped == [{"id": 999, "reason": "账号不存在"}]
    assert db.get_account(first_id) is None
    assert db.get_account(second_id)["access_token"] == "second-token"
