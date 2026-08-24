# -*- coding: utf-8 -*-
"""
RoxyBrowser 注册流程 CDP HAR 采集器。

在真实指纹浏览器注册过程中，通过 Chrome DevTools Protocol 采集完整请求链路
（Network.* 事件），导出为标准 HAR JSON；同时 dump 一份 JS 侧指纹快照。
产物可直接喂给 tools/analyze_har_protocol.py 反哺纯协议对齐。

设计约束：
- 纯采集、可降级：任何异常只记日志，绝不抛进注册主流程。
- 默认关闭：ROXY_CAPTURE_HAR=False 时不产生任何开销。
- 后台 daemon 线程收 CDP 事件，不阻塞 Selenium 主流程。
- 敏感值脱敏：cookie / authorization / sentinel-token 等头与 OTP/密码请求体
  默认打码（保留字段名与长度），ROXY_HAR_REDACT=False 时保留原始值。
"""
from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_OUTPUT_DIR = _PROJECT_ROOT / "har_captures"

# 敏感头：值打码，保留名字和长度（与 tools/analyze_har_protocol.py 口径一致）。
_SENSITIVE_HEADERS = frozenset({
    "authorization", "cookie", "set-cookie",
    "openai-sentinel-token", "openai-sentinel-so-token",
    "x-access-token", "proxy-authorization",
})
# post body JSON 里打码的字段（OTP/密码）；"p" 是指纹数据，不脱敏。
_SENSITIVE_POST_FIELDS = frozenset({"code", "password", "passcode", "otp", "totp", "totp_code"})

# getResponseBody 单响应体大小上限与命令等待超时
_MAX_RESPONSE_BODY_BYTES = 300_000
_GET_BODY_TIMEOUT = 4.0
_WS_READ_TIMEOUT = 0.5
_WS_CONNECT_TIMEOUT = 15
# 连续收包失败超过该次数后退出 reader 线程（约 10 秒），避免浏览器提前关闭时死循环
_MAX_CONSECUTIVE_READ_ERRORS = 20

# JS 侧指纹快照：与纯协议 generate_fingerprint_data()（core/sentinel.py）的
# 25 维 p 数组字段对应，方便拿真实浏览器真值回来对齐/校验纯协议画像。
_FINGERPRINT_SCRIPT = r"""
(() => {
  const out = {};
  try {
    const nav = navigator;
    out.userAgent = nav.userAgent || null;
    out.appVersion = nav.appVersion || null;
    out.platform = nav.platform || null;
    out.language = nav.language || null;
    out.languages = Array.isArray(nav.languages) ? nav.languages : [];
    out.hardwareConcurrency = nav.hardwareConcurrency ?? null;
    out.deviceMemory = nav.deviceMemory ?? null;
    out.vendor = nav.vendor || null;
    out.webdriver = nav.webdriver;
    out.screen = {
      width: screen.width, height: screen.height,
      availWidth: screen.availWidth, availHeight: screen.availHeight,
      colorDepth: screen.colorDepth, pixelDepth: screen.pixelDepth,
    };
    out.windowSize = {
      innerWidth: window.innerWidth, innerHeight: window.innerHeight,
      outerWidth: window.outerWidth, outerHeight: window.outerHeight,
      devicePixelRatio: window.devicePixelRatio || null,
    };
    out.timezone = (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) { return null; } })();
    out.timezoneOffset = new Date().getTimezoneOffset();
    out.buildId = (document.documentElement && document.documentElement.dataset && document.documentElement.dataset.build) || null;
    out.url = location.href || null;
    out.cookieKeys = document.cookie.split(';').map(c => c.split('=')[0].trim()).filter(Boolean);
    try { out.localStorageKeys = Object.keys(localStorage); } catch (e) { out.localStorageKeys = null; }
    try { out.sessionStorageKeys = Object.keys(sessionStorage); } catch (e) { out.sessionStorageKeys = null; }
    const fnv1a = (s) => {
      let h = 2166136261;
      for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619) >>> 0; }
      h ^= h >>> 16; h = Math.imul(h, 2246822507) >>> 0;
      h ^= h >>> 13; h = Math.imul(h, 3266489909) >>> 0; h ^= h >>> 16;
      return (h >>> 0).toString(16);
    };
    out.canvas = (() => {
      try {
        const c = document.createElement('canvas'); c.width = 300; c.height = 150;
        const ctx = c.getContext('2d');
        ctx.textBaseline = 'top'; ctx.font = '14px Arial';
        ctx.fillStyle = '#f60'; ctx.fillRect(0, 0, 300, 150);
        ctx.fillStyle = '#069'; ctx.fillText('hello, canvas fingerprint 123', 10, 20);
        const data = c.toDataURL();
        return { hash: fnv1a(data), length: data.length };
      } catch (e) { return null; }
    })();
    out.webgl = (() => {
      try {
        const c = document.createElement('canvas');
        const gl = c.getContext('webgl') || c.getContext('experimental-webgl');
        if (!gl) return null;
        const dbg = gl.getExtension('WEBGL_debug_renderer_info');
        return {
          renderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
          vendor: dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR),
          version: gl.getParameter(gl.VERSION),
          shadingLanguageVersion: gl.getParameter(gl.SHADING_LANGUAGE_VERSION),
        };
      } catch (e) { return null; }
    })();
    try { out.perfTimeOrigin = performance.timeOrigin || null; } catch (e) { out.perfTimeOrigin = null; }
    out.windowFlags = {
      ai: 'ai' in window,
      InstallTrigger: 'InstallTrigger' in window,
      cache: 'cache' in window,
      data: 'data' in window,
      solana: 'solana' in window,
      dump: 'dump' in window,
      requestIdleCallback: 'requestIdleCallback' in window,
    };
  } catch (e) {
    out.error = String((e && e.message) || e);
  }
  return out;
})()
"""


def _iso(ts) -> str:
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def _safe_email(email: str) -> str:
    return re.sub(r"[^A-Za-z0-9._@-]+", "_", str(email or "")).strip("_") or "unknown"


def capture_js_fingerprint(driver) -> dict:
    """通过 Selenium driver 抓取当前页面/浏览器的 JS 指纹快照。失败返回空 dict。"""
    try:
        raw = driver.execute_script(_FINGERPRINT_SCRIPT)
        return raw if isinstance(raw, dict) else {}
    except Exception as exc:
        logger.debug("[HarRecorder] JS 指纹采集失败：%s: %s", type(exc).__name__, exc)
        return {}


class CdpHarRecorder:
    """通过原始 websocket 连接 Chrome DevTools Protocol，采集 Network.* 事件并导出 HAR。

    - 连接目标：优先用 debugger_address 的 /json/list 找 page target 的
      webSocketDebuggerUrl；拿不到时退回 Roxy open 返回的 ws_endpoint。
    - 事件在后台 daemon 线程接收，通过 requestId 聚合为 HAR entry。
    - 所有对外方法都不向调用方抛错（内部捕获并记日志）。
    """

    def __init__(
        self,
        debugger_address: str | None = None,
        ws_endpoint: str | None = None,
        email: str = "",
        output_dir: str | Path | None = None,
        redact: bool = True,
    ):
        self._debugger_address = debugger_address
        self._ws_endpoint = ws_endpoint
        self._email = email
        self._output_dir = Path(output_dir) if output_dir else None
        self._redact = redact

        self._ws = None
        self._thread: threading.Thread | None = None
        self._running = False

        self._cmd_id = 0
        self._pending: dict[int, dict] = {}          # cmd_id -> {"event", "result", "error"}
        self._lock = threading.Lock()

        self._entries: dict[str, dict] = {}          # hop key -> entry
        self._entry_order: list[str] = []            # hop key 出现顺序
        self._last_key: dict[str, str] = {}          # 真实 requestId -> 当前最后一个 hop key
        self._fingerprint: dict | None = None

    # ------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------

    def start(self) -> bool:
        """连接 CDP 并开启网络采集。失败返回 False，不抛错。"""
        url = self._resolve_page_ws_url()
        if not url:
            logger.warning("[HarRecorder] 无法解析页面 CDP websocket 地址（debugger=%s ws=%s）",
                           self._debugger_address or "-", self._ws_endpoint or "-")
            return False
        try:
            import websocket  # 延迟导入，避免硬依赖
            # suppress_origin=True：Chrome 111+ 的 CDP 会拒绝带 Origin 头的 websocket
            # 握手（403 Forbidden "...Use --remote-allow-origins=..."）。不带 Origin 头时，
            # Chrome 把本地连接当作可信连接放行，这是绕过该限制的标准做法。
            self._ws = websocket.create_connection(
                url,
                timeout=_WS_CONNECT_TIMEOUT,
                enable_multithread=True,
                suppress_origin=True,
            )
            self._ws.settimeout(_WS_READ_TIMEOUT)
        except Exception as exc:
            logger.warning("[HarRecorder] CDP websocket 连接失败：%s: %s", type(exc).__name__, exc)
            self._ws = None
            return False

        self._running = True
        self._thread = threading.Thread(target=self._reader_loop, name="cdp-har-reader", daemon=True)
        self._thread.start()

        # 开启网络采集（在导航到登录页之前，尽量覆盖注册相关请求）。
        try:
            self._send_command("Network.enable", {
                "maxTotalBufferSize": 100_000_000,
                "maxResourceBufferSize": 50_000_000,
                "maxPostDataSize": 10_000_000,
            })
            self._send_command("Page.enable", {})
        except Exception as exc:
            logger.warning("[HarRecorder] Network.enable 失败，改为仅记录已发生事件：%s: %s",
                           type(exc).__name__, exc)
        logger.info("[HarRecorder] 已连接 CDP，开始采集（email=%s）", self._email or "-")
        return True

    def capture_js_fingerprint(self, driver) -> None:
        """采集 JS 指纹快照（注册流程开始时调用，趁页面尚未跳转）。"""
        self._fingerprint = capture_js_fingerprint(driver)

    def stop(self, driver=None) -> tuple | None:
        """结束采集：拉取 JSON 响应体 → 导出 HAR + 指纹 → 关闭连接。返回 (har_path, fingerprint_path)。"""
        if self._fingerprint is None and driver is not None:
            self._fingerprint = capture_js_fingerprint(driver)
        try:
            self._fetch_json_bodies()
        except Exception as exc:
            logger.debug("[HarRecorder] 拉取响应体失败：%s: %s", type(exc).__name__, exc)

        har = self.build_har()
        paths = None
        try:
            paths = self._save(har)
        except Exception as exc:
            logger.warning("[HarRecorder] 保存 HAR 失败：%s: %s", type(exc).__name__, exc)

        self._running = False
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:
            pass
        self._ws = None
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3)
        entry_count = len(har.get("log", {}).get("entries", []))
        logger.info("[HarRecorder] 采集结束：请求=%d 条，HAR=%s，指纹=%s",
                    entry_count, (paths[0] if paths else "-"), (paths[1] if paths and len(paths) > 1 else "-"))
        return paths

    def build_har(self) -> dict:
        """把采集到的事件聚合为标准 HAR（兼容 tools/analyze_har_protocol.py）。"""
        entries = []
        for key in self._entry_order:
            e = self._entries.get(key)
            if e is None:
                continue
            req = e.get("request") or {}
            resp = e.get("response") or {}
            post_text = str(req.get("post_text") or "")
            request_obj = {
                "method": req.get("method") or "",
                "url": req.get("url") or "",
                "httpVersion": "HTTP/2",
                "headers": req.get("headers") or [],
                "queryString": [],
                "cookies": [],
                "headersSize": -1,
                "bodySize": len(post_text) if post_text else -1,
            }
            if post_text:
                request_obj["postData"] = {"mimeType": "application/json", "text": post_text}
            body_size = int(resp.get("body_size") or -1)
            response_obj = {
                "status": int(resp.get("status") or 0),
                "statusText": resp.get("statusText") or "",
                "httpVersion": "HTTP/2",
                "headers": resp.get("headers") or [],
                "cookies": [],
                "content": {
                    "size": body_size,
                    "mimeType": resp.get("mime_type") or "",
                    "text": resp.get("body_text") or "",
                },
                "redirectURL": resp.get("redirect_url") or "",
                "headersSize": -1,
                "bodySize": body_size,
            }
            entries.append({
                "startedDateTime": _iso(e.get("started_at")),
                "time": round(float(e.get("total_ms") or 0), 3),
                "request": request_obj,
                "response": response_obj,
                "cache": {},
                "timings": e.get("timings") or {"send": 0, "wait": 0, "receive": 0},
            })
        return {
            "log": {
                "version": "1.2",
                "creator": {"name": "gpt-free-register cdp-har-recorder", "version": "1.0"},
                "entries": entries,
            }
        }

    # ------------------------------------------------------------
    # CDP 连接与命令
    # ------------------------------------------------------------

    def _resolve_page_ws_url(self) -> str | None:
        """解析页面级 websocket 地址。优先 debugger_address 的 /json/list。"""
        if self._debugger_address:
            try:
                base = self._debugger_address if "://" in self._debugger_address else "http://" + self._debugger_address
                base = base.rstrip("/")
                with urllib.request.urlopen(base + "/json/list", timeout=5) as resp:
                    targets = json.loads(resp.read().decode("utf-8", "replace"))
                pages = [t for t in targets if isinstance(t, dict) and t.get("type") == "page"]
                for t in pages:
                    u = str(t.get("url") or "").lower()
                    if "chatgpt.com" in u or "openai.com" in u:
                        return t.get("webSocketDebuggerUrl") or None
                if pages:
                    return pages[0].get("webSocketDebuggerUrl") or None
            except Exception as exc:
                logger.debug("[HarRecorder] /json/list 解析失败：%s: %s", type(exc).__name__, exc)
        if self._ws_endpoint:
            return self._ws_endpoint
        return None

    def _send_command(self, method: str, params: dict | None = None) -> dict:
        """发送 CDP 命令并同步等待响应；失败抛异常。"""
        with self._lock:
            self._cmd_id += 1
            cmd_id = self._cmd_id
            holder = {"event": threading.Event(), "result": None, "error": None}
            self._pending[cmd_id] = holder
        payload = json.dumps({"id": cmd_id, "method": method, "params": params or {}}, separators=(",", ":"))
        try:
            self._ws.send(payload)
        except Exception:
            with self._lock:
                self._pending.pop(cmd_id, None)
            raise
        holder["event"].wait(timeout=_GET_BODY_TIMEOUT)
        with self._lock:
            self._pending.pop(cmd_id, None)
            result, error = holder["result"], holder["error"]
        if error is not None:
            raise RuntimeError(f"CDP {method} 错误: {error}")
        return result

    def _reader_loop(self) -> None:
        """后台线程：持续 recv CDP 消息并分发。"""
        consecutive_errors = 0
        while self._running:
            try:
                raw = self._ws.recv()
            except Exception:
                if not self._running:
                    break
                consecutive_errors += 1
                if consecutive_errors >= _MAX_CONSECUTIVE_READ_ERRORS:
                    logger.warning("[HarRecorder] CDP 连续收包失败 %s 次，reader 线程退出",
                                   consecutive_errors)
                    break
                time.sleep(0.05)
                continue
            consecutive_errors = 0
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if not isinstance(msg, dict):
                continue
            if "id" in msg:
                self._fulfill(msg)
            elif "method" in msg:
                self._dispatch_event(msg)

    def _fulfill(self, msg: dict) -> None:
        with self._lock:
            holder = self._pending.pop(msg.get("id"), None)
        if holder is None:
            return
        if msg.get("error") is not None:
            holder["error"] = msg.get("error")
        else:
            holder["result"] = msg.get("result") or {}
        holder["event"].set()

    def _dispatch_event(self, msg: dict) -> None:
        method = msg.get("method", "")
        params = msg.get("params") or {}
        try:
            if method == "Network.requestWillBeSent":
                self._on_request_will_be_sent(params)
            elif method == "Network.requestWillBeSentExtraInfo":
                self._on_request_extra_info(params)
            elif method == "Network.responseReceived":
                self._on_response_received(params)
            elif method == "Network.dataReceived":
                self._on_data_received(params)
            elif method == "Network.loadingFinished":
                self._on_loading_finished(params)
            elif method == "Network.loadingFailed":
                self._on_loading_failed(params)
        except Exception as exc:
            logger.debug("[HarRecorder] 处理 %s 事件失败：%s: %s", method, type(exc).__name__, exc)

    # ------------------------------------------------------------
    # 事件聚合
    # ------------------------------------------------------------

    def _new_hop_key(self, request_id: str, params: dict) -> str:
        """同一 requestId 出现多次（重定向链）时，为每次跳转分配独立 key。"""
        redirect = params.get("redirectResponse")
        if redirect and request_id in self._last_key:
            prev_key = self._last_key[request_id]
            prev = self._entries.get(prev_key)
            if prev is not None and prev.get("response") is None:
                prev["response"] = self._build_response(redirect, redirect_hop=True)
        n = 1
        while True:
            key = request_id if n == 1 else f"{request_id}#{n}"
            if key not in self._entries:
                break
            n += 1
        self._last_key[request_id] = key
        self._entry_order.append(key)
        return key

    def _on_request_will_be_sent(self, params: dict) -> None:
        req_id = str(params.get("requestId") or "")
        if not req_id:
            return
        request = params.get("request") or {}
        key = self._new_hop_key(req_id, params)
        post_text = str(request.get("postData") or "")
        if post_text and self._redact:
            post_text = self._redact_post(post_text)
        self._entries[key] = {
            "_request_id": req_id,
            "started_at": time.time(),
            "start_ts": params.get("timestamp"),
            "type": str(params.get("type") or ""),
            "request": {
                "url": str(request.get("url") or ""),
                "method": str(request.get("method") or ""),
                "headers": self._normalize_headers(request.get("headers")),
                "post_text": post_text or None,
            },
            "response": None,
            "_response_ts": None,
            "_end_ts": None,
            "timings": {"send": 0, "wait": 0, "receive": 0},
            "total_ms": 0,
        }

    def _on_request_extra_info(self, params: dict) -> None:
        req_id = str(params.get("requestId") or "")
        key = self._last_key.get(req_id)
        entry = self._entries.get(key)
        if entry is None:
            return
        extra = params.get("headers")
        if not isinstance(extra, dict):
            return
        existing = {h["name"].lower() for h in entry["request"]["headers"]}
        for name, value in extra.items():
            if name.lower() not in existing and value is not None:
                appended = {"name": str(name), "value": str(value)}
                if self._redact and appended["name"].lower() in _SENSITIVE_HEADERS and appended["value"]:
                    appended["value"] = f"<redacted:len={len(appended['value'])}>"
                entry["request"]["headers"].append(appended)
        # 注意：这里不能对整表再 _redact_headers()，否则会把已脱敏的 <redacted:len=N>
        # 当原始值二次打码（长度会翻倍失真）。

    def _on_response_received(self, params: dict) -> None:
        req_id = str(params.get("requestId") or "")
        key = self._last_key.get(req_id)
        entry = self._entries.get(key)
        if entry is None:
            return
        response = params.get("response") or {}
        entry["response"] = self._build_response(response)
        entry["_response_ts"] = params.get("timestamp")

    def _on_data_received(self, params: dict) -> None:
        req_id = str(params.get("requestId") or "")
        key = self._last_key.get(req_id)
        entry = self._entries.get(key)
        if entry is None or entry.get("response") is None:
            return
        size = int(params.get("encodedDataLength") or 0)
        entry["response"]["body_size"] = max(entry["response"].get("body_size") or 0, size)

    def _on_loading_finished(self, params: dict) -> None:
        req_id = str(params.get("requestId") or "")
        key = self._last_key.get(req_id)
        entry = self._entries.get(key)
        if entry is None:
            return
        entry["_end_ts"] = params.get("timestamp")
        start, resp_ts, end = entry.get("start_ts"), entry.get("_response_ts"), entry.get("_end_ts")
        if start is None:
            return
        resp_ts = start if resp_ts is None else resp_ts
        end = start if end is None else end
        entry["timings"] = {
            "send": round(max(0.0, (resp_ts - start) * 1000), 3),
            "wait": 0,
            "receive": round(max(0.0, (end - resp_ts) * 1000), 3),
        }
        entry["total_ms"] = round(max(0.0, (end - start) * 1000), 3)

    def _on_loading_failed(self, params: dict) -> None:
        req_id = str(params.get("requestId") or "")
        key = self._last_key.get(req_id)
        entry = self._entries.get(key)
        if entry is None:
            return
        entry["_loading_failed"] = str(params.get("errorText") or "")

    # ------------------------------------------------------------
    # HAR 拼装辅助
    # ------------------------------------------------------------

    def _build_response(self, response: dict, redirect_hop: bool = False) -> dict:
        headers = self._normalize_headers(response.get("headers"))
        redirect_url = ""
        if redirect_hop:
            for h in headers:
                if h["name"].lower() == "location":
                    redirect_url = h["value"]
                    break
        return {
            "status": int(response.get("status") or 0),
            "statusText": str(response.get("statusText") or ""),
            "headers": headers,
            "mime_type": str(response.get("mimeType") or ""),
            "body_text": None,
            "body_size": -1,
            "redirect_url": redirect_url,
        }

    def _normalize_headers(self, headers) -> list[dict]:
        if not headers:
            return []
        if isinstance(headers, dict):
            items = [{"name": str(k), "value": str(v)} for k, v in headers.items() if v is not None]
        elif isinstance(headers, list):
            items = []
            for h in headers:
                if isinstance(h, dict):
                    items.append({"name": str(h.get("name") or ""), "value": str(h.get("value") or "")})
        else:
            return []
        return self._redact_headers(items)

    def _redact_headers(self, headers: list[dict]) -> list[dict]:
        if not self._redact:
            return headers
        out = []
        for h in headers:
            name = str(h.get("name") or "")
            value = str(h.get("value") or "")
            if name.lower() in _SENSITIVE_HEADERS and value:
                out.append({"name": name, "value": f"<redacted:len={len(value)}>"})
            else:
                out.append({"name": name, "value": value})
        return out

    def _redact_post(self, text: str) -> str:
        if not self._redact or not text:
            return text
        try:
            obj = json.loads(text)
        except Exception:
            return text
        if isinstance(obj, dict):
            for key in list(obj.keys()):
                if key in _SENSITIVE_POST_FIELDS and obj[key] is not None:
                    value = obj[key]
                    obj[key] = f"<redacted:len={len(str(value))}>"
            return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        return text

    def _fetch_json_bodies(self) -> None:
        """best-effort 拉取 XHR/Fetch JSON 响应体，供协议对齐分析。"""
        if self._ws is None or not self._running:
            return
        for key in self._entry_order:
            entry = self._entries.get(key)
            if entry is None or entry.get("response") is None:
                continue
            resp = entry["response"]
            mime = str(resp.get("mime_type") or "").lower()
            req_type = str(entry.get("type") or "")
            if "json" not in mime or req_type not in ("XHR", "Fetch"):
                continue
            try:
                result = self._send_command("Network.getResponseBody", {"requestId": entry["_request_id"]})
            except Exception as exc:
                logger.debug("[HarRecorder] getResponseBody 失败 %s：%s", entry["_request_id"], exc)
                continue
            if not isinstance(result, dict):
                continue
            body = result.get("body") or ""
            if result.get("base64Encoded"):
                try:
                    body = base64.b64decode(body).decode("utf-8", "replace")
                except Exception:
                    continue
            if len(body) > _MAX_RESPONSE_BODY_BYTES:
                body = body[:_MAX_RESPONSE_BODY_BYTES] + "\n<!--truncated-->"
            resp["body_text"] = body
            resp["body_size"] = len(body)

    def _save(self, har: dict) -> tuple:
        out_dir = self._output_dir or _DEFAULT_OUTPUT_DIR
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            out_dir = _DEFAULT_OUTPUT_DIR
            out_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        safe = _safe_email(self._email)
        har_path = out_dir / f"{safe}-{ts}-har.json"
        har_path.write_text(json.dumps(har, ensure_ascii=False, indent=2), encoding="utf-8")
        fp_path = None
        if self._fingerprint:
            fp_path = out_dir / f"{safe}-{ts}-fingerprint.json"
            fp_path.write_text(json.dumps(self._fingerprint, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(har_path), str(fp_path) if fp_path else None


def start_har_recorder(opened, email: str = "", output_dir: str | Path | None = None) -> CdpHarRecorder | None:
    """Roxy 注册流程入口：按配置启动 HAR 采集。任何失败都返回 None，不影响注册主流程。

    Args:
        opened: RoxyOpenResult（需有 debugger_address / ws_endpoint 属性）。
        email: 注册邮箱，用于产物文件名。
        output_dir: 输出目录；None 时用 ROXY_HAR_OUTPUT_DIR 或默认 har_captures/。
    """
    try:
        from config import roxybrowser as _cfg
        if not bool(getattr(_cfg, "ROXY_CAPTURE_HAR", False)):
            return None
        debugger_address = getattr(opened, "debugger_address", None) or None
        ws_endpoint = getattr(opened, "ws_endpoint", None) or None
        if not debugger_address and not ws_endpoint:
            logger.warning("[HarRecorder] ROXY_CAPTURE_HAR=True 但未拿到 CDP 调试地址，本次跳过采集")
            return None
        if output_dir is None:
            output_dir = str(getattr(_cfg, "ROXY_HAR_OUTPUT_DIR", "") or "").strip() or None
        recorder = CdpHarRecorder(
            debugger_address=debugger_address,
            ws_endpoint=ws_endpoint,
            email=email,
            output_dir=output_dir,
            redact=bool(getattr(_cfg, "ROXY_HAR_REDACT", True)),
        )
        if not recorder.start():
            return None
        return recorder
    except Exception as exc:
        logger.warning("[HarRecorder] 启动采集失败，本次不采集：%s: %s", type(exc).__name__, exc)
        return None
