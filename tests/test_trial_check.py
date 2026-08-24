# -*- coding: utf-8 -*-
import base64
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from core import db
from core.chatgpt_plan import (
    check_account_trial,
    normalize_trial_region,
    parse_accounts_check,
    parse_accounts_trial,
    resolve_trial_check_route,
)
from config import proxy as proxy_cfg


def _accounts_payload(*, plan_type="free", subscription_plan="chatgptfreeplan", plus_campaign=True):
    item = {
        "account": {"account_id": "acc_1", "plan_type": plan_type},
        "entitlement": {
            "subscription_plan": subscription_plan,
            "has_active_subscription": False,
            "billing_period": "monthly",
            "billing_currency": "usd",
        },
        "eligible_offers": {"offers": [{"id": "offer_1"}]},
        "can_access_with_session": True,
        "features": [1, 2],
    }
    if plus_campaign:
        item["eligible_promo_campaigns"] = {
            "plus": {
                "id": "camp_jp_1",
                "metadata": {
                    "title": "1 Month of Plus free",
                    "summary": "Try Plus free for a month",
                    "discount": {"percentage": 100},
                    "duration": {"num_periods": 1, "period": "monthly"},
                    "promotion_type_label": "试用",
                },
            }
        }
    else:
        item["eligible_promo_campaigns"] = {}
    return {"accounts": {"acc_1": item, "default": dict(item)}}


class ParseAccountsTrialTests(unittest.TestCase):
    def test_eligible_free_account_with_plus_campaign(self):
        result = parse_accounts_trial(_accounts_payload())
        self.assertTrue(result["ok"])
        self.assertTrue(result["trial_eligible"])
        self.assertEqual(result["campaign_id"], "camp_jp_1")
        self.assertEqual(result["title"], "1 Month of Plus free")
        self.assertEqual(result["discount_percentage"], 100)
        self.assertEqual(result["duration_num_periods"], 1)
        self.assertEqual(result["duration_period"], "monthly")
        self.assertEqual(result["current_plan_type"], "free")

    def test_free_account_without_plus_campaign(self):
        result = parse_accounts_trial(_accounts_payload(plus_campaign=False))
        self.assertTrue(result["ok"])
        self.assertFalse(result["trial_eligible"])
        self.assertIsNone(result["campaign_id"])

    def test_non_free_account_with_campaign_not_eligible(self):
        result = parse_accounts_trial(_accounts_payload(plan_type="plus", subscription_plan="chatgptplusplan"))
        self.assertTrue(result["ok"])
        self.assertFalse(result["trial_eligible"])

    def test_missing_accounts_object_raises(self):
        with self.assertRaises(ValueError):
            parse_accounts_trial({"other": 1})


class ParseAccountsCheckTests(unittest.TestCase):
    def test_plan_parse_returns_no_trial_fields(self):
        result = parse_accounts_check(_accounts_payload())
        self.assertTrue(result["ok"])
        self.assertEqual(result["current_plan_type"], "free")
        self.assertEqual(result["billing_currency"], "usd")
        self.assertEqual(result["eligible_offer_ids"], ["offer_1"])
        for key in ("plus_trial_eligible", "plus_trial_campaign_id", "plus_trial_title"):
            self.assertNotIn(key, result)


class NormalizeTrialRegionTests(unittest.TestCase):
    def test_valid_regions(self):
        self.assertEqual(normalize_trial_region("JP"), "jp")
        self.assertEqual(normalize_trial_region(" gb "), "gb")

    def test_invalid_region_raises(self):
        with self.assertRaises(ValueError):
            normalize_trial_region("us")


class ResolveTrialCheckRouteTests(unittest.TestCase):
    def test_unknown_region_raises(self):
        with self.assertRaises(ValueError):
            resolve_trial_check_route("us")

    def test_explicit_proxy_wins(self):
        route = resolve_trial_check_route("jp", "socks5://1.2.3.4:1080")
        self.assertEqual(route["proxy"], "socks5://1.2.3.4:1080")
        self.assertEqual(route["proxy_source"], "request")

    def test_empty_explicit_proxy_raises(self):
        with self.assertRaises(ValueError):
            resolve_trial_check_route("jp", "")

    def test_empty_pool_raises(self):
        with patch.object(proxy_cfg, "TRIAL_JP_PROXY_POOL", []):
            with self.assertRaises(ValueError) as ctx:
                resolve_trial_check_route("jp")
        self.assertIn("TRIAL_JP_PROXY_POOL", str(ctx.exception))

    def test_pool_pick_normalized_and_masked(self):
        with patch.object(proxy_cfg, "TRIAL_JP_PROXY_POOL", ["1.2.3.4:1080"]):
            route = resolve_trial_check_route("jp")
        self.assertEqual(route["proxy"], "socks5h://1.2.3.4:1080")
        self.assertEqual(route["proxy_source"], "trial_pool")
        self.assertEqual(route["proxy_used"], "socks5h://1.2.3.4:1080")

    def test_gb_pool_isolation(self):
        with patch.object(proxy_cfg, "TRIAL_JP_PROXY_POOL", ["1.1.1.1:1080"]), \
             patch.object(proxy_cfg, "TRIAL_GB_PROXY_POOL", []):
            with self.assertRaises(ValueError):
                resolve_trial_check_route("gb")


class CheckAccountTrialTests(unittest.TestCase):
    def test_empty_token(self):
        result = check_account_trial("", "jp")
        self.assertFalse(result["ok"])
        self.assertIn("token 为空", result["error"])

    def test_enqueue_rejects_expired_token_before_claiming_db(self):
        from core import trial_check_service

        payload = base64.urlsafe_b64encode(json.dumps({"exp": 1}).encode()).decode().rstrip("=")
        token = f"e30.{payload}.sig"
        with patch.object(trial_check_service.db, "claim_account_trial_check") as claim:
            result = trial_check_service.enqueue_account_trial_check(
                account_id=1,
                email="expired@example.com",
                access_token=token,
                region="jp",
                trigger="manual",
            )
        self.assertFalse(result["accepted"])
        self.assertTrue(result["needs_live_check"])
        self.assertIn("查活刷新", result["error"])
        claim.assert_not_called()

    def test_empty_pool_error_without_network(self):
        with patch.object(proxy_cfg, "TRIAL_JP_PROXY_POOL", []):
            result = check_account_trial("fake.token.here", "jp")
        self.assertFalse(result["ok"])
        self.assertIn("试用资格查询配置错误", result["error"])

    def test_invalid_region_error_without_network(self):
        result = check_account_trial("fake.token.here", "us")
        self.assertFalse(result["ok"])
        self.assertIn("试用资格查询配置错误", result["error"])


class _FakeResp:
    def __init__(self, status_code: int, text: str = "", headers: dict | None = None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class _FakeHttpSession:
    def __init__(self, status_code: int, text: str = "", headers: dict | None = None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.calls = 0
        self.requests = []

    def get(self, *args, **kwargs):
        self.calls += 1
        self.requests.append((args, kwargs))
        return _FakeResp(self.status_code, self.text, self.headers)

    def close(self):
        pass


class _FakeBrowserSession:
    def __init__(self, status_code: int, text: str = "", headers: dict | None = None):
        self.session = _FakeHttpSession(status_code, text, headers)
        self.device_id = "dev"
        self.oai_session_id = "session-1"

    def _get_common_headers(self):
        return {"user-agent": "test-ua"}

    def get_chatgpt_headers(self, referer="https://chatgpt.com/"):
        return {
            "user-agent": "test-ua",
            "oai-device-id": self.device_id,
            "oai-language": "en-US",
            "oai-client-build-number": "1",
            "oai-client-version": "test",
            "oai-session-id": self.oai_session_id,
            "referer": referer,
        }

    def get(self, *args, **kwargs):
        return self.session.get(*args, **kwargs)

    def navigator_language(self):
        return "en-US"


class PlanRetry403Tests(unittest.TestCase):
    def test_plan_request_matches_reference_headers_and_timezone(self):
        import core.chatgpt_plan as cp

        fake = _FakeBrowserSession(403, "forbidden")
        with patch.object(cp, "BrowserSession", return_value=fake):
            result = cp.check_account_plan(
                self._token(),
                proxy="socks5h://127.0.0.1:1",
                max_attempts=1,
                retry_delay=0,
            )
        self.assertFalse(result["ok"])
        args, kwargs = fake.session.requests[0]
        self.assertIn("timezone_offset_min=-480", args[0])
        headers = {str(k).lower(): v for k, v in kwargs["headers"].items()}
        self.assertEqual(headers["accept"], "application/json")
        self.assertEqual(headers["authorization"], f"Bearer {self._token()}")
        self.assertEqual(headers["oai-device-id"], "dev")
        self.assertEqual(headers["oai-language"], "en-US")
        self.assertEqual(headers["x-openai-target-path"], cp.ACCOUNTS_CHECK_PATH)
        self.assertEqual(headers["x-openai-target-route"], cp.ACCOUNTS_CHECK_ROUTE)
        self.assertEqual(headers["oai-session-id"], "session-1")
        self.assertEqual(headers["oai-client-build-number"], "1")

    def test_403_is_not_retryable(self):
        from core.chatgpt_plan import _retryable_plan_error

        self.assertFalse(_retryable_plan_error(403))
        self.assertTrue(_retryable_plan_error(429))
        self.assertTrue(_retryable_plan_error(500))
        self.assertTrue(_retryable_plan_error(None))
        self.assertFalse(_retryable_plan_error(400))
        self.assertFalse(_retryable_plan_error(401))
        self.assertFalse(_retryable_plan_error(404))

    def _token(self) -> str:
        payload = base64.urlsafe_b64encode(
            json.dumps({"exp": 4102444800}).encode()
        ).decode().rstrip("=")
        return f"e30.{payload}.sig"

    def test_browser_query_uses_live_page_request_and_parses_response(self):
        import core.chatgpt_plan as cp

        class _FakePage:
            def __init__(self):
                self.calls = []

            def execute_async_script(self, script, *args):
                self.calls.append((script, args))
                return {
                    "status": 200,
                    "body": json.dumps(_accounts_payload()),
                    "headers": {"content-type": "application/json"},
                    "timezone_offset_min": "-480",
                }

        page = _FakePage()
        result = cp.check_account_plan_browser(page, self._token(), timezone_offset_min="-480")
        self.assertTrue(result["ok"])
        self.assertEqual(result["http_status"], 200)
        self.assertTrue(result["browser_context"])
        self.assertEqual(result["network_route"], "browser")
        self.assertEqual(len(page.calls), 1)
        script, args = page.calls[0]
        self.assertIn("authorization", script)
        self.assertIn("AbortController", script)
        self.assertIn("x-openai-target-path", script)
        self.assertEqual(args[1], cp.ACCOUNTS_CHECK_PATH)
        self.assertEqual(args[2], cp.ACCOUNTS_CHECK_ROUTE)
        self.assertEqual(args[3], "-480")
        self.assertEqual(args[4], 8000)

    def test_playwright_browser_query_routes_navigation_and_restores_home(self):
        import core.chatgpt_plan as cp

        class _FakeResponse:
            status = 200
            headers = {"content-type": "application/json"}

            def text(self):
                return json.dumps(_accounts_payload())

        class _FakeRoute:
            def __init__(self):
                self.request = type("Request", (), {"headers": {"user-agent": "browser"}})()
                self.continued_headers = None

            def continue_(self, *, headers):
                self.continued_headers = headers

        class _FakePage:
            def __init__(self):
                self.handler = None
                self.urls = []
                self.route_obj = None
                self.unrouted = False

            def evaluate(self, script):
                return {"language": "en-US", "timezoneOffset": "-480", "deviceId": "did-1"}

            def route(self, pattern, handler):
                self.handler = handler

            def goto(self, url, *, wait_until):
                self.urls.append(url)
                if cp.ACCOUNTS_CHECK_PATH in url:
                    self.route_obj = _FakeRoute()
                    self.handler(self.route_obj)
                    return _FakeResponse()
                return None

            def unroute(self, pattern, handler):
                self.unrouted = handler is self.handler

        page = _FakePage()
        result = cp.check_account_plan_browser(page, self._token())
        self.assertTrue(result["ok"])
        self.assertEqual(result["timezone_offset_min"], "-480")
        self.assertEqual(page.route_obj.continued_headers["authorization"], f"Bearer {self._token()}")
        self.assertEqual(page.route_obj.continued_headers["oai-device-id"], "did-1")
        self.assertTrue(page.unrouted)
        self.assertEqual(page.urls[-1], "https://chatgpt.com/")

    def test_browser_query_reports_unauthorized_without_retry(self):
        import core.chatgpt_plan as cp

        class _FakePage:
            def execute_async_script(self, script, *args):
                return {
                    "status": 401,
                    "body": '{"detail":"Unauthorized"}',
                    "headers": {"content-type": "application/json"},
                    "timezone_offset_min": "-480",
                }

        result = cp.check_account_plan_browser(_FakePage(), self._token())
        self.assertFalse(result["ok"])
        self.assertEqual(result["http_status"], 401)
        self.assertFalse(result["retryable"])
        self.assertTrue(result["needs_live_check"])
        self.assertIn("AT已过期/失效", result["error"])

    def test_browser_query_reports_abort_as_retryable(self):
        import core.chatgpt_plan as cp

        class _FakePage:
            def execute_async_script(self, script, *args):
                return {"status": 0, "body": "", "headers": {}, "error": "AbortError"}

        result = cp.check_account_plan_browser(_FakePage(), self._token())
        self.assertFalse(result["ok"])
        self.assertTrue(result["retryable"])
        self.assertFalse(result["needs_live_check"])
        self.assertIn("AbortError", result["error"])

    def test_trial_pool_retries_403_with_new_proxy(self):
        import core.chatgpt_plan as cp

        payload = json.dumps(_accounts_payload())
        first = _FakeBrowserSession(403, "<html>forbidden</html>", {"server": "cloudflare"})
        second = _FakeBrowserSession(200, payload, {"content-type": "application/json"})
        picked = iter(["socks5h://jp-one:1080", "socks5h://jp-two:1080"])
        created = []

        def make_session(*, proxy, detect_exit_geo):
            created.append(proxy)
            return first if len(created) == 1 else second

        with patch.object(proxy_cfg, "pick_trial_proxy", side_effect=lambda region: next(picked)), \
             patch.object(cp, "BrowserSession", side_effect=make_session):
            result = cp.check_account_trial(
                self._token(),
                "jp",
                max_attempts=2,
                retry_delay=0,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(created, ["socks5h://jp-one:1080", "socks5h://jp-two:1080"])
        self.assertEqual(result["proxy_used"], "socks5h://jp-two:1080")
        self.assertEqual(first.session.calls, 1)
        self.assertEqual(second.session.calls, 1)

    def test_explicit_trial_proxy_does_not_retry_403(self):
        import core.chatgpt_plan as cp

        fake = _FakeBrowserSession(403, "<html>forbidden</html>", {"server": "cloudflare"})
        with patch.object(cp, "BrowserSession", return_value=fake):
            result = cp.check_account_trial(
                self._token(),
                "jp",
                proxy="socks5h://fixed:1080",
                max_attempts=3,
                retry_delay=0,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["http_status"], 403)
        self.assertEqual(fake.session.calls, 1)
        self.assertFalse(result["cloudflare_challenge"])

    def test_403_challenge_does_not_retry(self):
        import core.chatgpt_plan as cp

        fake = _FakeBrowserSession(
            403,
            "<html>Enable JavaScript and cookies to continue</html>",
            {"cf-mitigated": "challenge", "server": "cloudflare"},
        )
        with patch.object(cp, "BrowserSession", return_value=fake):
            result = cp.check_account_plan(
                self._token(),
                proxy="socks5h://127.0.0.1:1",
                max_attempts=3,
                retry_delay=0,
            )
        self.assertEqual(fake.session.calls, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["http_status"], 403)
        self.assertFalse(result["retryable"])
        self.assertTrue(result["cloudflare_challenge"])
        self.assertIn("Cloudflare Challenge", result["error"])


class TrialDbUpdateTests(unittest.TestCase):
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

    def _insert(self, email: str) -> int:
        return db.insert_account(email=email, access_token="tok")

    def test_update_writes_only_requested_region(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with ExitStack() as stack:
                for p in self._db_patches(root):
                    stack.enter_context(p)
                acc_id = self._insert("x@y.z")
                ok1 = db.update_account_trial_check(acc_id=acc_id, result={
                    "ok": True, "region": "jp", "checked_at": "2026-08-18T10:00:00",
                    "trial_eligible": True, "campaign_id": "c1", "title": "t",
                    "discount_percentage": 100, "duration_num_periods": 1, "duration_period": "monthly",
                })
                self.assertTrue(ok1)
                acc = db.get_account(acc_id)
                self.assertEqual(acc.get("trial_check_status"), "success")
                self.assertTrue(acc.get("trial_jp_eligible"))
                self.assertEqual(acc.get("trial_jp_campaign_id"), "c1")
                self.assertNotIn("trial_gb_eligible", acc)

    def test_failure_does_not_clear_previous_success(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with ExitStack() as stack:
                for p in self._db_patches(root):
                    stack.enter_context(p)
                acc_id = self._insert("x@y.z")
                db.update_account_trial_check(acc_id=acc_id, result={
                    "ok": True, "region": "jp", "checked_at": "2026-08-18T10:00:00",
                    "trial_eligible": True, "campaign_id": "c1",
                })
                db.update_account_trial_check(acc_id=acc_id, result={
                    "ok": False, "region": "gb", "checked_at": "2026-08-18T10:01:00",
                    "error": "HTTP 500",
                })
                acc = db.get_account(acc_id)
                self.assertEqual(acc.get("trial_check_status"), "failed")
                self.assertTrue(acc.get("trial_jp_eligible"))
                self.assertNotIn("trial_gb_eligible", acc)
                self.assertEqual(acc.get("trial_check_error"), "HTTP 500")

    def test_claim_rejects_concurrent_check(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with ExitStack() as stack:
                for p in self._db_patches(root):
                    stack.enter_context(p)
                acc_id = self._insert("x@y.z")
                self.assertTrue(db.claim_account_trial_check(acc_id=acc_id, region="jp"))
                self.assertFalse(db.claim_account_trial_check(acc_id=acc_id, region="gb"))

    def test_recover_interrupted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with ExitStack() as stack:
                for p in self._db_patches(root):
                    stack.enter_context(p)
                acc_id = self._insert("x@y.z")
                db.claim_account_trial_check(acc_id=acc_id, region="jp")
                db.mark_account_trial_check_running(acc_id)
                self.assertEqual(db.recover_interrupted_trial_checks(), 1)
                acc = db.get_account(acc_id)
                self.assertEqual(acc.get("trial_check_status"), "failed")
                self.assertFalse(acc.get("trial_check_ok"))


if __name__ == "__main__":
    unittest.main()
