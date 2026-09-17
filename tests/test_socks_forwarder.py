# -*- coding: utf-8 -*-
"""本地 SOCKS5 转发层测试：模拟带认证上游，验证认证补全与端口复用。"""
import socket
import socketserver
import struct
import unittest

from core import socks_forwarder
from core.socks_forwarder import forwarded_proxy_url, parse_socks_url, stop_all


class _FakeUpstreamHandler(socketserver.BaseRequestHandler):
    """最小带认证 SOCKS5 上游：固定账号密码，CONNECT 成功后进入回显模式。"""

    def handle(self):
        r = self.request
        r.settimeout(5)

        def recv_exact(n):
            buf = b""
            while len(buf) < n:
                chunk = r.recv(n - len(buf))
                if not chunk:
                    raise ConnectionError("上游收到不完整请求")
                buf += chunk
            return buf

        try:
            head = recv_exact(2)
            recv_exact(head[1])  # 方法列表
            r.sendall(b"\x05\x02")  # 要求用户名/密码认证
            recv_exact(1)  # subnegotiation ver
            ulen = recv_exact(1)[0]
            user = recv_exact(ulen)
            plen = recv_exact(1)[0]
            password = recv_exact(plen)
            if user != self.server.expected_user or password != self.server.expected_password:
                r.sendall(b"\x01\x01")
                return
            r.sendall(b"\x01\x00")
            req = recv_exact(4)
            atyp = req[3]
            if atyp == 0x01:
                recv_exact(4)
            elif atyp == 0x03:
                recv_exact(recv_exact(1)[0])
            elif atyp == 0x04:
                recv_exact(16)
            recv_exact(2)  # port
            r.sendall(b"\x05\x00\x00\x01" + b"\x00\x00\x00\x00\x00\x00")
            while True:
                data = r.recv(4096)
                if not data:
                    break
                r.sendall(data)
        except (ConnectionError, OSError):
            pass


class _FakeUpstreamServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, expected_user: bytes, expected_password: bytes):
        super().__init__(("127.0.0.1", 0), _FakeUpstreamHandler)
        self.expected_user = expected_user
        self.expected_password = expected_password


class SocksForwarderTests(unittest.TestCase):
    def setUp(self):
        self._servers = []
        self.addCleanup(stop_all)
        self.addCleanup(self._stop_servers)

    def _start_upstream(self, user: bytes = b"user1", password: bytes = b"pass1") -> _FakeUpstreamServer:
        import threading

        server = _FakeUpstreamServer(user, password)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        self._servers.append(server)
        return server

    def _stop_servers(self):
        for server in self._servers:
            server.shutdown()
            server.server_close()

    @staticmethod
    def _socks5_roundtrip(local_port: int) -> tuple[bytes, bytes, bytes]:
        """经本地入口完成 CONNECT 并回显一段数据，返回 (握手回复, CONNECT 回复, 回显内容)。"""
        with socket.create_connection(("127.0.0.1", local_port), timeout=5) as s:
            s.settimeout(5)
            s.sendall(b"\x05\x01\x00")
            greet = s.recv(2)
            host = b"example.com"
            s.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + struct.pack("!H", 80))
            # 回显模式会把 CONNECT 请求原样发回，读到包含完整请求的回包为止。
            # 成功回包 = 05 00 00 + ATYP + 域名长度 + 域名 + 端口。
            reply = b""
            while len(reply) < 3 + 1 + 1 + len(host) + 2:
                chunk = s.recv(1024)
                if not chunk:
                    break
                reply += chunk
            # 严格校验成功回包结构：05 00 00 | ATYP=03 | LEN | 域名 | 端口。
            expect_tail = b"\x03" + bytes([len(host)]) + host + struct.pack("!H", 80)
            assert reply[3:] == expect_tail, f"成功回包地址段错误: {reply!r}"
            # 回显上游已消费完整请求，此后只应有新数据的回显。
            s.sendall(b"ping-echo")
            buf = b""
            while buf != b"ping-echo":
                chunk = s.recv(1024)
                if not chunk:
                    break
                buf += chunk
            return greet, reply, buf

    def test_parse_socks_url(self):
        self.assertEqual(
            parse_socks_url("socks5h://user:p%40ss@1.2.3.4:1080"),
            socks_forwarder.SocksUpstream(host="1.2.3.4", port=1080, username="user", password="p@ss"),
        )
        # 无凭据 / 非 socks 协议 → 不需要转发
        self.assertIsNone(parse_socks_url("socks5://1.2.3.4:1080"))
        self.assertIsNone(parse_socks_url("http://user:pass@1.2.3.4:8080"))
        self.assertIsNone(parse_socks_url(""))

    def test_forwarder_completes_auth_and_relays(self):
        upstream = self._start_upstream(b"user1", b"pass1")
        local = forwarded_proxy_url(f"socks5h://user1:pass1@127.0.0.1:{upstream.server_address[1]}", base_port=0)
        self.assertTrue(local.startswith("socks5://127.0.0.1:"))
        port = int(local.rsplit(":", 1)[1])
        greet, reply, echoed = self._socks5_roundtrip(port)
        self.assertEqual(greet, b"\x05\x00")  # 对客户端始终无认证
        self.assertEqual(reply[:3], b"\x05\x00\x00")  # CONNECT 成功
        self.assertEqual(echoed, b"ping-echo")  # 上游已消费完整请求，回显无残留

    def test_same_upstream_reuses_same_local_port(self):
        upstream = self._start_upstream()
        url = f"socks5://user1:pass1@127.0.0.1:{upstream.server_address[1]}"
        self.assertEqual(forwarded_proxy_url(url, base_port=0), forwarded_proxy_url(url, base_port=0))

    def test_wrong_password_returns_failure_reply(self):
        upstream = self._start_upstream(b"user1", b"pass1")
        local = forwarded_proxy_url(f"socks5://user1:WRONG@127.0.0.1:{upstream.server_address[1]}", base_port=0)
        port = int(local.rsplit(":", 1)[1])
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.settimeout(5)
            s.sendall(b"\x05\x01\x00")
            self.assertEqual(s.recv(2), b"\x05\x00")
            host = b"example.com"
            s.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + struct.pack("!H", 80))
            reply = s.recv(1024)
        self.assertEqual(reply[:2], b"\x05\x01")  # general failure（上游认证被拒）

    def test_unreachable_upstream_returns_failure_reply(self):
        # 端口 1 保留端口，本机几乎必然拒绝连接，模拟上游不可达。
        local = forwarded_proxy_url("socks5://user1:pass1@127.0.0.1:1", base_port=0)
        port = int(local.rsplit(":", 1)[1])
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.settimeout(10)
            s.sendall(b"\x05\x01\x00")
            self.assertEqual(s.recv(2), b"\x05\x00")
            host = b"example.com"
            s.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + struct.pack("!H", 80))
            reply = s.recv(1024)
        self.assertEqual(reply[:2], b"\x05\x04")  # host unreachable
