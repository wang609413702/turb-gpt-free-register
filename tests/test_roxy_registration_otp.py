# -*- coding: utf-8 -*-
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core import roxy_registration


class RoxyEmailOtpReadyTests(unittest.TestCase):
    def test_input_page_attempts_resend_before_waiting_for_otp(self):
        driver = SimpleNamespace()
        page_state = {"url": "https://auth.openai.com/email-verification", "inputs": []}
        calls = {"resend": 0}

        def fake_resend(target_driver, timeout=20):
            self.assertIs(target_driver, driver)
            self.assertEqual(timeout, 4)
            calls["resend"] += 1
            setattr(target_driver, "_registration_otp_trigger_ts", 1234.0)
            return {"ok": True, "text": "Resend"}

        with patch.object(roxy_registration, "_has_access_token", return_value=False), \
             patch.object(roxy_registration, "_email_otp_input_present", return_value=True), \
             patch.object(roxy_registration, "_email_otp_page_state", return_value=page_state), \
             patch.object(roxy_registration, "_click_resend_email_otp", side_effect=fake_resend), \
             patch.object(roxy_registration, "_install_email_otp_send_probe"), \
             patch.object(roxy_registration, "_log_email_otp_send_probe", return_value=[{"url": "https://auth.openai.com/api/accounts/email-otp/send", "status": 200}]), \
             patch.object(roxy_registration, "_request_email_otp_send_via_browser") as direct_send, \
             patch.object(roxy_registration.time, "time", return_value=1000.0), \
             patch.object(roxy_registration.time, "sleep"):
            after_ts = roxy_registration._ensure_email_otp_ready(driver, "user@example.com", timeout=35)

        self.assertEqual(after_ts, 1234.0)
        self.assertEqual(calls["resend"], 1)
        direct_send.assert_not_called()

    def test_input_page_without_observed_send_request_calls_send_endpoint(self):
        driver = SimpleNamespace()
        setattr(driver, "_registration_otp_trigger_ts", 2222.0)
        page_state = {"url": "https://auth.openai.com/email-verification", "inputs": []}
        direct_calls = {"count": 0}

        def fake_direct_send(target_driver, reason=""):
            self.assertIs(target_driver, driver)
            self.assertEqual(reason, "otp_ready_without_send_request")
            direct_calls["count"] += 1
            setattr(target_driver, "_registration_otp_trigger_ts", 3333.0)
            return {"ok": True, "status": 200}

        with patch.object(roxy_registration, "_has_access_token", return_value=False), \
             patch.object(roxy_registration, "_email_otp_input_present", return_value=True), \
             patch.object(roxy_registration, "_email_otp_page_state", return_value=page_state), \
             patch.object(roxy_registration, "_click_resend_email_otp", side_effect=RuntimeError("missing resend")), \
             patch.object(roxy_registration, "_install_email_otp_send_probe"), \
             patch.object(roxy_registration, "_log_email_otp_send_probe", return_value=[]), \
             patch.object(roxy_registration, "_request_email_otp_send_via_browser", side_effect=fake_direct_send), \
             patch.object(roxy_registration.time, "time", return_value=1000.0), \
             patch.object(roxy_registration.time, "sleep"):
            after_ts = roxy_registration._ensure_email_otp_ready(driver, "user@example.com", timeout=35)

        self.assertEqual(after_ts, 3333.0)
        self.assertEqual(direct_calls["count"], 1)

    def test_preferred_after_ts_is_kept_when_direct_send_is_later(self):
        driver = SimpleNamespace()
        setattr(driver, "_registration_otp_trigger_ts", 2222.0)
        page_state = {"url": "https://auth.openai.com/email-verification", "inputs": []}

        def fake_direct_send(target_driver, reason=""):
            setattr(target_driver, "_registration_otp_trigger_ts", 3333.0)
            return {"ok": True, "status": 200}

        with patch.object(roxy_registration, "_has_access_token", return_value=False), \
             patch.object(roxy_registration, "_email_otp_input_present", return_value=True), \
             patch.object(roxy_registration, "_email_otp_page_state", return_value=page_state), \
             patch.object(roxy_registration, "_click_resend_email_otp", side_effect=RuntimeError("missing resend")), \
             patch.object(roxy_registration, "_install_email_otp_send_probe"), \
             patch.object(roxy_registration, "_log_email_otp_send_probe", return_value=[]), \
             patch.object(roxy_registration, "_request_email_otp_send_via_browser", side_effect=fake_direct_send), \
             patch.object(roxy_registration.time, "time", return_value=1000.0), \
             patch.object(roxy_registration.time, "sleep"):
            after_ts = roxy_registration._ensure_email_otp_ready(
                driver,
                "user@example.com",
                timeout=35,
                prefer_after_ts=1111.0,
            )

        self.assertEqual(after_ts, 1111.0)

    def test_restart_email_otp_flow_resubmits_email_and_uses_restart_trigger_ts(self):
        driver = SimpleNamespace()
        email = "user@example.com"

        def fake_submit(target_driver, target_email, attempts=3):
            self.assertIs(target_driver, driver)
            self.assertEqual(target_email, email)
            self.assertEqual(attempts, 2)
            setattr(target_driver, "_registration_otp_trigger_ts", 1234.0)
            return "password"

        def fake_ensure(target_driver, target_email, timeout=35, prefer_after_ts=None):
            self.assertIs(target_driver, driver)
            self.assertEqual(target_email, email)
            self.assertEqual(timeout, 35)
            self.assertEqual(prefer_after_ts, 1234.0)
            return 1234.0

        with patch.object(roxy_registration, "_check_manual_stop"), \
             patch.object(roxy_registration, "_safe_get") as safe_get, \
             patch.object(roxy_registration, "human_delay"), \
             patch.object(roxy_registration, "_install_email_otp_send_probe"), \
             patch.object(roxy_registration, "_page_warmup"), \
             patch.object(roxy_registration, "_maybe_accept"), \
             patch.object(roxy_registration, "_submit_email_and_wait_next", side_effect=fake_submit), \
             patch.object(roxy_registration, "_fill_password_page_if_present", return_value="new-password"), \
             patch.object(roxy_registration, "_ensure_email_otp_ready", side_effect=fake_ensure), \
             patch.object(roxy_registration.time, "time", return_value=1000.0):
            after_ts, password = roxy_registration._restart_email_otp_flow(
                driver,
                email,
                reason="test restart",
            )

        self.assertEqual(after_ts, 1234.0)
        self.assertEqual(password, "new-password")
        safe_get.assert_called_once()

    def test_passwordless_send_otp_probe_counts_as_send_success(self):
        rows = [{"url": "https://auth.openai.com/u/signup?intent=passwordless_signup_send_otp", "status": 302}]

        self.assertTrue(roxy_registration._email_otp_send_has_success(rows))


if __name__ == "__main__":
    unittest.main()
