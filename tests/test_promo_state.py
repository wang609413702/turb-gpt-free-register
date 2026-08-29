# -*- coding: utf-8 -*-
"""checkout 会话优惠授予状态的提取：优惠与本地支付方式互斥，需单独记录。"""
from core.chatgpt_momo import promo_state


def test_promo_state_granted():
    state = promo_state({
        "promo_campaign": {"promo_campaign_id": "plus-1-month-free", "is_coupon_from_query_param": False},
        "custom_payment_methods": [],
    })
    assert state == {"promo_granted": True, "promo_campaign_id": "plus-1-month-free"}


def test_promo_state_not_granted():
    assert promo_state({"promo_campaign": None}) == {"promo_granted": False, "promo_campaign_id": None}
    assert promo_state({}) == {"promo_granted": False, "promo_campaign_id": None}


def test_promo_state_malformed():
    assert promo_state({"promo_campaign": "plus-1-month-free"}) == {
        "promo_granted": False, "promo_campaign_id": None,
    }
    assert promo_state(None) == {"promo_granted": False, "promo_campaign_id": None}
