# -*- coding: utf-8 -*-
"""Plus 试用资格查询支持的地区及其字段/代理池映射。"""
from datetime import datetime
from zoneinfo import ZoneInfo

TRIAL_REGIONS = ("jp", "gb", "de", "br", "th", "ph", "vn")

TRIAL_PROXY_POOL_NAMES = {
    region: f"TRIAL_{region.upper()}_PROXY_POOL"
    for region in TRIAL_REGIONS
}

TRIAL_REGION_TIMEZONES = {
    "jp": "Asia/Tokyo",
    "gb": "Europe/London",
    "de": "Europe/Berlin",
    "br": "America/Sao_Paulo",
    "th": "Asia/Bangkok",
    "ph": "Asia/Manila",
    "vn": "Asia/Ho_Chi_Minh",
}

TRIAL_REGION_FIELD_PREFIXES = {
    region: f"trial_{region}"
    for region in TRIAL_REGIONS
}

TRIAL_REGION_DETAIL_SUFFIXES = (
    "eligible",
    "checked_at",
    "campaign_id",
    "title",
    "discount_percentage",
    "duration_num_periods",
    "duration_period",
    "last_success_at",
)


def trial_timezone_offset_min(region: str, at: datetime | None = None) -> str:
    """返回地区当前的 JavaScript getTimezoneOffset 分钟值，自动处理夏令时。"""
    zone = ZoneInfo(TRIAL_REGION_TIMEZONES[region])
    if at is None:
        local = datetime.now(zone)
    elif at.tzinfo is None:
        local = at.replace(tzinfo=zone)
    else:
        local = at.astimezone(zone)
    offset = local.utcoffset()
    return str(-int(offset.total_seconds() // 60)) if offset is not None else "0"


def account_has_trial_eligibility(account: dict) -> bool:
    """区域查询结果优先；完全没有区域数据时才使用历史字段兜底。"""
    regional_values = [
        account.get(f"{TRIAL_REGION_FIELD_PREFIXES[region]}_eligible")
        for region in TRIAL_REGIONS
    ]
    if any(value is True for value in regional_values):
        return True
    if any(value is False for value in regional_values):
        return False
    return account.get("plus_trial_eligible") is True
