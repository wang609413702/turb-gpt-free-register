# -*- coding: utf-8 -*-
"""
OpenAI / ChatGPT OAuth 协议固定参数

来自抓包，OpenAI 自己的 client_id 是固定值。
SENTINEL_SV 是 sdk.js 的版本号，会随 OpenAI 更新而变化，
更新时去 https://sentinel.openai.com/sentinel/<version>/sdk.js 找当前版本。
"""

# OAuth 客户端 ID（固定）
OPENAI_CLIENT_ID = "app_X8zY6vW2pQ9tR3dE7nK1jL5gH"

# OAuth scopes
OPENAI_SCOPE = (
    "openid email profile offline_access "
    "model.request model.read "
    "organization.read organization.write"
)

# OAuth audience
OPENAI_AUDIENCE = "https://api.openai.com/v1"

# OAuth 回调（chatgpt.com 端）
OPENAI_REDIRECT_URI = "https://chatgpt.com/api/auth/callback/openai"

# Sentinel SDK 版本号（影响 sentinel iframe URL 与 referer header）
SENTINEL_SV = "20260219f9f6"

# ChatGPT 页面 build 标识（用于 Sentinel p[6] / documentElement data-build 模拟）
OPENAI_BUILD_ID = "prod-fb4a8a2a751dfec391053cfd7b01c52699ccf78c"

# ChatGPT 前端 CES / API 上报头，来自 2026-07-19 抓包。
OAI_CLIENT_BUILD_NUMBER = "8370486"
OAI_CLIENT_VERSION = OPENAI_BUILD_ID

# Statsig / Analytics SDK 版本，纯协议补齐前端同形态链路时使用。
STATSIG_CLIENT_KEY = "client-nb0qtYlZuy2tCMN5s5ncnuIBCJncjRViT0IzFm7GqST"
STATSIG_SDK_VERSION = "3.32.6"
STATSIG_SDK_TYPE = "javascript-client"
AB_CLIENT_KEY = "client-tN5GMyzpIPKXd3KNv7ANIfiqjRSvNNTTWbZdbdabF58"
AB_SDK_VERSION = "3.32.4"

# email-otp/validate 是否携带 Sentinel token。
# 2026-08-14 真实浏览器 HAR 采集确认：email-otp/validate 会携带
# openai-sentinel-token + openai-sentinel-so-token（flow=authorize_continue）。
# 默认开启以对齐真实浏览器；如服务端策略变化可设 False 回退。
SEND_SENTINEL_ON_EMAIL_OTP_VALIDATE = True

# 是否补齐 HAR 中 ChatGPT Web 首屏 bootstrap 预热链路。
CHATGPT_ANON_BOOTSTRAP_ENABLED = True
CHATGPT_AUTH_BOOTSTRAP_ENABLED = True
# True 时预热失败会中断主流程；默认 False，仅记录日志并继续。
CHATGPT_BOOTSTRAP_STRICT = False

# 纯协议资源预热（实验性，默认关闭）。
# 真实浏览器加载页面会抓取 HTML 引用的 CSS/JS/字体/图片（单次注册数百条），
# 纯协议原本只打 ~18 条核心 API，“零资源加载”是强自动化特征。开启后会在
# 关键页面加载后解析并抓取其引用的静态资源，把请求画像拉到接近真实浏览器。
# 见 core/asset_warmup.py。不影响注册主流程（失败一律吞掉）。
PROTOCOL_ASSET_WARMUP: bool = False
# 单页最多抓取的资源数，控制耗时；真实页面资源很多，按需调大。
PROTOCOL_ASSET_WARMUP_MAX_PER_PAGE: int = 60


from config.env_loader import apply_env_overrides  # noqa: E402

apply_env_overrides(globals(), {
    "PROTOCOL_ASSET_WARMUP": "bool",
    "PROTOCOL_ASSET_WARMUP_MAX_PER_PAGE": "int",
})
