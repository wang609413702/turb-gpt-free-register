# -*- coding: utf-8 -*-
"""注册时代理国家/地区代码的提取与规范化。"""
from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)


_COUNTRY_ALIASES = {
    "JA": "JP",
    "JAPAN": "JP",
    "UK": "GB",
    "UNITED KINGDOM": "GB",
    "USA": "US",
    "UNITED STATES": "US",
    "UNITED STATES OF AMERICA": "US",
    "SOUTH KOREA": "KR",
    "REPUBLIC OF KOREA": "KR",
    "VIETNAM": "VN",
    "PHILIPPINES": "PH",
    "SINGAPORE": "SG",
    "INDONESIA": "ID",
}


def normalize_registration_country(value) -> str:
    """把 GeoIP/云代理地区值规范化为两位大写国家代码。"""
    if isinstance(value, dict):
        value = (
            value.get("country_code")
            or value.get("countryCode")
            or value.get("cc")
            or value.get("country")
            or ""
        )
    text = str(value or "").strip().upper().replace("-", "_")
    if not text or text in {"NONE", "DEFAULT", "AUTO"}:
        return ""
    if text == "RESIDENTIAL":
        return "US"
    if text.startswith("RESIDENTIAL_"):
        text = text.removeprefix("RESIDENTIAL_")
    text = _COUNTRY_ALIASES.get(text.replace("_", " "), text)
    if re.fullmatch(r"[A-Z]{2}", text):
        return text
    return ""


def detect_selenium_registration_country(driver) -> str:
    """通过当前 Selenium 浏览器探测实际出口国家；失败不阻断注册。"""
    try:
        from config import browser as browser_cfg

        endpoints = list(getattr(browser_cfg, "IP_GEO_ENDPOINTS", []) or [])
        timeout = float(getattr(browser_cfg, "IP_GEO_TIMEOUT", 6) or 6)
    except Exception:
        return ""

    try:
        driver.set_page_load_timeout(timeout)
    except Exception:
        pass
    for url in endpoints:
        try:
            driver.get(url)
            raw = driver.execute_script(
                "return (document.body && (document.body.innerText || document.body.textContent)) || '';"
            )
            data = json.loads(str(raw or "").strip())
            country = normalize_registration_country(data)
            if country:
                logger.info("[注册地区] 浏览器出口国家: %s", country)
                return country
        except Exception as exc:
            logger.debug("[注册地区] 浏览器出口探测失败 endpoint=%s: %s: %s", url, type(exc).__name__, exc)
    return ""
