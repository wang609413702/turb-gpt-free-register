# -*- coding: utf-8 -*-
import unittest
from pathlib import Path
from unittest.mock import patch

from core.account_export import save_account_data


class SaveAccountPlanResultTests(unittest.TestCase):
    def test_browser_plan_result_is_saved_without_http_plan_enqueue(self):
        plan_result = {
            "ok": True,
            "checked_at": "2026-08-20T18:00:00",
            "http_status": 200,
            "current_plan_type": "free",
            "browser_context": True,
            "network_route": "browser",
            "proxy_mode": "browser",
        }
        with patch("core.db.insert_account", return_value=17), \
             patch("core.account_export._append_batch_archive", return_value=Path("accounts/test")), \
             patch("core.db.update_account_plan_check", return_value=True) as update_plan, \
             patch("core.plan_check_service.enqueue_account_plan_check") as enqueue_plan, \
             patch("core.trial_check_service.enqueue_account_trial_check", return_value={"accepted": True}):
            account_id = save_account_data(
                email="plan@example.com",
                access_token="token",
                extra={"user": {}, "account": {}},
                plan_result=plan_result,
            )

        self.assertEqual(account_id, 17)
        update_plan.assert_called_once_with(acc_id=17, result=plan_result)
        enqueue_plan.assert_not_called()

    def test_retryable_browser_failure_falls_back_to_http_queue(self):
        plan_result = {
            "ok": False,
            "checked_at": "2026-08-20T18:00:00",
            "error": "浏览器套餐请求失败: TimeoutException",
            "retryable": True,
            "browser_context": True,
        }
        with patch("core.db.insert_account", return_value=18), \
             patch("core.account_export._append_batch_archive", return_value=Path("accounts/test")), \
             patch("core.db.update_account_plan_check", return_value=True) as update_plan, \
             patch(
                 "core.plan_check_service.enqueue_account_plan_check",
                 return_value={"accepted": True, "busy": False},
             ) as enqueue_plan, \
             patch("core.trial_check_service.enqueue_account_trial_check", return_value={"accepted": True}):
            account_id = save_account_data(
                email="fallback@example.com",
                access_token="token",
                extra={"user": {}, "account": {}},
                plan_result=plan_result,
            )

        self.assertEqual(account_id, 18)
        update_plan.assert_not_called()
        enqueue_plan.assert_called_once_with(
            account_id=18,
            email="fallback@example.com",
            access_token="token",
            trigger="registration_browser_fallback",
        )

    def test_non_retryable_browser_failure_is_saved_without_fallback(self):
        plan_result = {
            "ok": False,
            "checked_at": "2026-08-20T18:00:00",
            "http_status": 401,
            "error": "AT已过期/失效，请手动查活刷新",
            "retryable": False,
            "needs_live_check": True,
            "browser_context": True,
        }
        with patch("core.db.insert_account", return_value=19), \
             patch("core.account_export._append_batch_archive", return_value=Path("accounts/test")), \
             patch("core.db.update_account_plan_check", return_value=True) as update_plan, \
             patch("core.plan_check_service.enqueue_account_plan_check") as enqueue_plan, \
             patch("core.trial_check_service.enqueue_account_trial_check", return_value={"accepted": True}):
            save_account_data(
                email="expired@example.com",
                access_token="token",
                extra={"user": {}, "account": {}},
                plan_result=plan_result,
            )

        update_plan.assert_called_once_with(acc_id=19, result=plan_result)
        enqueue_plan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
