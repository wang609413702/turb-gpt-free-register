# -*- coding: utf-8 -*-
"""CloakBrowser 自动化注册配置。"""
from config.env_loader import apply_env_overrides

# 是否无头启动：False=显示窗口，True=无头。
CLOAK_HEADLESS: bool = True

# 是否启用 CloakBrowser humanize 行为。
CLOAK_HUMANIZE: bool = True

# 使用当前出口 IP 自动匹配时区/语言/WebRTC IP。
CLOAK_GEOIP: bool = True

# 显式指定 Cloak 语言/时区；留空则在 CLOAK_GEOIP=True 时按出口 IP 自动推断。
# 例如：CLOAK_LOCALE="ja-JP"，CLOAK_TIMEZONE="Asia/Tokyo"。
CLOAK_LOCALE: str = ""
CLOAK_TIMEZONE: str = ""

# 是否把本项目传入/代理池抽取的代理传给 CloakBrowser。
CLOAK_USE_PROXY: bool = True

# 带认证的 SOCKS5 代理改走本地无认证转发入口。Chromium 不支持 SOCKS5 认证
# （带凭据的 socks:// URL 会被 --proxy-server 整体判无效，页面报
# net::ERR_NO_SUPPORTED_PROXIES），转发层在本机补上认证再转发到上游。
CLOAK_SOCKS_FORWARD: bool = True

# 本地转发入口起始端口；每个上游代理占一个端口，被占用时自动向后搜索。
SOCKS_FORWARD_BASE_PORT: int = 18100

# Pro license；留空则使用免费 binary。
CLOAK_LICENSE_KEY: str = ""

# 固定指纹 seed；留空则每次 launch 随机生成新指纹。
CLOAK_FINGERPRINT_SEED: str = ""

# 持久化用户目录；留空则临时上下文。若要固定账号画像/缓存，可填如 "./cloak-profiles/default"。
CLOAK_USER_DATA_DIR: str = ""

# 额外 Chromium 参数，例如 ["--fingerprint=12345"]。CLOAK_FINGERPRINT_SEED 会自动追加。
CLOAK_EXTRA_ARGS: list = []

# 与原 Roxy Selenium 流程共用的超时时间。
CLOAK_SELENIUM_TIMEOUT: int = 90

# Cloudflare 人机验证放行等待上限（秒）。实测正常放行 20~40 秒；
# 超时通常说明出口 IP 信誉差，重提任务换 session 即可。
CLOAK_CF_WAIT_TIMEOUT: int = 75

# 调试时保留浏览器不自动关闭。
CLOAK_KEEP_BROWSER_OPEN: bool = False

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {'CLOAK_HEADLESS': 'bool', 'CLOAK_HUMANIZE': 'bool', 'CLOAK_GEOIP': 'bool', 'CLOAK_LOCALE': 'str', 'CLOAK_TIMEZONE': 'str', 'CLOAK_USE_PROXY': 'bool', 'CLOAK_SOCKS_FORWARD': 'bool', 'SOCKS_FORWARD_BASE_PORT': 'int', 'CLOAK_CF_WAIT_TIMEOUT': 'int', 'CLOAK_LICENSE_KEY': 'str', 'CLOAK_FINGERPRINT_SEED': 'str', 'CLOAK_USER_DATA_DIR': 'str', 'CLOAK_SELENIUM_TIMEOUT': 'int', 'CLOAK_KEEP_BROWSER_OPEN': 'bool'})
