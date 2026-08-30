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
from config.trial import TRIAL_PROXY_POOL_NAMES
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

# GCash 检测专用代理池，与 MoMo/注册/查套餐代理池隔离，单独轮换。
# 格式同上。留空则 GCash 检测直连。
GCASH_PROXY_POOL: list[str] = []

# Kakao 检测专用代理池（KR 出口），格式同 MoMo/GCash 池。留空则 Kakao 检测直连。
KAKAO_PROXY_POOL: list[str] = []

# PayPal 检测-BR 专用代理池（BR 出口），格式同上。留空则 BR 检测直连。
PAYPAL_BR_PROXY_POOL: list[str] = []

# PayPal 检测-TH 专用代理池（TH 出口），格式同上。留空则 TH 检测直连。
PAYPAL_TH_PROXY_POOL: list[str] = []

# PayPal 检测-DE 专用代理池（DE 出口），格式同上。留空则 DE 检测直连。
PAYPAL_DE_PROXY_POOL: list[str] = []

# IDEAL 检测专用代理池（NL 出口），格式同上。留空则 IDEAL 检测直连。
IDEAL_PROXY_POOL: list[str] = []

# GoPay 检测专用代理池（ID 出口），与 MoMo/GCash/Kakao/PayPal/IDEAL 代理池隔离，单独轮换。
# 格式同上。留空则 GoPay 检测直连。
GOPAY_PROXY_POOL: list[str] = []

# 查 JP 试用资格专用代理池（JP 出口）。试用资格按请求出口地区下发，因此查资格
# 不允许直连/回退主池：池为空时查询直接报错，避免用错区出口得出错误资格结论。
# 格式同上。
TRIAL_JP_PROXY_POOL: list[str] = []

# 查 GB 试用资格专用代理池（GB 出口），与 JP 池隔离，单独轮换。格式同上。
TRIAL_GB_PROXY_POOL: list[str] = []

# 查 DE/BR/TH/PH/VN 试用资格的地区专用代理池，彼此及支付检测池完全隔离。格式同上。
TRIAL_DE_PROXY_POOL: list[str] = []
TRIAL_BR_PROXY_POOL: list[str] = []
TRIAL_TH_PROXY_POOL: list[str] = []
TRIAL_PH_PROXY_POOL: list[str] = []
TRIAL_VN_PROXY_POOL: list[str] = []

# GCash 检测：PH/PHP 返回 OpenAI 自定义结账（oaics_），GCash 只出现在
# custom_payment_methods（cpmt_*），不在 payment_method_types 里。
# 留空 = 只要出现自定义支付方式即视为支持 GCash；也可填已知 cpmt_ 白名单（每行一个）精确匹配。
GCASH_CUSTOM_PAYMENT_METHOD_IDS: list[str] = []

# GoPay 检测：ID/IDR 返回 OpenAI 自定义结账（oaics_），GoPay 只出现在
# custom_payment_methods（cpmt_*），不在 payment_method_types 里。
# 留空 = 只要出现自定义支付方式即视为支持 GoPay；也可填已知 cpmt_ 白名单（每行一个）精确匹配。
GOPAY_CUSTOM_PAYMENT_METHOD_IDS: list[str] = []

# 支付检测通用网络参数，所有支付方式检测共用（MoMo/GCash/Kakao/PayPal-BR/TH/DE/IDEAL/GoPay）：
#   MOMO_CHECK_TIMEOUT      单次请求超时（秒），运行时钳制 1-60
#   MOMO_CHECK_MAX_ATTEMPTS 总尝试次数（含首次），钳制 1-6；超时/网络类失败换代理重试
#   MOMO_CHECK_RETRY_DELAY  重试等待基准（秒），线性递增（第1次重试等 1×基准，第2次等 2×基准），钳制 0-30
# 常量名沿用 MOMO_CHECK_* 历史命名，_momo_settings 读取后对所有检测生效。
MOMO_CHECK_TIMEOUT = 12.0
MOMO_CHECK_MAX_ATTEMPTS = 3
MOMO_CHECK_RETRY_DELAY = 1.0

# 支付检测是否携带 Sentinel 令牌（OpenAI-Sentinel-Token / OpenAI-Sentinel-SO-Token）。
# 无令牌的 checkout 请求会被风控按 "unusual activity" 拦截（HTTP 400）；
# 生成依赖 Node.js 18+ 与项目根 node_modules/jsdom（npm install）。缺失时自动降级为无令牌请求。
CHECKOUT_SENTINEL_ENABLED = True

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

# 查试用资格（JP/GB/DE/BR/TH/PH/VN）后台队列参数，与套餐查询队列隔离。
# 请求超时/重试沿用 PLAN_CHECK_TIMEOUT / PLAN_CHECK_MAX_ATTEMPTS / PLAN_CHECK_RETRY_DELAY。
TRIAL_CHECK_WORKERS = 3
TRIAL_CHECK_QUEUE_LIMIT = 200
TRIAL_CHECK_MIN_INTERVAL = 0.4
TRIAL_CHECK_JITTER = 0.3


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


def normalize_proxy(raw: str) -> str:
    """归一化单行代理为 curl 可用的 URL；非法返回空串（跳过该条）。

    curl 要求认证写成 user:pass@host:port（@ 分隔），不能串成 host:port:user:pass。
    支持：
      - socks5://... / socks5h://... / http(s)://... 完整 URL
        （socks5:// 统一为 socks5h://；错误形态 host:port:user:pass 会重排为标准形态）
      - host:port:user:pass / host:port / user:pass@host:port → 自动补 socks5h:// 前缀并重排认证
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


# 向后兼容别名
normalize_momo_proxy = normalize_proxy


def pick_momo_proxy() -> str:
    """从 MoMo 检测代理池中随机抽取并归一化一个代理 URL；池为空返回空串（直连）。"""
    if not MOMO_PROXY_POOL:
        return ""
    proxies = [normalize_proxy(line) for line in MOMO_PROXY_POOL]
    valid = [p for p in proxies if p]
    return random.choice(valid) if valid else ""


def pick_gcash_proxy() -> str:
    """从 GCash 检测代理池中随机抽取并归一化一个代理 URL；池为空返回空串（直连）。"""
    if not GCASH_PROXY_POOL:
        return ""
    proxies = [normalize_proxy(line) for line in GCASH_PROXY_POOL]
    valid = [p for p in proxies if p]
    return random.choice(valid) if valid else ""


def pick_kakao_proxy() -> str:
    """从 Kakao 检测代理池中随机抽取并归一化一个代理 URL；池为空返回空串（直连）。"""
    if not KAKAO_PROXY_POOL:
        return ""
    proxies = [normalize_proxy(line) for line in KAKAO_PROXY_POOL]
    valid = [p for p in proxies if p]
    return random.choice(valid) if valid else ""


def pick_paypal_proxy(region: str = "br") -> str:
    """按检测地区从对应 PayPal 代理池中随机抽取并归一化一个代理 URL；池为空返回空串（直连）。

    region: "br" → PAYPAL_BR_PROXY_POOL，"th" → PAYPAL_TH_PROXY_POOL，"de" → PAYPAL_DE_PROXY_POOL；未知地区回退 br。
    """
    key = str(region or "br").strip().lower()
    pool_name = {
        "br": "PAYPAL_BR_PROXY_POOL",
        "th": "PAYPAL_TH_PROXY_POOL",
        "de": "PAYPAL_DE_PROXY_POOL",
    }.get(key, "PAYPAL_BR_PROXY_POOL")
    pool = list(globals().get(pool_name) or [])
    if not pool:
        return ""
    proxies = [normalize_proxy(line) for line in pool]
    valid = [p for p in proxies if p]
    return random.choice(valid) if valid else ""


def pick_ideal_proxy() -> str:
    """从 IDEAL 检测代理池中随机抽取并归一化一个代理 URL；池为空返回空串（直连）。"""
    if not IDEAL_PROXY_POOL:
        return ""
    proxies = [normalize_proxy(line) for line in IDEAL_PROXY_POOL]
    valid = [p for p in proxies if p]
    return random.choice(valid) if valid else ""


def pick_gopay_proxy() -> str:
    """从 GoPay 检测代理池中随机抽取并归一化一个代理 URL；池为空返回空串（直连）。"""
    if not GOPAY_PROXY_POOL:
        return ""
    proxies = [normalize_proxy(line) for line in GOPAY_PROXY_POOL]
    valid = [p for p in proxies if p]
    return random.choice(valid) if valid else ""


def pick_trial_proxy(region: str = "jp") -> str:
    """从对应地区资格代理池随机抽取并归一化一个代理 URL；池为空返回空串。

    未知地区返回空串。与支付检测池不同，池为空时调用方必须报错而不是直连。
    """
    key = str(region or "").strip().lower()
    pool_name = TRIAL_PROXY_POOL_NAMES.get(key)
    if not pool_name:
        return ""
    pool = list(globals().get(pool_name) or [])
    if not pool:
        return ""
    proxies = [normalize_proxy(line) for line in pool]
    valid = [p for p in proxies if p]
    return random.choice(valid) if valid else ""


# 兼容入口：默认每次进程启动随机选一个，作为本次注册全程的固定代理
PROXY = pick_proxy()

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'PROXY_POOL': 'list_str_multiline',
    'MOMO_PROXY_POOL': 'list_str_multiline',
    'GCASH_PROXY_POOL': 'list_str_multiline',
    'KAKAO_PROXY_POOL': 'list_str_multiline',
    'PAYPAL_BR_PROXY_POOL': 'list_str_multiline',
    'PAYPAL_TH_PROXY_POOL': 'list_str_multiline',
    'PAYPAL_DE_PROXY_POOL': 'list_str_multiline',
    'IDEAL_PROXY_POOL': 'list_str_multiline',
    'GOPAY_PROXY_POOL': 'list_str_multiline',
    'TRIAL_JP_PROXY_POOL': 'list_str_multiline',
    'TRIAL_GB_PROXY_POOL': 'list_str_multiline',
    'TRIAL_DE_PROXY_POOL': 'list_str_multiline',
    'TRIAL_BR_PROXY_POOL': 'list_str_multiline',
    'TRIAL_TH_PROXY_POOL': 'list_str_multiline',
    'TRIAL_PH_PROXY_POOL': 'list_str_multiline',
    'TRIAL_VN_PROXY_POOL': 'list_str_multiline',
    'GOPAY_CUSTOM_PAYMENT_METHOD_IDS': 'list_str_multiline',
    'MOMO_CHECK_TIMEOUT': 'float',
    'MOMO_CHECK_MAX_ATTEMPTS': 'int',
    'MOMO_CHECK_RETRY_DELAY': 'float',
    'CHECKOUT_SENTINEL_ENABLED': 'bool',
    'MOMO_CHECK_WORKERS': 'int',
    'MOMO_CHECK_QUEUE_LIMIT': 'int',
    'GCASH_CHECK_WORKERS': 'int',
    'GCASH_CHECK_QUEUE_LIMIT': 'int',
    'KAKAO_CHECK_WORKERS': 'int',
    'KAKAO_CHECK_QUEUE_LIMIT': 'int',
    'PAYPAL_CHECK_WORKERS': 'int',
    'PAYPAL_CHECK_QUEUE_LIMIT': 'int',
    'IDEAL_CHECK_WORKERS': 'int',
    'IDEAL_CHECK_QUEUE_LIMIT': 'int',
    'GOPAY_CHECK_WORKERS': 'int',
    'GOPAY_CHECK_QUEUE_LIMIT': 'int',
    'GCASH_CUSTOM_PAYMENT_METHOD_IDS': 'list_str_multiline',
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
    'TRIAL_CHECK_WORKERS': 'int',
    'TRIAL_CHECK_QUEUE_LIMIT': 'int',
    'TRIAL_CHECK_MIN_INTERVAL': 'float',
    'TRIAL_CHECK_JITTER': 'float',
})
PROXY = pick_proxy()
