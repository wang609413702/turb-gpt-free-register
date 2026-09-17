# -*- coding: utf-8 -*-
"""配置热重载测试：WebUI 保存后 config 模块属性立即更新，无需重启。"""
import os
import unittest
from unittest.mock import patch

from config import env_loader
from config import roxybrowser as roxy_cfg


class ReloadConfigModulesTests(unittest.TestCase):
    def test_reload_applies_new_env_value_without_restart(self):
        original = roxy_cfg.ROXY_CODEX_CALLBACK_TIMEOUT
        try:
            with patch.dict(os.environ, {"ROXY_CODEX_CALLBACK_TIMEOUT": "99"}):
                env_loader.reload_config_modules()
                self.assertEqual(roxy_cfg.ROXY_CODEX_CALLBACK_TIMEOUT, 99)
        finally:
            # patch.dict 退出即恢复 os.environ 的原值（.env 里的配置），再 reload 回去。
            env_loader.load_env(override=True)
            env_loader.reload_config_modules()
        self.assertEqual(roxy_cfg.ROXY_CODEX_CALLBACK_TIMEOUT, original)

    def test_reload_restores_source_default_when_env_cleared(self):
        """去掉环境变量后 reload，模块属性应回到源码默认值。"""
        original = roxy_cfg.ROXY_CODEX_CALLBACK_TIMEOUT
        try:
            with patch.dict(os.environ, {"ROXY_CODEX_CALLBACK_TIMEOUT": "99"}):
                env_loader.reload_config_modules()
            os.environ.pop("ROXY_CODEX_CALLBACK_TIMEOUT", None)
            env_loader.reload_config_modules()
            self.assertEqual(roxy_cfg.ROXY_CODEX_CALLBACK_TIMEOUT, 180)
        finally:
            if original is not None:
                os.environ["ROXY_CODEX_CALLBACK_TIMEOUT"] = str(original)
            env_loader.reload_config_modules()


if __name__ == "__main__":
    unittest.main()
