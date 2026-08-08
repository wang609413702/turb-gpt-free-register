# -*- coding: utf-8 -*-
"""
代理池配置

每次注册随机抽取一个代理，保证不同 sid 之间彼此独立，避免风控关联。

协议说明：
    - http:// / https://   HTTP(S) 代理
    - socks5://            SOCKS5（DNS 本地解析，可能泄漏）
    - socks5h://           SOCKS5（DNS 在代理端解析，推荐，避免 DNS-IP 错配）
"""
from config.env_loader import apply_env_overrides
from urllib.parse import quote
import random


# 本地代理入口；实际出口地区以代理/分流规则为准。
# 推荐使用 socks5h://（DNS 在代理端解析），避免本地 DNS 与出口 IP 地区错配。
PROXY_POOL = [
    "socks5://127.0.0.1:7897",
]

# MoMo 检测专用代理池，与注册/查套餐代理池隔离，单独轮换。
# 支持 socks5:// / socks5h:// 完整 URL，或 host:port:user:pass / host:port
# （无 scheme 时自动补 socks5h://）。留空则 MoMo 检测直连。
MOMO_PROXY_POOL: list[str] = []

# 套餐/Plus 试用资格查询与 Codex Agent Token 生成共用这组独立网络策略，
# 避免批量请求被注册代理池中的临时本地代理拖垮，也避免无条件直连造成出口策略失控。
#   auto   = 优先使用 PLAN_CHECK_PROXY 或代理池；本地代理端口未监听时回退直连
#   proxy  = 强制使用 PLAN_CHECK_PROXY 或代理池，失败直接报错
#   direct = 始终直连
PLAN_CHECK_PROXY_MODE = "auto"

# 套餐查询 / Codex Agent Token 生成专用代理。留空时 auto/proxy 模式从 PROXY_POOL 选择。
# 代理可能包含账号密码，因此 WebUI 会把它保存到 .env。
PLAN_CHECK_PROXY = ""

# 查套餐 / 生成 Codex Agent Token 使用独立的短超时和有限重试，避免后台任务长时间卡住。
PLAN_CHECK_TIMEOUT = 15.0
PLAN_CHECK_MAX_ATTEMPTS = 2
PLAN_CHECK_RETRY_DELAY = 1.5

# 新注册账号的权益可能存在短暂同步延迟。首次查询失败，或返回 free 且暂未发现
# Plus 试用资格时，等待该秒数后再复查一次；设为 0 可关闭复查。
PLAN_CHECK_REGISTRATION_RECHECK_DELAY = 2.0

# 自动、手动和批量套餐查询共用同一个后台队列；Codex Agent Token 使用独立队列，
# 但复用这里的网络模式、请求启动间隔与随机抖动，避免批量后台请求过于集中。
PLAN_CHECK_WORKERS = 3
PLAN_CHECK_QUEUE_LIMIT = 500
PLAN_CHECK_MIN_INTERVAL = 0.4
PLAN_CHECK_JITTER = 0.3


def pick_proxy() -> str:
    """从代理池中随机抽取一个代理 URL；池为空时返回空串（即不使用代理）。"""
    return random.choice(PROXY_POOL) if PROXY_POOL else ""


def _build_proxy_url(scheme: str, host: str, port: str, user: str = "", password: str = "") -> str:
    """拼成 curl 要求的 URL：认证信息用 user:pass@host:port，并对凭据做 URL 编码。"""
    # from urllib.parse import quote  ← 放到模块顶部 import
    auth = ""
    if user or password:
        cred = quote(str(user or ""), safe="")
        if password:
            cred += ":" + quote(str(password), safe="")
        auth = cred + "@"
    return f"{scheme}://{auth}{host}:{port}"


def normalize_momo_proxy(raw: str) -> str:
    """归一化单行 MoMo 代理为 curl 可用的 URL；非法返回空串（跳过该条）。

    curl 要求认证写成 user:pass@host:port（@ 分隔），不能串成 host:port:user:pass。
    支持：
      - socks5://... / socks5h://... / http(s)://... 完整 URL
        （socks5:// 统一为 socks5h://；错误形态 host:port:user:pass 会重排为标准形态）
      - host:port:user:pass / host:port   → 自动补 socks5h:// 前缀并重排认证
    """
    value = str(raw or "").strip()
    if not value:
        return ""

    scheme = "socks5h"  # 默认；DNS 在代理端解析，和 BrowserSession 行为一致
    body = value
    if "://" in value:
        scheme, body = value.split("://", 1)
        scheme = scheme.lower()
        # socks5:// 统一成 socks5h://
        if scheme == "socks5":
            scheme = "socks5h"
        if scheme not in ("socks5h", "http", "https"):
            return ""

    # body 可能是：
    #   host:port                （无认证）
    #   user:pass@host:port      （标准认证，已带 @）
    #   host:port:user:pass      （错误形态，需重排）
    host = port = user = password = ""
    if "@" in body:
        cred, endpoint = body.rsplit("@", 1)
        # endpoint = host:port
        ep = endpoint.split(":")
        if len(ep) != 2 or not ep[1].isdigit():
            return ""
        host, port = ep[0], ep[1]
        cred_parts = cred.split(":", 1)
        user = cred_parts[0]
        password = cred_parts[1] if len(cred_parts) == 2 else ""
    else:
        parts = body.split(":")
        # host:port（2 段，无认证）或 host:port:user:pass（4 段，带认证）
        if len(parts) == 2 and parts[1].isdigit():
            host, port = parts[0], parts[1]
        elif len(parts) == 4 and parts[1].isdigit():
            host, port, user, password = parts[0], parts[1], parts[2], parts[3]
        else:
            return ""

    if not host or not port:
        return ""
    return _build_proxy_url(scheme, host, port, user, password)


def pick_momo_proxy() -> str:
    """从 MoMo 检测代理池中随机抽取并归一化一个代理 URL；池为空返回空串（直连）。"""
    if not MOMO_PROXY_POOL:
        return ""
    proxies = [normalize_momo_proxy(line) for line in MOMO_PROXY_POOL]
    valid = [p for p in proxies if p]
    return random.choice(valid) if valid else ""


# 兼容入口：默认每次进程启动随机选一个，作为本次注册全程的固定代理
PROXY = pick_proxy()

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'PROXY_POOL': 'list_str_multiline',
    'MOMO_PROXY_POOL': 'list_str_multiline',
    'PLAN_CHECK_PROXY_MODE': 'str',
    'PLAN_CHECK_PROXY': 'str',
    'PLAN_CHECK_TIMEOUT': 'float',
    'PLAN_CHECK_MAX_ATTEMPTS': 'int',
    'PLAN_CHECK_RETRY_DELAY': 'float',
    'PLAN_CHECK_REGISTRATION_RECHECK_DELAY': 'float',
    'PLAN_CHECK_WORKERS': 'int',
    'PLAN_CHECK_QUEUE_LIMIT': 'int',
    'PLAN_CHECK_MIN_INTERVAL': 'float',
    'PLAN_CHECK_JITTER': 'float',
})
PROXY = pick_proxy()
