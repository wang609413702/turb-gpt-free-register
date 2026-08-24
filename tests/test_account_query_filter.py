# -*- coding: utf-8 -*-
"""账号页搜索关键字筛选（支持列）回归测试。"""
import unittest

from core.db import _account_matches_query


def _row(**overrides):
    row = {
        "id": 1,
        "email": "user@example.com",
        "gcash_has_gcash": None,
        "momo_has_momo": None,
        "kakao_has_kakao": None,
        "paypal_has_paypal": None,
        "ideal_has_ideal": None,
        "gopay_has_gopay": None,
        "trial_jp_eligible": None,
        "trial_gb_eligible": None,
        "trial_ph_eligible": None,
        "gcash_check_error": "checkout 创建失败",
    }
    row.update(overrides)
    return row


class CapabilityQueryTests(unittest.TestCase):
    def test_capability_keyword_matches_supported_only(self):
        supported = _row(gcash_has_gcash=True)
        unsupported = _row(gcash_has_gcash=False)
        unchecked = _row()
        self.assertTrue(_account_matches_query(supported, "GCash"))
        self.assertFalse(_account_matches_query(unsupported, "gcash"))
        # 未检测过的账号不算支持；错误文本里恰好含渠道名也不算
        self.assertFalse(_account_matches_query(unchecked, "gcash"))

    def test_capability_negation_keeps_unsupported_and_unchecked(self):
        self.assertFalse(_account_matches_query(_row(gcash_has_gcash=True), "-gcash"))
        self.assertFalse(_account_matches_query(_row(gcash_has_gcash=True), "!gcash"))
        self.assertTrue(_account_matches_query(_row(gcash_has_gcash=False), "-gcash"))
        self.assertTrue(_account_matches_query(_row(), "-gcash"))

    def test_keywords_combine_with_and(self):
        both = _row(gcash_has_gcash=True, momo_has_momo=True)
        only_gcash = _row(gcash_has_gcash=True)
        self.assertTrue(_account_matches_query(both, "gcash momo"))
        self.assertFalse(_account_matches_query(only_gcash, "gcash momo"))

    def test_plain_text_search_still_works(self):
        row = _row(email="alice@momo-mail.example")
        self.assertTrue(_account_matches_query(row, "alice"))
        self.assertFalse(_account_matches_query(row, "bob"))

    def test_capability_wins_over_substring_for_known_keywords(self):
        # 邮箱里含 momo 但检测不支持 momo：关键字语义优先，不按子串命中
        row = _row(email="momo_fan@example.com", momo_has_momo=False)
        self.assertFalse(_account_matches_query(row, "momo"))

    def test_all_capability_keywords_registered(self):
        cases = {
            "kakao": "kakao_has_kakao",
            "paypal": "paypal_has_paypal",
            "ideal": "ideal_has_ideal",
            "gopay": "gopay_has_gopay",
            "trial_jp": "trial_jp_eligible",
            "trial_gb": "trial_gb_eligible",
            "trial_ph": "trial_ph_eligible",
        }
        for keyword, field in cases.items():
            self.assertTrue(_account_matches_query(_row(**{field: True}), keyword), keyword)
            self.assertFalse(_account_matches_query(_row(**{field: False}), keyword), keyword)


if __name__ == "__main__":
    unittest.main()
