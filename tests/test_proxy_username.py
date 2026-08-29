# -*- coding: utf-8 -*-
"""检测结果携带代理用户名（proxy_username）的解析与路由注入。"""
from core.chatgpt_plan import proxy_username, resolve_plan_check_route, resolve_trial_check_route


def test_proxy_username_from_url():
    assert proxy_username(
        "socks5h://pfu1t25723-region-PH-sid-NzWpRbcn-t-5:secret@us.lajiaohttp.net:2000"
    ) == "pfu1t25723-region-PH-sid-NzWpRbcn-t-5"


def test_proxy_username_from_bare_host_port_user_pass():
    assert proxy_username("us.lajiaohttp.net:2000:user-x:pass-y") == "user-x"


def test_proxy_username_without_credentials():
    assert proxy_username("socks5h://us.lajiaohttp.net:2000") == ""
    assert proxy_username("") == ""
    assert proxy_username(None) == ""


def test_plan_check_route_carries_username_on_explicit_proxy():
    route = resolve_plan_check_route("socks5h://user-de:pw@proxy.example.com:1080")
    assert route["network_route"] == "proxy"
    assert route["proxy_used"] == "socks5h://***:***@proxy.example.com:1080"
    assert route["proxy_username"] == "user-de"


def test_plan_check_route_direct_has_no_username():
    route = resolve_plan_check_route("")
    assert route["network_route"] == "direct"
    assert route["proxy_username"] is None


def test_trial_check_route_carries_username():
    route = resolve_trial_check_route("de", "socks5h://user-de:pw@proxy.example.com:1080")
    assert route["proxy_username"] == "user-de"
