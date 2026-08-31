# -*- coding: utf-8 -*-
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from config import proxy as proxy_cfg
from config.trial import (
    TRIAL_PROXY_POOL_NAMES,
    TRIAL_REGION_FIELD_PREFIXES,
    TRIAL_REGIONS,
    account_has_trial_eligibility,
    trial_timezone_offset_min,
)
from core import db
from core.chatgpt_plan import normalize_trial_region, resolve_trial_check_route
from webui import config_editor
from webui.app import _compact_account_for_list, create_app


class TrialRegionRegistryTests(unittest.TestCase):
    def test_registry_contains_all_supported_regions(self):
        self.assertEqual(TRIAL_REGIONS, ("jp", "gb", "de", "br", "th", "ph", "id"))
        self.assertEqual(
            TRIAL_PROXY_POOL_NAMES,
            {region: f"TRIAL_{region.upper()}_PROXY_POOL" for region in TRIAL_REGIONS},
        )
        self.assertEqual(
            TRIAL_REGION_FIELD_PREFIXES,
            {region: f"trial_{region}" for region in TRIAL_REGIONS},
        )
        for region in TRIAL_REGIONS:
            self.assertEqual(normalize_trial_region(region.upper()), region)

    def test_each_region_uses_only_its_dedicated_pool(self):
        patches = [
            patch.object(proxy_cfg, pool_name, [f"{index}.0.0.1:1080"])
            for index, pool_name in enumerate(TRIAL_PROXY_POOL_NAMES.values(), start=1)
        ]
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            for index, region in enumerate(TRIAL_REGIONS, start=1):
                with self.subTest(region=region):
                    route = resolve_trial_check_route(region)
                    self.assertEqual(route["proxy"], f"socks5h://{index}.0.0.1:1080")
                    self.assertEqual(route["proxy_source"], "trial_pool")

    def test_empty_pool_error_names_requested_region_pool(self):
        for region, pool_name in TRIAL_PROXY_POOL_NAMES.items():
            with self.subTest(region=region), patch.object(proxy_cfg, pool_name, []):
                with self.assertRaises(ValueError) as ctx:
                    resolve_trial_check_route(region)
                self.assertIn(pool_name, str(ctx.exception))

    def test_config_editor_exposes_and_normalizes_all_trial_pools(self):
        fields = {field["key"]: field for field in config_editor.EDITABLE_FIELDS}
        for pool_name in TRIAL_PROXY_POOL_NAMES.values():
            with self.subTest(pool=pool_name):
                self.assertIn(pool_name, fields)
                self.assertIn(pool_name, config_editor.EXPLICIT_EMPTY_LIST_KEYS)
                self.assertEqual(
                    config_editor._format_env_value(
                        ["1.2.3.4:1080:user:pass"],
                        "list_str_multiline",
                        pool_name,
                    ),
                    "socks5h://user:pass@1.2.3.4:1080",
                )

        queue_keys = {
            "TRIAL_CHECK_WORKERS",
            "TRIAL_CHECK_QUEUE_LIMIT",
            "TRIAL_CHECK_MIN_INTERVAL",
            "TRIAL_CHECK_JITTER",
        }
        self.assertTrue(queue_keys.issubset(fields))

    def test_any_region_eligibility_and_legacy_fallback(self):
        for region in TRIAL_REGIONS:
            with self.subTest(region=region):
                self.assertTrue(account_has_trial_eligibility({f"trial_{region}_eligible": True}))
        self.assertTrue(account_has_trial_eligibility({"plus_trial_eligible": True}))
        self.assertFalse(account_has_trial_eligibility({"trial_de_eligible": False}))
        self.assertFalse(account_has_trial_eligibility({
            "plus_trial_eligible": True,
            "trial_de_eligible": False,
        }))
        self.assertTrue(account_has_trial_eligibility({
            "plus_trial_eligible": False,
            "trial_br_eligible": False,
            "trial_th_eligible": True,
        }))

    def test_region_timezone_offsets_include_dst(self):
        winter = datetime(2026, 1, 15, 12, 0, 0)
        summer = datetime(2026, 7, 15, 12, 0, 0)
        self.assertEqual(trial_timezone_offset_min("jp", winter), "-540")
        self.assertEqual(trial_timezone_offset_min("th", winter), "-420")
        self.assertEqual(trial_timezone_offset_min("ph", winter), "-480")
        self.assertEqual(trial_timezone_offset_min("br", winter), "180")
        self.assertEqual(trial_timezone_offset_min("gb", winter), "0")
        self.assertEqual(trial_timezone_offset_min("gb", summer), "-60")
        self.assertEqual(trial_timezone_offset_min("de", winter), "-60")
        self.assertEqual(trial_timezone_offset_min("de", summer), "-120")


class TrialRegionDbTests(unittest.TestCase):
    def _db_patches(self, root: Path):
        return (
            patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
            patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"),
            patch.object(db, "_OUTLOOK_JSON", root / "outlook.json"),
            patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
            patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
            patch.object(db, "_OUTLOOK_TXT", root / "outlook.txt"),
            patch.object(db, "_VIEWER_HTML", root / "viewer.html"),
        )

    def test_stale_claim_cannot_overwrite_new_region_query(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with ExitStack() as stack:
                for item in self._db_patches(root):
                    stack.enter_context(item)
                account_id = db.insert_account(email="takeover@example.com", access_token="token")
                self.assertTrue(db.claim_account_trial_check(
                    acc_id=account_id,
                    region="jp",
                    claim_id="old-claim",
                ))
                account = db.get_account(account_id)
                account["trial_check_queued_at"] = "2000-01-01T00:00:00"
                db._save_accounts([account])

                self.assertTrue(db.claim_account_trial_check(
                    acc_id=account_id,
                    region="de",
                    claim_id="new-claim",
                ))
                self.assertFalse(db.mark_account_trial_check_running(
                    account_id,
                    claim_id="old-claim",
                ))
                self.assertTrue(db.mark_account_trial_check_running(
                    account_id,
                    claim_id="new-claim",
                ))
                self.assertFalse(db.update_account_trial_check(
                    acc_id=account_id,
                    claim_id="old-claim",
                    result={
                        "ok": True,
                        "region": "jp",
                        "trial_eligible": True,
                    },
                ))
                self.assertTrue(db.update_account_trial_check(
                    acc_id=account_id,
                    claim_id="new-claim",
                    result={
                        "ok": True,
                        "region": "de",
                        "trial_eligible": False,
                    },
                ))
                final = db.get_account(account_id)
                self.assertNotIn("trial_jp_eligible", final)
                self.assertIs(final["trial_de_eligible"], False)
                self.assertIsNone(final["trial_check_claim_id"])

    def test_region_results_are_independent_and_projected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with ExitStack() as stack:
                for item in self._db_patches(root):
                    stack.enter_context(item)
                account_id = db.insert_account(email="regions@example.com", access_token="token")
                for index, region in enumerate(TRIAL_REGIONS):
                    db.update_account_trial_check(
                        acc_id=account_id,
                        result={
                            "ok": True,
                            "region": region,
                            "checked_at": f"2026-08-21T12:00:0{index}",
                            "trial_eligible": region in {"de", "th"},
                            "campaign_id": f"campaign-{region}",
                            "title": f"Trial {region.upper()}",
                            "discount_percentage": 100,
                            "duration_num_periods": 1,
                            "duration_period": "monthly",
                        },
                    )

                account = db.get_account(account_id)
                snapshot = db.list_account_plan_check_statuses(limit=10)
                item = snapshot["items"][0]
                compact = _compact_account_for_list(account)
                for region in TRIAL_REGIONS:
                    with self.subTest(region=region):
                        expected = region in {"de", "th"}
                        self.assertIs(account[f"trial_{region}_eligible"], expected)
                        self.assertIs(item[f"trial_{region}_eligible"], expected)
                        self.assertEqual(item[f"trial_{region}_campaign_id"], f"campaign-{region}")
                        self.assertIs(compact[f"trial_{region}_eligible"], expected)
                        self.assertEqual(compact[f"trial_{region}_title"], f"Trial {region.upper()}")

    def test_jp_then_other_region_changes_revision_and_preserves_both_results(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with ExitStack() as stack:
                for item in self._db_patches(root):
                    stack.enter_context(item)
                stack.enter_context(patch.object(db, "_now", return_value="2026-08-24T12:00:00"))
                account_id = db.insert_account(email="jp-then-gb@example.com", access_token="token")
                db.update_account_trial_check(
                    acc_id=account_id,
                    result={
                        "ok": True,
                        "region": "jp",
                        "checked_at": "2026-08-24T12:00:00",
                        "trial_eligible": True,
                        "campaign_id": "campaign-jp",
                    },
                )
                jp_snapshot = db.list_account_plan_check_statuses(limit=10)

                self.assertTrue(db.claim_account_trial_check(
                    acc_id=account_id,
                    region="gb",
                    claim_id="gb-claim",
                ))
                queued_snapshot = db.list_account_plan_check_statuses(limit=10)
                self.assertTrue(db.mark_account_trial_check_running(
                    account_id,
                    claim_id="gb-claim",
                ))
                self.assertTrue(db.update_account_trial_check(
                    acc_id=account_id,
                    claim_id="gb-claim",
                    result={
                        "ok": True,
                        "region": "gb",
                        "checked_at": "2026-08-24T12:00:00",
                        "trial_eligible": False,
                    },
                ))
                final_snapshot = db.list_account_plan_check_statuses(limit=10)
                final = final_snapshot["items"][0]

                self.assertNotEqual(jp_snapshot["revision"], queued_snapshot["revision"])
                self.assertNotEqual(queued_snapshot["revision"], final_snapshot["revision"])
                self.assertIs(final["trial_jp_eligible"], True)
                self.assertIs(final["trial_gb_eligible"], False)
                self.assertEqual(final["trial_check_region"], "gb")
                self.assertEqual(final["trial_check_status"], "success")

    def test_missing_false_and_true_remain_distinct(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with ExitStack() as stack:
                for item in self._db_patches(root):
                    stack.enter_context(item)
                account_id = db.insert_account(email="states@example.com", access_token="token")
                db.update_account_trial_check(
                    acc_id=account_id,
                    result={
                        "ok": True,
                        "region": "br",
                        "checked_at": "2026-08-21T12:00:00",
                        "trial_eligible": False,
                    },
                )
                db.update_account_trial_check(
                    acc_id=account_id,
                    result={
                        "ok": True,
                        "region": "th",
                        "checked_at": "2026-08-21T12:00:01",
                        "trial_eligible": True,
                    },
                )
                account = db.get_account(account_id)
                self.assertNotIn("trial_de_eligible", account)
                self.assertIs(account["trial_br_eligible"], False)
                self.assertIs(account["trial_th_eligible"], True)

    def test_revision_changes_when_region_details_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with ExitStack() as stack:
                for item in self._db_patches(root):
                    stack.enter_context(item)
                account_id = db.insert_account(email="revision@example.com", access_token="token")
                db.update_account_trial_check(
                    acc_id=account_id,
                    result={
                        "ok": True,
                        "region": "de",
                        "checked_at": "2026-08-21T12:00:00",
                        "trial_eligible": True,
                        "title": "First title",
                    },
                )
                first = db.list_account_plan_check_statuses(limit=10)["revision"]
                db.update_account_trial_check(
                    acc_id=account_id,
                    result={
                        "ok": True,
                        "region": "de",
                        "checked_at": "2026-08-21T12:00:00",
                        "trial_eligible": True,
                        "title": "Updated title",
                    },
                )
                second = db.list_account_plan_check_statuses(limit=10)["revision"]
                self.assertNotEqual(first, second)


class TrialRegistrationRecheckTests(unittest.TestCase):
    @patch("core.trial_check_service._QUEUE_SLOTS")
    @patch("core.trial_check_service.time.sleep")
    @patch("core.trial_check_service._wait_for_rate_slot")
    @patch("core.trial_check_service._registration_recheck_delay", return_value=1.0)
    @patch("core.trial_check_service.db.update_account_trial_check")
    @patch("core.trial_check_service.db.mark_account_trial_check_running", return_value=True)
    @patch("core.trial_check_service.check_account_trial")
    def test_registration_rechecks_retryable_initial_failure(
        self,
        check_trial,
        mark_running,
        update_trial,
        recheck_delay,
        wait_slot,
        sleep,
        queue_slots,
    ):
        from core import trial_check_service

        check_trial.side_effect = [
            {"ok": False, "region": "jp", "retryable": True, "error": "HTTP 503"},
            {"ok": True, "region": "jp", "current_plan_type": "free", "trial_eligible": True},
        ]
        result = trial_check_service._run_account_trial_check(
            account_id=1,
            email="retry@example.com",
            access_token="token",
            region="jp",
            trigger="registration_auto",
            proxy=None,
            timezone_offset_min="-540",
            claim_id="claim-1",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(check_trial.call_count, 2)
        update_trial.assert_called_once_with(
            acc_id=1,
            result=result,
            claim_id="claim-1",
        )
        queue_slots.release.assert_called_once()

    @patch("core.trial_check_service._QUEUE_SLOTS")
    @patch("core.trial_check_service._wait_for_rate_slot")
    @patch("core.trial_check_service._registration_recheck_delay", return_value=1.0)
    @patch("core.trial_check_service.db.update_account_trial_check")
    @patch("core.trial_check_service.db.mark_account_trial_check_running", return_value=True)
    @patch("core.trial_check_service.check_account_trial")
    def test_registration_does_not_recheck_token_failure(
        self,
        check_trial,
        mark_running,
        update_trial,
        recheck_delay,
        wait_slot,
        queue_slots,
    ):
        from core import trial_check_service

        check_trial.return_value = {
            "ok": False,
            "region": "jp",
            "retryable": False,
            "needs_live_check": True,
            "error": "AT已过期",
        }
        result = trial_check_service._run_account_trial_check(
            account_id=1,
            email="expired@example.com",
            access_token="token",
            region="jp",
            trigger="registration_auto",
            proxy=None,
            timezone_offset_min="-540",
            claim_id="claim-2",
        )
        self.assertFalse(result["ok"])
        check_trial.assert_called_once()
        update_trial.assert_called_once_with(
            acc_id=1,
            result=result,
            claim_id="claim-2",
        )
        queue_slots.release.assert_called_once()


class TrialRegionWebUiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
        self.account = {"id": 7, "email": "query@example.com", "access_token": "token"}

    @patch("webui.app.trial_check_service.enqueue_account_trial_check")
    @patch("webui.app.db.get_account")
    def test_single_api_accepts_all_regions(self, get_account, enqueue_trial):
        get_account.return_value = self.account
        enqueue_trial.return_value = {"accepted": True, "busy": False}
        for region in TRIAL_REGIONS:
            with self.subTest(region=region):
                response = self.client.post(
                    "/api/accounts/check-trial",
                    json={"account_id": 7, "region": region.upper()},
                )
                self.assertEqual(response.status_code, 202)
                self.assertEqual(enqueue_trial.call_args.kwargs["region"], region)

    @patch("webui.app.trial_check_service.enqueue_account_trial_check")
    @patch("webui.app.db.get_account")
    def test_bulk_api_accepts_all_regions(self, get_account, enqueue_trial):
        get_account.return_value = self.account
        enqueue_trial.return_value = {"accepted": True, "busy": False}
        for region in TRIAL_REGIONS:
            with self.subTest(region=region):
                response = self.client.post(
                    "/api/accounts/check-trial-bulk",
                    json={"account_ids": [7], "region": region},
                )
                self.assertEqual(response.status_code, 202)
                self.assertEqual(enqueue_trial.call_args.kwargs["region"], region)

    @patch("webui.app.trial_check_service.enqueue_account_trial_check")
    def test_unknown_region_is_rejected_before_enqueue(self, enqueue_trial):
        single = self.client.post(
            "/api/accounts/check-trial",
            json={"account_id": 7, "region": "us"},
        )
        bulk = self.client.post(
            "/api/accounts/check-trial-bulk",
            json={"account_ids": [7], "region": "us"},
        )
        self.assertEqual(single.status_code, 400)
        self.assertEqual(bulk.status_code, 400)
        enqueue_trial.assert_not_called()

    @patch("webui.app.extract_link_service.enqueue_account_extract")
    @patch("webui.app.db.get_account")
    def test_new_region_eligibility_passes_extract_gate(self, get_account, enqueue_extract):
        enqueue_extract.return_value = {"accepted": True, "busy": False}
        for region in ("de", "br", "th", "ph"):
            with self.subTest(region=region):
                get_account.return_value = {
                    **self.account,
                    "current_plan_type": "free",
                    f"trial_{region}_eligible": True,
                }
                response = self.client.post(
                    "/api/accounts/extract-link",
                    json={"account_id": 7},
                )
                self.assertEqual(response.status_code, 202)

    def test_modern_and_legacy_templates_include_all_region_controls(self):
        root = Path(__file__).resolve().parents[1]
        for template_name in ("index.html", "index_legacy.html"):
            text = (root / "webui" / "templates" / template_name).read_text(encoding="utf-8")
            with self.subTest(template=template_name):
                self.assertIn("function _isTrialEligible(account)", text)
                self.assertIn("values.some(value => value === false)", text)
                self.assertIn("trial_check_region", text)
                for region in TRIAL_REGIONS:
                    self.assertIn(f'data-trial-bulk-region="{region}"', text)
                    self.assertIn(f"trial_${{region}}_eligible", text)
                self.assertIn("function trackTrialChecks(accountIds, region)", text)
                self.assertIn("pollAccountPlanStatuses(forceMerge = false)", text)
                self.assertIn("await pollAccountPlanStatuses(true)", text)
                self.assertIn('class="trial-pills"', text)
                optimistic_marker = "trial_check_status = 'queued'"
                post_marker = "await api('/api/accounts/check-trial'"
                self.assertEqual(text.count(optimistic_marker), 2)
                self.assertLess(text.index(optimistic_marker), text.index(post_marker))


if __name__ == "__main__":
    unittest.main()
