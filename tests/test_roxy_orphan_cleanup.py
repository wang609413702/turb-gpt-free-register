# -*- coding: utf-8 -*-
"""Roxy 孤儿环境回收测试：打开失败即时回收 + 崩溃遗留登记清扫。"""
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core import roxybrowser_client
from core.roxybrowser_client import RoxyBrowserClient, RoxyOpenResult, sweep_orphan_profiles


def _stub_request(paths_result: dict, failing_paths: set[str] | None = None):
    calls: list[tuple[str, str]] = []
    failing_paths = failing_paths or set()

    def _request(_self, method, path=None, *args, **kwargs):
        calls.append((str(method).upper(), str(path)))
        for fragment, action in paths_result.items():
            if fragment in str(path):
                if fragment in failing_paths:
                    raise RuntimeError(f"Roxy stub failure for {path}")
                return action() if callable(action) else action
        raise RuntimeError(f"Roxy stub 未定义路径 {path}")

    return _request, calls


class RoxyOrphanCleanupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        self._path_patcher = patch.object(
            roxybrowser_client, "_ORPHAN_REGISTRY_PATH", self._tmp / "roxy_orphan_profiles.json"
        )
        self._path_patcher.start()
        self.addCleanup(self._path_patcher.stop)
        roxybrowser_client._INVENTORY_CACHE.clear() if hasattr(roxybrowser_client, "_INVENTORY_CACHE") else None

    def _registry(self) -> list[dict]:
        path = roxybrowser_client._ORPHAN_REGISTRY_PATH
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))

    def test_open_profile_failure_after_create_deletes_profile(self):
        stub, calls = _stub_request({
            "/browser/create": {"data": {"dirId": "123"}},
            "/browser/open": {},
            "/browser/close": {"code": 0},
            "/browser/delete": {"code": 0},
        }, failing_paths={"/browser/open"})
        client = RoxyBrowserClient(api_base="http://127.0.0.1:1", token="t")
        with patch.object(RoxyBrowserClient, "request", stub):
            with self.assertRaisesRegex(RuntimeError, "stub failure for /browser/open"):
                client.open_profile()

        deleted = [path for method, path in calls if "/browser/delete" in path]
        self.assertEqual(len(deleted), 1)
        # 回收成功后登记表不应残留该环境。
        self.assertEqual(self._registry(), [])

    def test_create_registers_and_cleanup_removes_entry(self):
        stub, calls = _stub_request({
            "/browser/create": {"data": {"dirId": "456"}},
            "/browser/close": {"code": 0},
            "/browser/delete": {"code": 0},
        })
        client = RoxyBrowserClient(api_base="http://127.0.0.1:1", token="t")
        with patch.object(RoxyBrowserClient, "request", stub):
            profile_id = client.create_profile()
        self.assertEqual([entry["profile_id"] for entry in self._registry()], ["456"])

        with patch.object(RoxyBrowserClient, "request", stub):
            client.cleanup_profile(RoxyOpenResult(profile_id, {}, created_by_run=True))
        self.assertEqual(self._registry(), [])

    def test_sweep_deletes_only_stale_registered_profiles(self):
        now = time.time()
        (roxybrowser_client._ORPHAN_REGISTRY_PATH).write_text(
            json.dumps([
                {"profile_id": "stale-1", "created_at": now - 7200},
                {"profile_id": "fresh-1", "created_at": now - 60},
            ]),
            encoding="utf-8",
        )
        stub, calls = _stub_request({"/browser/delete": {"code": 0}})
        client = RoxyBrowserClient(api_base="http://127.0.0.1:1", token="t")
        with patch.object(roxybrowser_client, "RoxyBrowserClient", lambda *a, **k: client), \
             patch.object(RoxyBrowserClient, "request", stub):
            deleted = sweep_orphan_profiles(max_age_seconds=1800)

        self.assertEqual(deleted, 1)
        self.assertEqual([entry["profile_id"] for entry in self._registry()], ["fresh-1"])
        self.assertEqual(len([path for _, path in calls if "/browser/delete" in path]), 1)


if __name__ == "__main__":
    unittest.main()
