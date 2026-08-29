# -*- coding: utf-8 -*-
"""注册失败邮箱回收分类回归测试：OTP 超时邮箱必须剔除，不回 available。"""
import unittest
from unittest.mock import patch

from core import email_provider as ep


class GenericApiMailError(RuntimeError):
    pass


class _OtherError(RuntimeError):
    pass


class IsOtpDeliveryTimeoutTests(unittest.TestCase):
    def test_mail_timeout_with_no_code_marker(self):
        exc = GenericApiMailError("等待通用 API 验证码超时: a@b.c; HTTP 200 但未提取到 6 位验证码，响应预览: no_code")
        self.assertTrue(ep.is_otp_delivery_timeout(exc))

    def test_outlook_timeout_detected(self):
        class OutlookClientError(RuntimeError):
            pass
        self.assertTrue(ep.is_otp_delivery_timeout(
            OutlookClientError("等待 a@b.c 的 OTP 超时（>90s）")))

    def test_non_timeout_mail_error_not_flagged(self):
        # 取码服务本身的请求异常（非超时）不应剔除邮箱
        exc = GenericApiMailError("ConnectionError: connection refused")
        self.assertFalse(ep.is_otp_delivery_timeout(exc))

    def test_non_mail_exception_ignored(self):
        self.assertFalse(ep.is_otp_delivery_timeout(_OtherError("等待通用 API 验证码超时")))
        self.assertFalse(ep.is_otp_delivery_timeout(RuntimeError("no_code")))


class ReleaseAfterFailureTests(unittest.TestCase):
    def _release(self, exc, *, create_acknowledged=False, account_dead=False):
        with patch.object(ep, "release_email") as release:
            src = ep.release_email_after_registration_failure(
                "a@b.c", exc,
                create_acknowledged=create_acknowledged,
                account_dead=account_dead,
            )
        return release, src

    def test_otp_timeout_marks_failed_not_available(self):
        exc = GenericApiMailError("等待通用 API 验证码超时: a@b.c; no_code 暂未收到验证码")
        release, _ = self._release(exc)
        release.assert_called_once()
        kwargs = release.call_args.kwargs
        self.assertEqual(kwargs["status"], "failed")
        self.assertIn("收不到 OpenAI 验证码", kwargs["note"])

    def test_ordinary_failure_returns_available(self):
        release, _ = self._release(RuntimeError("页面超时"))
        self.assertEqual(release.call_args.kwargs["status"], "available")

    def test_account_dead_marks_failed(self):
        release, _ = self._release(RuntimeError("deactivated"), account_dead=True)
        self.assertEqual(release.call_args.kwargs["status"], "failed")
        self.assertIn("删除/停用", release.call_args.kwargs["note"])

    def test_create_acknowledged_marks_failed(self):
        release, _ = self._release(RuntimeError("session 超时"), create_acknowledged=True)
        self.assertEqual(release.call_args.kwargs["status"], "failed")

    def test_empty_email_noop(self):
        with patch.object(ep, "release_email") as release:
            ep.release_email_after_registration_failure("", RuntimeError("x"), create_acknowledged=False)
        release.assert_not_called()


if __name__ == "__main__":
    unittest.main()
