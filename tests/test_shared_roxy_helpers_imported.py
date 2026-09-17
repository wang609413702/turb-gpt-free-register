# -*- coding: utf-8 -*-
"""复用 Roxy 页面操作函数的模块必须显式导入所引用的共享私有函数。

Cloak 复用 core.roxy_registration 的页面操作函数；漏写 import
只会在注册任务运行到该行时才以 NameError 爆炸（编译/import 都不报错）。
此测试静态扫描复用模块源码，确保引用到的 roxy_registration 顶层私有函数
都已导入到本模块命名空间。
"""
import re
import unittest

from core import cloakbrowser_registration, roxy_registration

_REUSE_MODULES = (cloakbrowser_registration,)
_IDENTIFIER_RE = re.compile(r"\b_[a-z][a-z0-9_]*\b")


class SharedRoxyHelperImportTests(unittest.TestCase):
    def test_reused_roxy_helpers_are_imported(self):
        roxy_names = {
            name for name in dir(roxy_registration)
            if name.startswith("_") and callable(getattr(roxy_registration, name))
        }
        for module in _REUSE_MODULES:
            with self.subTest(module=module.__name__):
                source = open(module.__file__, encoding="utf-8").read()
                referenced = set(_IDENTIFIER_RE.findall(source))
                missing = sorted(
                    name for name in referenced & roxy_names
                    if not hasattr(module, name)
                )
                self.assertEqual(
                    missing, [],
                    f"{module.__name__} 引用了 roxy_registration 的共享函数但未导入，"
                    f"运行时会 NameError: {missing}",
                )


if __name__ == "__main__":
    unittest.main()
