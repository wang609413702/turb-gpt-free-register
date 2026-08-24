# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from webui.app import create_app


class PlanTrialWebUiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
        self.account = {"id": 7, "email": "query@example.com", "access_token": "token"}

    @patch("webui.app.trial_check_service.enqueue_account_trial_check")
    @patch("webui.app.db.get_account")
    def test_bulk_trial_uses_region_default_timezone(self, get_account, enqueue_trial):
        get_account.return_value = self.account
        enqueue_trial.return_value = {"accepted": True, "busy": False}
        expected_offsets = {"jp": "-540", "br": "180", "th": "-420", "ph": "-480"}

        for region, expected_offset in expected_offsets.items():
            with self.subTest(region=region):
                response = self.client.post(
                    "/api/accounts/check-trial-bulk",
                    json={"account_ids": [7], "region": region},
                )

                self.assertEqual(response.status_code, 202)
                self.assertEqual(enqueue_trial.call_args.kwargs["region"], region)
                self.assertEqual(
                    enqueue_trial.call_args.kwargs["timezone_offset_min"],
                    expected_offset,
                )

    @patch("webui.app.plan_check_service.enqueue_account_plan_check")
    @patch("webui.app.db.get_account")
    def test_single_plan_returns_conflict_when_token_needs_refresh(self, get_account, enqueue_plan):
        get_account.return_value = self.account
        enqueue_plan.return_value = {
            "accepted": False,
            "busy": False,
            "needs_live_check": True,
            "error": "AT已过期/失效，请先查活刷新后再查询套餐",
        }

        response = self.client.post("/api/accounts/check-plan", json={"account_id": 7})

        self.assertEqual(response.status_code, 409)
        payload = response.get_json()
        self.assertTrue(payload["needs_live_check"])
        self.assertIn("查活刷新", payload["error"])


if __name__ == "__main__":
    unittest.main()
