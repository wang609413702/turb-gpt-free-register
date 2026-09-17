# -*- coding: utf-8 -*-
"""查活网络模式路由与并发数配置测试。"""
import unittest
from unittest.mock import patch

from config import proxy as proxy_cfg
from core import live_check_service
from core.chatgpt_plan import resolve_live_check_route, resolve_plan_check_route


class ResolveLiveCheckRouteTests(unittest.TestCase):
    def test_direct_mode_uses_local_network(self):
        with patch.object(proxy_cfg, "LIVE_CHECK_PROXY_MODE", "direct"), \
             patch.object(proxy_cfg, "PLAN_CHECK_PROXY", "socks5h://fixed.example:2000"):
            route = resolve_live_check_route()
        self.assertEqual(route["network_route"], "direct")
        self.assertEqual(route["proxy"], "")
        self.assertEqual(route["proxy_mode"], "direct")

    def test_proxy_mode_reuses_plan_check_dedicated_proxy(self):
        with patch.object(proxy_cfg, "LIVE_CHECK_PROXY_MODE", "proxy"), \
             patch.object(proxy_cfg, "PLAN_CHECK_PROXY", "socks5h://user:pw@fixed.example:2000"):
            route = resolve_live_check_route()
        self.assertEqual(route["network_route"], "proxy")
        self.assertIn("fixed.example:2000", route["proxy"])
        self.assertEqual(route["proxy_mode"], "proxy")

    def test_empty_mode_follows_plan_check_mode(self):
        with patch.object(proxy_cfg, "LIVE_CHECK_PROXY_MODE", ""), \
             patch.object(proxy_cfg, "PLAN_CHECK_PROXY_MODE", "direct"):
            route = resolve_live_check_route()
        self.assertEqual(route["network_route"], "direct")
        self.assertEqual(route["proxy_mode"], "direct")

    def test_explicit_proxy_overrides_mode(self):
        with patch.object(proxy_cfg, "LIVE_CHECK_PROXY_MODE", "direct"):
            route = resolve_live_check_route(explicit_proxy="socks5h://override.example:1080")
        self.assertEqual(route["network_route"], "proxy")
        self.assertIn("override.example:1080", route["proxy"])
        self.assertEqual(route["proxy_mode"], "request")

    def test_invalid_mode_is_rejected(self):
        with patch.object(proxy_cfg, "LIVE_CHECK_PROXY_MODE", "nope"):
            with self.assertRaisesRegex(ValueError, "查活"):
                resolve_live_check_route()

    def test_plan_check_route_still_uses_own_mode(self):
        with patch.object(proxy_cfg, "PLAN_CHECK_PROXY_MODE", "proxy"), \
             patch.object(proxy_cfg, "PLAN_CHECK_PROXY", "socks5h://plan.example:2000"), \
             patch.object(proxy_cfg, "LIVE_CHECK_PROXY_MODE", "direct"):
            plan_route = resolve_plan_check_route()
            live_route = resolve_live_check_route()
        self.assertEqual(plan_route["network_route"], "proxy")
        self.assertIn("plan.example:2000", plan_route["proxy"])
        self.assertEqual(live_route["network_route"], "direct")


class LiveCheckWorkersTests(unittest.TestCase):
    def setUp(self):
        # 单线程测试内直接快照/恢复执行器状态；不要持有 _EXECUTOR_LOCK，
        # 它是普通 Lock，_get_executor() 会再次申请导致死锁。
        self._executor = live_check_service._EXECUTOR
        self._workers = live_check_service._EXECUTOR_WORKERS
        self.addCleanup(self._restore)

    def _restore(self):
        live_check_service._EXECUTOR = self._executor
        live_check_service._EXECUTOR_WORKERS = self._workers

    def test_executor_resizes_when_config_changes(self):
        with patch.object(proxy_cfg, "LIVE_CHECK_WORKERS", 1):
            first = live_check_service._get_executor()
            self.assertEqual(first._max_workers, 1)
            self.assertEqual(live_check_service.queue_settings()["workers"], 1)
        with patch.object(proxy_cfg, "LIVE_CHECK_WORKERS", 4):
            second = live_check_service._get_executor()
            self.assertIsNot(second, first)
            self.assertEqual(second._max_workers, 4)
        with patch.object(proxy_cfg, "LIVE_CHECK_WORKERS", 99):
            self.assertEqual(live_check_service._get_executor()._max_workers, 16)

    def test_worker_count_invalid_falls_back_to_three(self):
        with patch.object(proxy_cfg, "LIVE_CHECK_WORKERS", "abc"):
            self.assertEqual(live_check_service._worker_count(), 3)


if __name__ == "__main__":
    unittest.main()
