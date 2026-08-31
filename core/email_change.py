# -*- coding: utf-8 -*-
"""
ChatGPT 换绑邮箱底层能力（参考 replace-emal 项目移植）：

    POST https://chatgpt.com/backend-api/accounts/change_email/begin
         {"email": "新邮箱"}
    POST https://chatgpt.com/backend-api/accounts/change_email/verify
         {"email": "新邮箱", "code": "6位验证码"}

begin 后 OpenAI 会向新邮箱发送验证码；verify 校验通过即完成换绑。

同时提供：
    - protocol_login：邮箱 OTP 协议登录（与查活/注册同一条链路），换绑前登录旧邮箱、
      换绑后用新邮箱重新登录拿新 accessToken。
    - fetch_generic_api_otp / fetch_cloudmail_otp：换绑邮箱两类取码来源；
      CloudMail 取码收紧 after_ts(±2s) 并支持 used_codes 排除，避免同一收件箱
      里换绑验证码与重新登录验证码串号。
"""
from __future__ import annotations

import base64
import json
import logging
import time
from typing import Callable

import requests
import urllib3

from core.session import BrowserSession

logger = logging.getLogger(__name__)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CHANGE_EMAIL_BEGIN_URL = "https://chatgpt.com/backend-api/accounts/change_email/begin"
CHANGE_EMAIL_VERIFY_URL = "https://chatgpt.com/backend-api/accounts/change_email/verify"

# 换绑各阶段超时（秒）；取码默认值跟随 config.email 的 OTP 配置
CHANGE_EMAIL_OTP_MAX_WAIT = 180
LOGIN_OTP_MAX_WAIT = 120
RELOGIN_OTP_MAX_WAIT = 120
OTP_POLL_INTERVAL = 3
OTP_SETTLE_SECONDS = 5
CHANGE_EMAIL_VERIFY_ATTEMPTS = 3
OTP_MAX_ATTEMPTS = 3
LOGIN_BOOTSTRAP = True
RELOGIN_DELAY_SECONDS = (3.0, 8.0)

_GENERIC_HEADERS = {
    "Accept": "application/json,text/plain,*/*",
    "User-Agent": "Mozilla/5.0 (compatible; gpt-email-change/1.0)",
}


class ChangeEmailError(RuntimeError):
    """换绑接口失败。"""


class ChangeEmailReauthRequiredError(ChangeEmailError):
    """换绑接口要求最近登录态（token 有效但需要重新登录建立会话）。"""


class ChangeEmailOtpInvalidError(ChangeEmailError):
    """换绑验证码无效/过期，可重新 begin 后再试。"""


class MailCodeError(RuntimeError):
    """取码失败/超时。"""


class LoginError(RuntimeError):
    """协议登录失败。"""


class EmailNotRegisteredError(LoginError):
    """该邮箱尚未注册（validate 后进入 about_you 注册分支）。"""


# ============================================================
# 换绑接口（backend-api，Bearer token + 会话 cookie）
# ============================================================

def normalize_token(token: str) -> str:
    token = (token or "").strip().strip('"').strip("'")
    if token.lower().startswith("authorization:"):
        token = token.split(":", 1)[1].strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def _jwt_account_id(token: str) -> str:
    """从 accessToken JWT 里解 chatgpt_account_id（不校验签名，仅取声明）。"""
    try:
        payload_b64 = normalize_token(token).split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        auth = payload.get("https://api.openai.com/auth") or {}
        return str(auth.get("chatgpt_account_id") or auth.get("account_id") or "").strip()
    except Exception:
        return ""


def _change_email_headers(session: BrowserSession, token: str) -> dict:
    headers = session.get_chatgpt_headers(referer="https://chatgpt.com/")
    headers.update({
        "accept": "application/json",
        "authorization": f"Bearer {normalize_token(token)}",
    })
    account_id = _jwt_account_id(token)
    if account_id:
        headers.setdefault("chatgpt-account-id", account_id)
    return headers


def _post_change_email(session: BrowserSession, token: str, url: str, body: dict) -> dict:
    resp = session.post(url, headers=_change_email_headers(session, token), data=json.dumps(body))
    text = resp.text or ""
    if resp.status_code >= 400:
        lower = text.lower()
        # token 本身有效但 OpenAI 要求"最近登录过"（敏感操作的安全要求），
        # 上层应回退为旧邮箱 OTP 登录建立会话后重试。
        if "reauth_required" in lower or "recent login" in lower:
            raise ChangeEmailReauthRequiredError(
                f"换绑接口要求最近登录态: status={resp.status_code}, body={text[:240]}"
            )
        # 验证码错误/过期：可重试类错误单独抛出，便于上层重新 begin。
        # 注意不能匹配裸词 "code"——错误响应里的 JSON 键名 "code" 会造成误分类。
        if resp.status_code in (400, 401, 422) and any(
            k in lower for k in ("invalid", "incorrect", "expired", "verification", "验证码")
        ):
            raise ChangeEmailOtpInvalidError(
                f"换绑请求被拒绝（验证码类错误）: status={resp.status_code}, body={text[:240]}"
            )
        raise ChangeEmailError(
            f"换绑请求失败: status={resp.status_code}, url={url}, body={text[:240]}"
        )
    try:
        return resp.json()
    except ValueError:
        return {"raw": text[:500]}


def change_email_begin(session: BrowserSession, token: str, new_email: str) -> dict:
    """发起换绑：向新邮箱发送验证码。"""
    logger.info("[换绑] begin: 新邮箱=%s", new_email)
    data = _post_change_email(session, token, CHANGE_EMAIL_BEGIN_URL, {"email": new_email})
    logger.info("[换绑] begin 响应: %s", json.dumps(data, ensure_ascii=False)[:300])
    return data


def change_email_verify(session: BrowserSession, token: str, new_email: str, code: str) -> dict:
    """校验换绑验证码，通过即完成换绑。"""
    logger.info("[换绑] verify: 新邮箱=%s, code=%s", new_email, code)
    data = _post_change_email(
        session, token, CHANGE_EMAIL_VERIFY_URL, {"email": new_email, "code": code},
    )
    logger.info("[换绑] verify 响应: %s", json.dumps(data, ensure_ascii=False)[:300])
    return data


# ============================================================
# 通用 API 取码（换绑邮箱池 generic_api 类型）
# ============================================================

def _offer_candidate(
    code: str,
    best_otp: str | None,
    best_seen_at: float,
    settle_until: float | None,
    settle: int,
    *,
    source: str,
    meta: dict,
) -> tuple[str | None, float, float | None]:
    """settle 语义：新候选替换旧候选并重置计时；相同候选只刷新来源日志。"""
    now = time.time()
    if not best_otp:
        logger.info(
            "[取码] 首次锁定 OTP=%s, source=%s ts=%s，等 %ss 确认...",
            code, source, meta.get("received_at"), settle,
        )
        return code, now, now + settle
    if code != best_otp:
        logger.info(
            "[取码] 发现更新 OTP=%s, source=%s ts=%s，替换 %s 并重置 settle",
            code, source, meta.get("received_at"), best_otp,
        )
        return code, now, now + settle
    return best_otp, best_seen_at, settle_until


def fetch_generic_api_otp(
    email: str,
    code_url: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
) -> str:
    """
    轮询通用 API 取码地址，直到提取到 6 位验证码或超时。

    settle 机制：首次拿到候选后继续观察 settle 秒，
    期间出现更新的验证码则替换并重置计时，避免取到接口缓存的旧码。
    """
    from config import email as _email_cfg
    from core.generic_api_mail_client import (
        _extract_code,
        _extract_structured_api_code,
        _fetch_yangyang_otp,
        _parse_yangyang_code_url,
    )

    deadline = time.time() + (max_wait or LOGIN_OTP_MAX_WAIT)
    interval = poll_interval or OTP_POLL_INTERVAL
    settle = settle_seconds if settle_seconds is not None else OTP_SETTLE_SECONDS

    logger.info(
        "[GenericAPI] 开始轮询取码地址: %s，最长 %ss, settle=%ss",
        email, max_wait or LOGIN_OTP_MAX_WAIT, settle,
    )
    is_yangyang = _parse_yangyang_code_url(code_url) is not None
    last_error = ""
    best_otp: str | None = None
    best_seen_at = 0.0
    settle_until: float | None = None

    while time.time() < deadline:
        try:
            session = requests.Session()
            yy_result = (
                _fetch_yangyang_otp(session, code_url, _GENERIC_HEADERS, after_ts=after_ts)
                if is_yangyang else None
            )
            if yy_result:
                code, yy_meta = yy_result
                best_otp, best_seen_at, settle_until = _offer_candidate(
                    code, best_otp, best_seen_at, settle_until, settle,
                    source="yangyang", meta=yy_meta,
                )
                text = ""
                resp = None
            elif is_yangyang:
                last_error = "yangyang 列表中尚未出现 after_ts 之后的新验证码邮件"
                resp = None
                text = ""
            else:
                resp = session.get(code_url, headers=_GENERIC_HEADERS, timeout=20, verify=False)
                text = resp.text or ""

            if resp is not None and resp.status_code == 200:
                structured = _extract_structured_api_code(text, after_ts=after_ts)
                structured_meta = structured[1] if structured else {}
                code = structured[0] if structured else _extract_code(text)
                if code:
                    best_otp, best_seen_at, settle_until = _offer_candidate(
                        code, best_otp, best_seen_at, settle_until, settle,
                        source="structured_api" if structured else "plain",
                        meta=structured_meta,
                    )
                else:
                    last_error = f"HTTP 200 但未提取到 6 位验证码，响应预览: {text[:160]}"
            elif resp is not None:
                last_error = f"HTTP {resp.status_code}: {text[:160]}"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        now = time.time()
        if best_otp and settle_until is not None and now >= settle_until:
            logger.info("[GenericAPI] settle 完成，返回 OTP=%s", best_otp)
            return best_otp

        remaining = int(deadline - now)
        if best_otp and settle_until is not None:
            logger.info(
                "[GenericAPI] 已锁定候选 OTP=%s，等 settle 中（剩余 settle ~%ss, 总剩余 %ss）...",
                best_otp, max(0, int(settle_until - now)), remaining,
            )
        else:
            logger.info(
                "[GenericAPI] 暂未取到验证码，%ss 后重试（剩余 %ss）...", interval, remaining,
            )
        time.sleep(interval)

    if best_otp:
        logger.warning("[GenericAPI] 总超时但已有候选，返回 OTP=%s", best_otp)
        return best_otp
    raise MailCodeError(f"等待通用 API 验证码超时: {email}; {last_error}")


# ============================================================
# CloudMail 取码（换绑邮箱池 cloudmail 类型 / CloudMail 生成的新邮箱）
# ============================================================

def fetch_cloudmail_otp(
    email: str,
    after_ts: float | None = None,
    used_codes: set[str] | frozenset[str] = frozenset(),
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
) -> str:
    """
    轮询 CloudMail 收件箱取 OpenAI 6 位验证码。

    与 core.cloudmail_client.fetch_latest_otp 的差异：
      - after_ts 过滤收紧到 ±2s（原为 -30s 宽限），换绑验证码和
        重新登录验证码在同一收件箱先后到达时不会串号；
      - 支持 used_codes 排除已提交成功的验证码。
    """
    from core.cloudmail_client import _parse_time, _request
    from core.otp_utils import extract_otp, looks_like_openai_email

    wait_seconds = int(max_wait if max_wait is not None else CHANGE_EMAIL_OTP_MAX_WAIT)
    interval = max(1, int(poll_interval if poll_interval is not None else OTP_POLL_INTERVAL))
    settle = max(0, int(settle_seconds if settle_seconds is not None else OTP_SETTLE_SECONDS))
    deadline = time.monotonic() + wait_seconds

    best_otp: str | None = None
    best_timestamp = float("-inf")
    best_seen_at = 0.0
    settle_until: float | None = None
    last_error = "收件箱为空或尚未出现新的 OpenAI 验证码"

    logger.info("[CloudMail] 开始轮询邮箱 %s，最长 %ss（排除已用码 %s）",
                email, wait_seconds, sorted(used_codes) or "无")
    while time.monotonic() <= deadline:
        try:
            mails = _request(
                "/api/public/emailList",
                {
                    "toEmail": email,
                    "timeSort": "desc",
                    "type": 0,
                    "isDel": 0,
                    "num": 1,
                    "size": 20,
                },
            )
            if not isinstance(mails, list):
                raise MailCodeError("CloudMail 邮件查询响应 data 不是数组")
            for mail in sorted(
                mails,
                key=lambda item: _parse_time((item or {}).get("createTime")) or float("-inf"),
                reverse=True,
            ):
                if not isinstance(mail, dict):
                    continue
                ts = _parse_time(mail.get("createTime"))
                if after_ts is not None and ts is not None and ts < after_ts - 2:
                    continue
                item = {
                    "id": mail.get("emailId") or mail.get("id"),
                    "from": mail.get("sendEmail") or mail.get("from") or "",
                    "subject": mail.get("subject") or "",
                    "text": mail.get("text") or "",
                    "html": mail.get("content") or mail.get("html") or "",
                }
                if not looks_like_openai_email(item):
                    continue
                otp = extract_otp(item)
                if not otp or otp in used_codes:
                    continue
                candidate_time = float("-inf") if ts is None else ts
                if (
                    best_otp is None
                    or candidate_time > best_timestamp
                    or (candidate_time == best_timestamp and otp != best_otp)
                ):
                    best_otp = otp
                    best_timestamp = candidate_time
                    best_seen_at = time.monotonic()
                    settle_until = best_seen_at + settle
                    logger.info(
                        "[CloudMail] 锁定 OTP 候选 %s（subject=%r），等 %ss 确认",
                        otp, str(item.get("subject") or "")[:60], settle,
                    )

            now = time.monotonic()
            if best_otp and settle_until is not None and now >= settle_until:
                return best_otp
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))

    if best_otp:
        return best_otp
    raise MailCodeError(f"等待 CloudMail 验证码超时: {email}; {last_error}")


def fetch_rebind_email_otp(email: str, pool_row: dict | None = None, **kwargs) -> str:
    """
    换绑邮箱取码分发：
      - cloudmail 类型 -> CloudMail 收件箱轮询；
      - generic_api 类型 -> 轮询池里的取码地址；
      - 池里查不到时兜底走全局 wait_for_otp（按各邮箱池归属解析来源）。
    """
    from core import db

    row = pool_row or db.get_rebind_email_by_email(email)
    kind = str((row or {}).get("kind") or "").strip()
    code_url = str((row or {}).get("code_url") or "").strip()
    if kind == "cloudmail" or (not kind and not code_url):
        return fetch_cloudmail_otp(email, **kwargs)
    if code_url:
        return fetch_generic_api_otp(email, code_url, **kwargs)
    from core.email_provider import wait_for_otp
    return wait_for_otp(email, after_ts=kwargs.get("after_ts") or 0.0, **{
        k: v for k, v in kwargs.items() if k in ("max_wait", "poll_interval", "settle_seconds")
    })


# ============================================================
# 协议登录（OTP-only，无密码）
# ============================================================

def protocol_login(
    email: str,
    fetch_otp: Callable[..., str],
    *,
    proxy: str | None = None,
    bootstrap: bool | None = None,
    otp_max_attempts: int | None = None,
    otp_max_wait: int | None = None,
    send_sentinel_on_validate: bool = True,
) -> tuple[BrowserSession, dict]:
    """
    用邮箱 OTP 完成一次协议登录，返回 (会话, {"access_token", "session_info"})。

    流程与查活/协议注册完全一致：
        预检 → 匿名预热 → providers → csrf → signin(login_hint)
        → follow_authorize 落到 /email-verification 并自动触发 OTP
        → 取码 → sentinel(authorize_continue) → email-otp/validate
        → OAuth 回调 → /api/auth/session
    validate 后进入 about_you 说明邮箱尚未注册，直接中止，避免误注册新账号。
    """
    from core.chatgpt_auth import get_csrf_token, get_providers, signin_openai
    from core.humanize import delay as human_delay
    from core.openai_auth import (
        EmailOtpInvalidError,
        build_sentinel_header,
        follow_authorize,
        network_preflight,
        request_sentinel_token,
        validate_email_otp,
    )
    from core.account_export import fetch_session, follow_oauth_callback

    if bootstrap is None:
        bootstrap = LOGIN_BOOTSTRAP
    attempts = otp_max_attempts or OTP_MAX_ATTEMPTS
    otp_kwargs = {"max_wait": otp_max_wait} if otp_max_wait is not None else {}

    session = BrowserSession(proxy=proxy)
    proxy_label = "无"
    if session.proxy:
        proxy_label = f"{session.proxy.split('://')[0]}://...@{session.proxy.split('@')[-1]}"
    logger.info("[登录] 开始：%s，代理=%s", email, proxy_label)

    network_preflight(session)
    human_delay("navigate")

    if bootstrap:
        try:
            from core.chatgpt_bootstrap import anonymous_bootstrap
            anonymous_bootstrap(session, strict=False)
        except Exception as exc:
            logger.warning("[登录] 匿名预热失败（忽略继续）：%s: %s", type(exc).__name__, str(exc)[:160])
        human_delay("navigate")

    providers = get_providers(session)
    human_delay("api")
    csrf_token = get_csrf_token(session)
    human_delay("api")
    authorize_url = signin_openai(session, csrf_token, email)
    human_delay("api")

    # OTP 触发前的时间戳：取码只看此后的邮件，避免拿到旧码。
    otp_after_ts = time.time()
    follow_authorize(session, authorize_url)
    human_delay("navigate")

    def restart_email_otp_flow(reason: str) -> float:
        """
        直接调用 resend 偶发会让 auth 流程进入 500/异常页；重新提交
        同一个邮箱能同时触发新 OTP 并恢复验证码页状态。
        """
        logger.info("[OTP] 重新触发邮箱验证码：%s", reason)
        new_after_ts = time.time()
        new_authorize_url = signin_openai(session, get_csrf_token(session), email)
        human_delay("api")
        follow_authorize(session, new_authorize_url)
        human_delay("navigate")
        return new_after_ts

    def fetch_latest_otp_fallback() -> str | None:
        try:
            return fetch_otp(after_ts=0.0, max_wait=15)
        except TypeError:
            try:
                return fetch_otp(after_ts=0.0)
            except Exception:
                return None
        except Exception:
            return None

    validate_result = None
    current_otp = None
    for otp_attempt in range(1, attempts + 1):
        if current_otp is None:
            logger.info("[OTP] 等待验证码：%s（第 %s/%s 次）", email, otp_attempt, attempts)
            try:
                current_otp = fetch_otp(after_ts=otp_after_ts, **otp_kwargs)
            except MailCodeError as exc:
                if otp_attempt >= attempts:
                    raise
                fallback_otp = fetch_latest_otp_fallback()
                if fallback_otp:
                    logger.info(
                        "[OTP] 取码超时但宽松取到最新验证码，直接尝试提交：%s (fallback)",
                        fallback_otp,
                    )
                    current_otp = fallback_otp
                else:
                    logger.warning(
                        "[OTP] 取码超时：%s，重新触发验证码后再等一轮（%s/%s）",
                        str(exc)[:180], otp_attempt, attempts,
                    )
                    otp_after_ts = restart_email_otp_flow(
                        "取码超时，避免直接 resend 导致 500/异常页"
                    )
                    current_otp = None
                    continue

        human_delay("otp_input")
        try:
            sentinel_header = so_header = None
            if send_sentinel_on_validate:
                sentinel_resp = request_sentinel_token(session, "authorize_continue")
                sentinel_header, so_header = build_sentinel_header(
                    session, sentinel_resp, "authorize_continue",
                )
                human_delay("challenge")
            validate_result = validate_email_otp(session, current_otp, sentinel_header, so_header)
            break
        except EmailOtpInvalidError as exc:
            if otp_attempt >= attempts:
                raise
            logger.warning(
                "[OTP] 验证码错误/过期：%s，准备重新触发并重新取码", str(exc)[:180],
            )
            otp_after_ts = restart_email_otp_flow(
                "验证码错误/过期，避免直接 resend 导致 500/异常页"
            )
            current_otp = None

    if validate_result is None:
        raise LoginError(f"OTP 验证未完成: {email}")

    human_delay("api")

    page = validate_result.get("page") if isinstance(validate_result, dict) else {}
    page = page if isinstance(page, dict) else {}
    page_type = str(page.get("type") or "")
    continue_url = (
        validate_result.get("continue_url")
        or validate_result.get("external_url")
        or validate_result.get("url")
        or page.get("continue_url")
        or page.get("external_url")
        or page.get("url")
    )
    logger.info(
        "[登录] OTP 后分支判断: page_type=%s, has_continue_url=%s",
        page_type or "空", bool(continue_url),
    )

    if page_type == "about_you" or (continue_url and "about-you" in str(continue_url)):
        raise EmailNotRegisteredError(
            f"邮箱尚未注册（OTP 后进入 about-you 注册分支），已中止: {email}"
        )

    if page_type == "external_url" or not continue_url:
        if not continue_url:
            raise LoginError(f"OTP 响应缺少可跟随 URL，无法完成登录: {validate_result}")
    elif "chatgpt.com/api/auth/callback" not in str(continue_url) and "auth.openai.com/authorize/continue" not in str(continue_url):
        raise LoginError(f"OTP 后续页面类型未知，拒绝继续: page_type={page_type}, resp={validate_result}")

    follow_oauth_callback(
        session, continue_url, referer="https://auth.openai.com/email-verification",
    )
    human_delay("post_auth")

    session_info = fetch_session(session)
    access_token = session_info.get("accessToken")
    if not access_token:
        raise LoginError(f"未拿到 accessToken，登录态可能未建立: {email}")
    logger.info("[登录] 成功：%s，user=%s", email, (session_info.get("user") or {}).get("id"))
    return session, {"access_token": access_token, "session_info": session_info}
