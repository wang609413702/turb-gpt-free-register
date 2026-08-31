# -*- coding: utf-8 -*-
"""
账号换绑后台队列与单账号编排（参考 replace-emal 项目移植）。

单账号流程：
    1. 建立登录态：优先复用账号已保存的 accessToken（JWT exp 未过期时直接调
       backend-api，跳过旧邮箱登录）。换绑是敏感操作，OpenAI 要求"最近登录过"
       的会话态：接口返回 reauth_required 时（token 仍有效）自动回退旧邮箱
       OTP 登录建立会话；token 缺失/过期或被 401/403 拒绝时同样回退。
    2. 领取新邮箱：换绑邮箱池（source=pool）或 CloudMail 随机生成（source=cloudmail）。
    3. 换绑：change_email/begin → 新邮箱收验证码 → change_email/verify
       （verify 验证码错误/过期会自动重新 begin 再取码，默认最多 3 次）。
    4. 重新登录：用新邮箱协议登录一次，拿新 accessToken。
    5. 成功后把账号 email 换成新邮箱并刷新 token；旧邮箱保留在 rebind_old_email。

代理：每个账号任务开始时从「代理池(每行一个)」（config.proxy.PROXY_POOL）随机抽
一条，该账号本次换绑全程（登录/begin/verify/重登录）共用同一条出口；池为空直连。

网络重试支持（resume）：
    run_rebind 不抛异常，返回带 stage 的结果；relogin 阶段失败但 verify 已成功时
    标记 rebind_effective=True，账号 email 同步切换，可稍后用「查活」刷新 token。
"""
from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from core import db
from core.chatgpt_plan import _mask_proxy
from core.email_change import (
    CHANGE_EMAIL_VERIFY_ATTEMPTS,
    ChangeEmailError,
    ChangeEmailOtpInvalidError,
    ChangeEmailReauthRequiredError,
    RELOGIN_DELAY_SECONDS,
    change_email_begin,
    change_email_verify,
    fetch_cloudmail_otp,
    protocol_login,
)

logger = logging.getLogger(__name__)

_WORKERS = 2
_QUEUE_LIMIT = 100
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="rebind")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_RUNNING: set[int] = set()
_LOCK = threading.Lock()

_LOG_DIR = Path(__file__).resolve().parent.parent / "换绑日志"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def log_path(email: str) -> Path:
    safe = str(email or "").replace("/", "_").replace("\\", "_").replace(":", "_")
    return _LOG_DIR / f"rebind-{safe}.log"


def _append_log(email: str, line: str, *, clear: bool = False) -> None:
    p = log_path(email)
    p.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H:%M:%S")
    mode = "w" if clear else "a"
    with p.open(mode, encoding="utf-8") as f:
        f.write(f"{stamp} [INFO] {line}\n")


def is_rebinding(email: str) -> bool:
    acc = db.get_account_by_email(email)
    if not acc:
        return False
    return str(acc.get("rebind_status") or "") in {"queued", "running"}


def queue_settings() -> dict:
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT}


def precheck_source(source: str) -> str | None:
    """入队前校验换绑邮箱来源可用性，返回错误信息或 None。"""
    if source == "pool":
        if db.count_rebind_emails_available() <= 0:
            return "换绑邮箱池没有可用邮箱，请先在「换绑邮箱池」导入"
        return None
    if source == "cloudmail":
        from config import email as _email_cfg
        if not str(getattr(_email_cfg, "CLOUDMAIL_API_BASE", "") or "").strip():
            return "CloudMail API 地址未配置（配置 → 通用邮箱 / OTP）"
        return None
    return f"未知换绑邮箱来源: {source}"


def _relogin_delay() -> None:
    lo, hi = RELOGIN_DELAY_SECONDS
    time.sleep(random.uniform(lo, hi))


def _pick_rebind_proxy() -> tuple[str, str]:
    """从「代理池(每行一个)」（config.proxy.PROXY_POOL）随机抽一条；池为空回退直连。"""
    from config import proxy as proxy_cfg
    pool = [str(p or "").strip() for p in (getattr(proxy_cfg, "PROXY_POOL", []) or [])]
    pool = [p for p in pool if p]
    if not pool:
        return "", "代理池为空，直连"
    return random.choice(pool), "代理池随机"


def _token_expired(token: str) -> bool:
    """本地解析 accessToken JWT 的 exp；解析不出 exp 时交给接口验证（返回未过期）。"""
    from core.chatgpt_plan import decode_jwt_payload_unverified
    claims = decode_jwt_payload_unverified(token)
    exp = claims.get("exp")
    if isinstance(exp, (int, float)):
        return float(exp) <= time.time()
    return False


def _pick_new_email(source: str) -> tuple[dict | None, str | None, str | None]:
    """
    领取换绑新邮箱。返回 (pool_row, new_email, error)。
    pool_row 仅 source=pool 时非空；CloudMail 直接生成随机邮箱。
    """
    if source == "pool":
        row = db.claim_rebind_email()
        if row is None:
            return None, None, "换绑邮箱池没有可用邮箱"
        return row, str(row.get("email") or ""), None
    from core import cloudmail_client
    account = cloudmail_client.pick_account()
    return None, str(account.email), None


def _new_email_material(source: str, pool_row: dict | None, new_email: str) -> str:
    if source == "pool" and pool_row:
        return str(pool_row.get("copy_line") or new_email)
    return new_email


def _fetch_new_email_otp(new_email: str, source: str, pool_row: dict | None, *, after_ts: float, used_codes: frozenset[str], max_wait: int) -> str:
    if source == "pool" and pool_row and str(pool_row.get("kind") or "") == "generic_api":
        from core.email_change import fetch_generic_api_otp
        return fetch_generic_api_otp(
            new_email, str(pool_row.get("code_url") or ""), after_ts=after_ts, max_wait=max_wait,
        )
    return fetch_cloudmail_otp(
        new_email, after_ts=after_ts, used_codes=used_codes, max_wait=max_wait,
    )


def _warm_chatgpt_session(proxy: str | None) -> "object":
    """建一个会话并预热 chatgpt.com/auth.openai.com cookie（不带邮箱不触发 OTP）。"""
    from core.openai_auth import network_preflight
    from core.session import BrowserSession

    session = BrowserSession(proxy=proxy if proxy else None)
    logger.info(
        "[换绑] 会话创建完成：proxy=%s device_id=%s",
        session.proxy or "配置随机/直连", session.device_id,
    )
    network_preflight(session)
    return session


def _relogin_new_email(
    new_email: str,
    source: str,
    pool_row: dict | None,
    result: dict,
    *,
    proxy: str | None,
    used_codes: set[str],
) -> dict:
    """最后阶段：用新邮箱重新协议登录，拿新 token；成功/异常都写入 result。"""
    result["stage"] = "relogin"

    def new_otp_fetch(after_ts: float, **kwargs) -> str:
        code = _fetch_new_email_otp(
            new_email, source, pool_row,
            after_ts=after_ts,
            used_codes=frozenset(used_codes),
            max_wait=kwargs.get("max_wait", 120),
        )
        used_codes.add(code)
        return code

    session2, login_info2 = protocol_login(new_email, new_otp_fetch, proxy=proxy)
    result["access_token"] = login_info2["access_token"]
    result["session"] = login_info2.get("session_info") or {}
    result["device_id"] = getattr(session2, "device_id", None)
    result["success"] = True
    result["ok"] = True
    result["completed_at"] = _now()
    logger.info("[换绑] 全流程成功：%s → %s", result.get("email"), new_email)
    return result


def run_rebind(
    *,
    account_id: int,
    email: str,
    source: str = "pool",
    stored_token: str | None = None,
    proxy: str | None = None,
) -> dict:
    """
    执行一个账号的完整换绑流程；异常不外抛，返回带 stage 的结果 dict。

    Args:
        source: 新邮箱来源，pool=换绑邮箱池 / cloudmail=CloudMail 随机生成
        stored_token: 账号已保存的 accessToken；非空且未过期时直接用它调 backend-api，
                      跳过旧邮箱登录（begin 返回 401/403 才回退登录）
        proxy: 代理；None=从「代理池(每行一个)」随机抽取，""=直连
    """
    started_at = _now()
    result = {
        "ok": False,
        "success": False,
        "email": email,
        "new_email": None,
        "access_token": None,
        "old_access_token": None,
        "stage": None,
        "rebind_effective": False,
        "new_email_line": None,
        "started_at": started_at,
        "completed_at": None,
        "error": None,
    }
    session = None
    pool_row: dict | None = None
    claimed_pool_email: str | None = None
    try:
        # ============ 代理出口：代理池(每行一个)随机抽取 ============
        if proxy is None:
            selected_proxy, proxy_note = _pick_rebind_proxy()
        else:
            selected_proxy = str(proxy or "").strip()
            proxy_note = "调用方指定直连" if not selected_proxy else "调用方指定"
        logger.info(
            "[换绑] 开始：%s source=%s 代理出口=%s（%s）",
            email, source, _mask_proxy(selected_proxy) or "直连", proxy_note,
        )

        # ============ 阶段 1：建立登录态（优先复用已保存 accessToken，跳过登录）============
        result["stage"] = "session"
        token = str(stored_token or "").strip()
        if token and _token_expired(token):
            logger.info("[换绑] 已保存 accessToken 已过期（JWT exp），跳过复用，改走旧邮箱登录")
            token = ""
        used_stored = bool(token)
        if used_stored:
            try:
                session = _warm_chatgpt_session(selected_proxy)
                result["old_access_token"] = token
                logger.info(
                    "[换绑] 已有 accessToken 且未过期，跳过旧邮箱登录，直接发起换绑"
                    "（若 token 被拒会自动回退登录）：%s...", token[:16],
                )
            except Exception as exc:
                logger.warning("[换绑] 复用已保存 token 预热失败，改走旧邮箱登录：%s", str(exc)[:180])
                session = None
                used_stored = False

        if not used_stored:
            result["stage"] = "login_old"

            def old_otp_fetch(after_ts: float, **kwargs) -> str:
                from core.email_provider import wait_for_otp
                return wait_for_otp(email, after_ts=after_ts, max_wait=kwargs.get("max_wait", 120))

            session, login_info = protocol_login(email, old_otp_fetch, proxy=selected_proxy)
            token = str(login_info["access_token"] or "")
            result["old_access_token"] = token
            logger.info("[换绑] 旧邮箱登录成功：%s，token=%s...", email, token[:16])

        # ============ 阶段 2：领取新邮箱 ============
        result["stage"] = "claim_new_email"
        pool_row, new_email, claim_error = _pick_new_email(source)
        if claim_error:
            raise ChangeEmailError(claim_error)
        claimed_pool_email = str(pool_row.get("email") or "") if pool_row else None
        result["new_email"] = new_email
        result["new_email_line"] = _new_email_material(source, pool_row, new_email)
        if not db.mark_account_rebind_running(account_id, new_email=new_email):
            raise ChangeEmailError("账号换绑状态已被重置，取消执行")
        logger.info("[换绑] 已领取新邮箱: %s（source=%s）", new_email, source)

        # ============ 阶段 3：begin → 取码 → verify ============
        # used_codes 保证换绑验证码不会被重新登录阶段重复使用。
        used_codes: set[str] = set()
        fell_back_to_login = False
        last_error: Exception | None = None

        def _fallback_old_login() -> str:
            """回退旧邮箱 OTP 登录，重建会话与 token；返回新 token。"""
            nonlocal session
            result["stage"] = "login_old"

            def old_otp_fetch(after_ts: float, **kwargs) -> str:
                from core.email_provider import wait_for_otp
                return wait_for_otp(email, after_ts=after_ts, max_wait=kwargs.get("max_wait", 120))

            session, login_info = protocol_login(email, old_otp_fetch, proxy=selected_proxy)
            new_token = str(login_info["access_token"] or "")
            result["old_access_token"] = new_token
            logger.info("[换绑] 旧邮箱登录成功（回退路径）：%s", email)
            return new_token

        attempt = 0
        while attempt < CHANGE_EMAIL_VERIFY_ATTEMPTS:
            result["stage"] = "change_email"
            begin_after_ts = time.time()
            try:
                change_email_begin(session, token, new_email)
            except ChangeEmailReauthRequiredError as exc:
                # token 本身有效，但换绑是敏感操作、要求"最近登录过"的会话态：
                # 回退旧邮箱 OTP 登录建立会话后重试（不消耗 verify 重试次数）。
                if used_stored and not fell_back_to_login:
                    fell_back_to_login = True
                    logger.warning(
                        "[换绑] token 未过期但换绑接口要求最近登录态（reauth_required），"
                        "回退旧邮箱 OTP 登录建立会话：%s", str(exc)[:140],
                    )
                    token = _fallback_old_login()
                    continue
                raise
            except ChangeEmailError as exc:
                # 复用已保存 token 被接口拒绝（401/403，可能已失效/被撤销）：回退登录再试。
                auth_failed = ("status=401" in str(exc) or "status=403" in str(exc))
                if auth_failed and used_stored and not fell_back_to_login:
                    fell_back_to_login = True
                    logger.warning(
                        "[换绑] 已保存 token 被接口拒绝（可能已失效/被撤销），回退旧邮箱 OTP 登录：%s",
                        str(exc)[:140],
                    )
                    token = _fallback_old_login()
                    continue
                raise

            attempt += 1
            code = _fetch_new_email_otp(
                new_email, source, pool_row,
                after_ts=begin_after_ts,
                used_codes=frozenset(used_codes),
                max_wait=180,
            )
            used_codes.add(code)
            try:
                change_email_verify(session, token, new_email, code)
                last_error = None
                break
            except ChangeEmailOtpInvalidError as exc:
                last_error = exc
                if attempt >= CHANGE_EMAIL_VERIFY_ATTEMPTS:
                    raise ChangeEmailError(f"换绑验证码重试耗尽: {exc}") from exc
                logger.warning(
                    "[换绑] verify 失败（第 %s/%s 次）：%s，重新 begin 再取码",
                    attempt, CHANGE_EMAIL_VERIFY_ATTEMPTS, str(exc)[:180],
                )
        if last_error is not None:
            raise last_error

        result["rebind_effective"] = True
        logger.info("[换绑] 换绑完成：%s → %s，准备重新登录", email, new_email)

        # ============ 阶段 4：用新邮箱重新登录 ============
        _relogin_delay()
        return _relogin_new_email(
            new_email, source, pool_row, result, proxy=selected_proxy, used_codes=used_codes,
        )

    except Exception as exc:
        logger.error("[换绑] 失败：%s（stage=%s），%s: %s",
                     email, result.get("stage"), type(exc).__name__, exc)
        result["ok"] = False
        result["success"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"[:800]
        result["completed_at"] = _now()
        return result
    finally:
        # ============ 新邮箱善后：成功=已用；begin 被拒=标失败；其余回收为可用 ============
        if claimed_pool_email:
            if result.get("rebind_effective"):
                db.release_rebind_email(
                    claimed_pool_email, status="used",
                    note=f"已换绑 {email} → {result.get('new_email')}",
                )
            elif str(result.get("stage") or "") == "change_email" and "换绑请求失败" in str(result.get("error") or ""):
                db.release_rebind_email(
                    claimed_pool_email, status="failed",
                    note=str(result.get("error") or "")[:200],
                )
            else:
                db.release_rebind_email(
                    claimed_pool_email, status="available",
                    note=f"换绑失败回收：{str(result.get('error') or '')[:120]}",
                )


def _run_rebind_task(*, account_id: int, email: str, source: str) -> dict:
    try:
        with _LOCK:
            _RUNNING.add(int(account_id))
        if not db.mark_account_rebind_running(account_id):
            _append_log(email, "[换绑] 账号已删除或换绑状态已被重置，取消执行")
            return {"ok": False, "error": "账号已删除或换绑状态已被重置"}
        _append_log(email, f"[换绑] 开始后台执行 source={source}", clear=True)

        acc = db.get_account(account_id) or {}
        stored_token = str(acc.get("access_token") or "").strip()

        root_logger = logging.getLogger()
        thread_name = threading.current_thread().name
        fh = logging.FileHandler(str(log_path(email)), encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S",
        ))
        fh.addFilter(lambda record: record.threadName == thread_name)
        root_logger.addHandler(fh)
        try:
            result = run_rebind(
                account_id=account_id,
                email=email,
                source=source,
                stored_token=stored_token,
                proxy=None,
            )
        finally:
            try:
                root_logger.removeHandler(fh)
                fh.close()
            except Exception:
                pass

        db.update_account_rebind(account_id, result)
        if result.get("ok"):
            _append_log(
                email,
                f"[换绑] 完成：{email} → {result.get('new_email')}，已刷新新 accessToken",
            )
        elif result.get("rebind_effective"):
            _append_log(
                email,
                f"[换绑] 换绑已生效但重新登录失败：账号邮箱已改为 {result.get('new_email')}，"
                "请稍后用「查活」刷新最新 Token",
            )
        else:
            _append_log(email, f"[换绑] 失败：{result.get('error')}")
        return result
    except Exception as exc:
        logger.exception("[换绑] 后台异常: %s", email)
        result = {
            "ok": False,
            "rebind_effective": False,
            "completed_at": _now(),
            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
        }
        try:
            db.update_account_rebind(account_id, result)
        except Exception:
            logger.exception("[换绑] 写入异常状态失败: account_id=%s", account_id)
        try:
            _append_log(email, f"[换绑] 后台异常：{result['error']}")
        except Exception:
            pass
        return result
    finally:
        with _LOCK:
            _RUNNING.discard(int(account_id))
        _QUEUE_SLOTS.release()


def enqueue_account_rebind(*, account_id: int, email: str, source: str = "pool") -> dict:
    """把单账号换绑加入后台队列。返回 {accepted, busy, queue_full, error?}。"""
    account_id = int(account_id)
    email = str(email or "").strip()
    if not email:
        return {"accepted": False, "busy": False, "error": "email 为空"}
    if source not in ("pool", "cloudmail"):
        return {"accepted": False, "busy": False, "error": f"未知换绑邮箱来源: {source}"}
    precheck_error = precheck_source(source)
    if precheck_error:
        return {"accepted": False, "busy": False, "error": precheck_error}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "换绑队列已满，请稍后重试"}
    if not db.claim_account_rebind(account_id, source=source):
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在换绑"}

    _append_log(email, f"[换绑] 已入队 account_id={account_id} source={source}", clear=True)
    try:
        _EXECUTOR.submit(
            _run_rebind_task,
            account_id=account_id,
            email=email,
            source=str(source),
        )
    except Exception as exc:
        _QUEUE_SLOTS.release()
        result = {
            "ok": False,
            "rebind_effective": False,
            "completed_at": _now(),
            "error": f"换绑入队失败: {type(exc).__name__}: {str(exc)[:160]}",
        }
        db.update_account_rebind(account_id, result)
        _append_log(email, result["error"])
        return {"accepted": False, "busy": False, "error": result["error"]}

    return {
        "accepted": True,
        "busy": False,
        "account_id": account_id,
        "email": email,
        "status": "queued",
        "source": str(source),
    }
