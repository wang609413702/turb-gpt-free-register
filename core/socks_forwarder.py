# -*- coding: utf-8 -*-
"""本地 SOCKS5 转发层：替 Chromium 完成 SOCKS5 用户名/密码认证。

Chromium 的 --proxy-server 不接受 socks:// URL 内嵌账号密码（整个代理条目被
判无效，页面报 net::ERR_NO_SUPPORTED_PROXIES），且内核本身不做 SOCKS5 认证
握手（去掉凭据后报 net::ERR_SOCKS_CONNECTION_FAILED），因此带认证的上游
代理池（如 711proxy）无法直接传给 CloakBrowser。

这里为每个上游代理在本机 127.0.0.1 起一个无认证 SOCKS5 入口：浏览器连本地
入口，转发层与上游完成认证后原样中继字节。入口按上游（host/port/账号/密码）
缓存复用，同一上游始终对应同一端口；转发线程为 daemon，进程退出即回收。
"""
from __future__ import annotations

import asyncio
import logging
import socket
import threading
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)

# 上游 TCP 连接与 SOCKS5 握手超时（秒）。
_UPSTREAM_CONNECT_TIMEOUT = 15.0
_UPSTREAM_HANDSHAKE_TIMEOUT = 15.0

# 上游瞬时抖动重试：连接/握手失败（EOF、超时等）自动换次重试；
# 认证被拒、CONNECT 被明确拒绝属于确定性失败，不重试。
_UPSTREAM_ATTEMPTS = 3
_UPSTREAM_RETRY_DELAY = 0.5

# 本地端口搜索范围：从起始端口起最多尝试的端口数。
_PORT_SCAN_LIMIT = 500

_SOCKS_VERSION = 0x05


@dataclass(frozen=True)
class SocksUpstream:
    """带认证的 SOCKS5 上游。"""
    host: str
    port: int
    username: str
    password: str


def parse_socks_url(url: str) -> SocksUpstream | None:
    """解析 socks5(h)://user:pass@host:port。

    非 socks5 协议、缺 host/port 或没有凭据时返回 None——无凭据的代理
    Chromium 可直接使用，无需转发。
    """
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("socks5", "socks5h"):
        return None
    if not parsed.hostname or not parsed.port:
        return None
    if not parsed.username:
        return None
    return SocksUpstream(
        host=parsed.hostname,
        port=parsed.port,
        username=unquote(parsed.username),
        password=unquote(parsed.password or ""),
    )


def _default_base_port() -> int:
    try:
        from config import cloakbrowser as _cfg
        return int(getattr(_cfg, "SOCKS_FORWARD_BASE_PORT", 18100) or 18100)
    except Exception:
        return 18100


class _Forwarder:
    """单个上游对应的本地无认证 SOCKS5 入口（独立事件循环线程）。"""

    def __init__(self, upstream: SocksUpstream, base_port: int):
        self._upstream = upstream
        self._base_port = int(base_port)
        self._loop = asyncio.new_event_loop()
        self._server: asyncio.AbstractServer | None = None
        self._ready = threading.Event()
        self._error: Exception | None = None
        self.port = 0
        self._thread = threading.Thread(
            target=self._run, name=f"socks-forwarder-{upstream.host}:{upstream.port}", daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("SOCKS 转发线程启动超时")
        if self._error is not None:
            raise self._error

    # ---- 生命周期 ----

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self.port = self._loop.run_until_complete(self._bind())
        except Exception as exc:
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        self._loop.run_forever()

    async def _bind(self) -> int:
        base = self._base_port
        last_exc: OSError | None = None
        for offset in range(_PORT_SCAN_LIMIT):
            try:
                self._server = await asyncio.start_server(self._handle, "127.0.0.1", base + offset)
            except OSError as exc:
                last_exc = exc
                continue
            actual = self._server.sockets[0].getsockname()[1]
            logger.info(
                "[SocksForward] 本地无认证入口 127.0.0.1:%s -> 上游 %s:%s（认证由转发层完成）",
                actual, self._upstream.host, self._upstream.port,
            )
            return actual
        raise RuntimeError(f"本地端口 {base}..{base + _PORT_SCAN_LIMIT - 1} 均无法绑定: {last_exc}")

    def stop(self) -> None:
        async def _shutdown() -> None:
            if self._server is not None:
                self._server.close()
                await self._server.wait_closed()

        try:
            asyncio.run_coroutine_threadsafe(_shutdown(), self._loop).result(timeout=5)
        except Exception:
            pass
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass
        if self._thread.is_alive():
            self._thread.join(timeout=5)
        try:
            self._loop.close()
        except Exception:
            pass

    # ---- SOCKS5 协议 ----

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            # 客户端方法协商：无认证入口，固定回复 0x00（no-auth）。
            head = await reader.readexactly(2)
            if head[0] != _SOCKS_VERSION:
                raise _BadClient(f"不支持的 SOCKS 版本: {head[0]}")
            await reader.readexactly(head[1])
            writer.write(b"\x05\x00")
            await writer.drain()

            # 客户端请求：仅支持 CONNECT（浏览器只发 CONNECT）。
            req = await reader.readexactly(4)
            if req[0] != _SOCKS_VERSION:
                raise _BadClient(f"请求版本异常: {req[0]}")
            if req[1] != 0x01:
                await self._reply_failure(writer, 0x07)
                return
            atyp = req[3]
            addr_bytes, port_bytes = await self._read_target(reader, atyp)

            # 连接上游并代为认证；瞬时抖动自动重试，失败按 SOCKS5 规范回错误码。
            streams, rep, reason = await self._establish(atyp, addr_bytes, port_bytes)
            if streams is None:
                logger.info(
                    "[SocksForward] 上游不可用 %s:%s：%s",
                    self._upstream.host, self._upstream.port, reason,
                )
                await self._reply_failure(writer, rep)
                return
            up_reader, up_writer = streams

            # 认证通过：告知客户端成功，之后进入纯字节中继。
            writer.write(b"\x05\x00\x00" + _encode_target(atyp, addr_bytes, port_bytes))
            await writer.drain()
            await asyncio.wait(
                {
                    asyncio.create_task(self._pump(reader, up_writer)),
                    asyncio.create_task(self._pump(up_reader, writer)),
                },
                return_when=asyncio.FIRST_COMPLETED,
            )
        except (asyncio.IncompleteReadError, ConnectionError, _BadClient) as exc:
            logger.debug("[SocksForward] 客户端连接结束/异常: %s", exc)
        except Exception as exc:
            logger.warning("[SocksForward] 转发连接异常: %s: %s", type(exc).__name__, exc)
        finally:
            _close_stream(writer)

    async def _establish(
        self,
        atyp: int,
        addr_bytes: bytes,
        port_bytes: bytes,
    ) -> tuple[tuple[asyncio.StreamReader, asyncio.StreamWriter] | None, int, str]:
        """建立上游连接并完成认证。

        返回 (流, 失败回码, 原因)：成功时回码为 None；连接/握手类瞬时失败
        自动重试，认证或 CONNECT 被明确拒绝时直接失败。
        """
        last_reason = ""
        for attempt in range(1, _UPSTREAM_ATTEMPTS + 1):
            try:
                up_reader, up_writer = await asyncio.wait_for(
                    asyncio.open_connection(self._upstream.host, self._upstream.port),
                    _UPSTREAM_CONNECT_TIMEOUT,
                )
            except Exception as exc:
                last_reason = f"连接失败 {type(exc).__name__}: {exc}"
                if attempt < _UPSTREAM_ATTEMPTS:
                    await asyncio.sleep(_UPSTREAM_RETRY_DELAY * attempt)
                continue
            try:
                ok = await asyncio.wait_for(
                    self._upstream_handshake(up_reader, up_writer, atyp, addr_bytes, port_bytes),
                    _UPSTREAM_HANDSHAKE_TIMEOUT,
                )
            except (asyncio.IncompleteReadError, ConnectionError, OSError, TimeoutError) as exc:
                _close_stream(up_writer)
                last_reason = f"握手中断 {type(exc).__name__}: {exc}"
                if attempt < _UPSTREAM_ATTEMPTS:
                    await asyncio.sleep(_UPSTREAM_RETRY_DELAY * attempt)
                continue
            except Exception as exc:
                _close_stream(up_writer)
                return None, 0x01, f"握手异常 {type(exc).__name__}: {exc}"
            if ok:
                return (up_reader, up_writer), None, ""
            _close_stream(up_writer)
            return None, 0x01, "上游明确拒绝（认证或 CONNECT）"
        return None, 0x04, f"重试 {_UPSTREAM_ATTEMPTS} 次仍不可用，最后原因：{last_reason}"

    async def _upstream_handshake(
        self,
        up_reader: asyncio.StreamReader,
        up_writer: asyncio.StreamWriter,
        atyp: int,
        addr_bytes: bytes,
        port_bytes: bytes,
    ) -> bool:
        # 问候：只提供用户名/密码认证（上游无认证时也会选 0x00）。
        up_writer.write(b"\x05\x01\x02")
        await up_writer.drain()
        resp = await up_reader.readexactly(2)
        if resp[0] != _SOCKS_VERSION or resp[1] not in (0x00, 0x02):
            logger.info("[SocksForward] 上游方法协商失败 %s:%s", self._upstream.host, self._upstream.port)
            return False
        if resp[1] == 0x02:
            user = self._upstream.username.encode("utf-8")
            password = self._upstream.password.encode("utf-8")
            up_writer.write(b"\x01" + bytes([len(user)]) + user + bytes([len(password)]) + password)
            await up_writer.drain()
            auth = await up_reader.readexactly(2)
            if auth[1] != 0x00:
                logger.warning(
                    "[SocksForward] 上游认证被拒 %s:%s（检查账号/套餐/余额）",
                    self._upstream.host, self._upstream.port,
                )
                return False
        # CONNECT：原样携带客户端请求的目标地址。
        up_writer.write(b"\x05\x01\x00" + _encode_target(atyp, addr_bytes, port_bytes))
        await up_writer.drain()
        reply = await up_reader.readexactly(4)
        if reply[1] != 0x00:
            logger.info("[SocksForward] 上游拒绝 CONNECT：rep=0x%02x", reply[1])
            return False
        await self._skip_bound_addr(up_reader, reply[3])
        return True

    @staticmethod
    async def _read_target(reader: asyncio.StreamReader, atyp: int) -> tuple[bytes, bytes]:
        if atyp == 0x01:
            addr = await reader.readexactly(4)
        elif atyp == 0x03:
            length = (await reader.readexactly(1))[0]
            addr = await reader.readexactly(length)
        elif atyp == 0x04:
            addr = await reader.readexactly(16)
        else:
            raise _BadClient(f"未知地址类型: {atyp}")
        return addr, await reader.readexactly(2)

    @staticmethod
    async def _skip_bound_addr(reader: asyncio.StreamReader, atyp: int) -> None:
        if atyp == 0x01:
            await reader.readexactly(4)
        elif atyp == 0x03:
            length = (await reader.readexactly(1))[0]
            await reader.readexactly(length)
        elif atyp == 0x04:
            await reader.readexactly(16)
        await reader.readexactly(2)

    @staticmethod
    async def _reply_failure(writer: asyncio.StreamWriter, rep: int) -> None:
        try:
            # 统一用 IPv4 0.0.0.0:0 作为绑定地址回错误码，客户端只看 rep。
            writer.write(b"\x05" + bytes([rep]) + b"\x00\x01" + b"\x00\x00\x00\x00\x00\x00")
            await writer.drain()
        except Exception:
            pass

    @staticmethod
    async def _pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await src.read(65536)
                if not data:
                    break
                dst.write(data)
                await dst.drain()
        except Exception:
            pass
        finally:
            _close_stream(dst)


class _BadClient(Exception):
    """客户端请求不符合 SOCKS5 规范。"""


def _encode_target(atyp: int, addr_bytes: bytes, port_bytes: bytes) -> bytes:
    """编码完整 SOCKS5 地址段：ATYP + 地址 + 端口；域名（ATYP=0x03）带 1 字节长度。"""
    if atyp == 0x03:
        return bytes([atyp, len(addr_bytes)]) + addr_bytes + port_bytes
    return bytes([atyp]) + addr_bytes + port_bytes


def _close_stream(writer: asyncio.StreamWriter | None) -> None:
    if writer is None:
        return
    try:
        writer.close()
    except Exception:
        pass


# upstream -> 本地入口 缓存；同进程内同一上游永远复用同一端口。
_forwarders: dict[SocksUpstream, _Forwarder] = {}
_lock = threading.Lock()


def forwarded_proxy_url(upstream_url: str, base_port: int | None = None) -> str:
    """返回指向本地无认证入口的 socks5:// URL；无需转发或启动失败时返回空串。

    base_port=None 时读 config.cloakbrowser.SOCKS_FORWARD_BASE_PORT；
    显式传 0 由操作系统分配临时端口（测试用）。
    """
    upstream = parse_socks_url(upstream_url)
    if upstream is None:
        return ""
    with _lock:
        fwd = _forwarders.get(upstream)
        if fwd is None:
            if base_port is None:
                base_port = _default_base_port()
            fwd = _Forwarder(upstream, base_port)
            _forwarders[upstream] = fwd
    return f"socks5://127.0.0.1:{fwd.port}"


def stop_all() -> None:
    """停止并清空全部本地转发入口（测试与优雅退出用）。"""
    with _lock:
        forwarders = list(_forwarders.values())
        _forwarders.clear()
    for fwd in forwarders:
        fwd.stop()
