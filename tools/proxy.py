#!/usr/bin/env python3
"""
本地 SOCKS5 代理转发工具
功能：监听 127.0.0.1:7890（无认证 SOCKS5），将所有请求通过带认证的上游 SOCKS5 代理转发。

使用方法：
1. 修改下方配置（主机、端口、用户名、密码）。
2. 运行：python proxy.py
3. 其他程序设置代理为 socks5://127.0.0.1:7890（无需认证）。
"""

import socket
import threading
import struct
import sys
import select

# ===================== 配置区域（请修改为实际值） =====================
UPSTREAM_HOST = "us.lajiaohttp.net"   # 上游代理主机地址
UPSTREAM_PORT = 2000                  # 上游代理端口
USERNAME = "pfu1t25723-region-JP"     # 代理认证用户名
PASSWORD = "cpugckts"                 # 代理认证密码
LOCAL_HOST = "127.0.0.1"              # 本地监听地址
LOCAL_PORT = 7890                     # 本地监听端口
# =====================================================================


def relay(client_sock, upstream_sock):
    """双向转发数据，直到一端关闭。"""
    try:
        while True:
            readable, _, _ = select.select([client_sock, upstream_sock], [], [])
            for src in readable:
                dst = upstream_sock if src is client_sock else client_sock
                data = src.recv(65536)
                if not data:
                    return
                dst.sendall(data)
    except Exception:
        pass
    finally:
        try:
            client_sock.close()
        except Exception:
            pass
        try:
            upstream_sock.close()
        except Exception:
            pass


def socks5_handshake_upstream(target_host, target_port):
    """
    作为 SOCKS5 客户端，连接上游带认证代理，建立到目标地址的隧道。
    返回与上游代理建立好连接后的 socket，失败则返回 None。
    """
    upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    upstream.settimeout(30)

    try:
        upstream.connect((UPSTREAM_HOST, UPSTREAM_PORT))
    except Exception as e:
        print(f"[Upstream Connect Error] Cannot connect to {UPSTREAM_HOST}:{UPSTREAM_PORT}: {e}")
        upstream.close()
        return None

    try:
        # Step 1: 发送认证方法选择（只提供用户名密码认证 0x02）
        upstream.sendall(struct.pack("!BBB", 0x05, 0x01, 0x02))

        # Step 2: 接收服务器选择的认证方法
        resp = upstream.recv(2)
        if len(resp) < 2:
            raise RuntimeError("Upstream closed during auth method negotiation")
        ver, method = struct.unpack("!BB", resp)
        if ver != 0x05:
            raise RuntimeError(f"Invalid SOCKS version: {ver}")
        if method == 0xFF:
            raise RuntimeError("No acceptable authentication methods")

        # Step 3: 用户名密码认证
        if method == 0x02:
            u_bytes = USERNAME.encode("utf-8")
            p_bytes = PASSWORD.encode("utf-8")
            auth_pkt = struct.pack("!B", 0x01) + struct.pack("!B", len(u_bytes)) + u_bytes + struct.pack("!B", len(p_bytes)) + p_bytes
            upstream.sendall(auth_pkt)

            auth_resp = upstream.recv(2)
            if len(auth_resp) < 2:
                raise RuntimeError("Upstream closed during authentication")
            auth_ver, status = struct.unpack("!BB", auth_resp)
            if status != 0x00:
                raise RuntimeError(f"SOCKS5 authentication failed, status={status}")
        elif method != 0x00:
            raise RuntimeError(f"Unsupported authentication method: {method}")

        # Step 4: 发送 CONNECT 请求
        # 先尝试将目标解析为 IP，失败则用域名
        try:
            addr = socket.inet_aton(target_host)
            atyp = 0x01  # IPv4
            addr_bytes = addr
        except OSError:
            try:
                addr = socket.inet_pton(socket.AF_INET6, target_host)
                atyp = 0x04  # IPv6
                addr_bytes = addr
            except OSError:
                atyp = 0x03  # 域名
                host_bytes = target_host.encode("utf-8")
                addr_bytes = struct.pack("!B", len(host_bytes)) + host_bytes

        req = struct.pack("!BBBB", 0x05, 0x01, 0x00, atyp) + addr_bytes + struct.pack("!H", target_port)
        upstream.sendall(req)

        # Step 5: 读取 CONNECT 响应
        resp = upstream.recv(4)
        if len(resp) < 4:
            raise RuntimeError("Upstream closed during CONNECT response")
        ver, rep, rsv, atyp_resp = struct.unpack("!BBBB", resp)
        if ver != 0x05:
            raise RuntimeError(f"Invalid SOCKS version in CONNECT response: {ver}")
        if rep != 0x00:
            error_map = {
                0x01: "General SOCKS server failure",
                0x02: "Connection not allowed by ruleset",
                0x03: "Network unreachable",
                0x04: "Host unreachable",
                0x05: "Connection refused",
                0x06: "TTL expired",
                0x07: "Command not supported",
                0x08: "Address type not supported",
            }
            raise RuntimeError(f"SOCKS5 CONNECT failed: {error_map.get(rep, f'Unknown error {rep}')}")

        # 读取绑定的地址（仅丢弃，不关心）
        if atyp_resp == 0x01:
            upstream.recv(4 + 2)
        elif atyp_resp == 0x03:
            len_byte = upstream.recv(1)
            if len_byte:
                upstream.recv(ord(len_byte) + 2)
        elif atyp_resp == 0x04:
            upstream.recv(16 + 2)

        return upstream

    except Exception as e:
        upstream.close()
        print(f"[SOCKS5 Handshake Error] {e}")
        return None


def handle_socks5_client(client_sock, client_addr):
    """处理单个 SOCKS5 客户端连接。"""
    try:
        # Step 1: 读取认证方法选择
        header = client_sock.recv(2)
        if len(header) < 2:
            client_sock.close()
            return
        ver, nmethods = struct.unpack("!BB", header)
        if ver != 0x05:
            client_sock.close()
            return
        methods = client_sock.recv(nmethods)
        if len(methods) < nmethods:
            client_sock.close()
            return

        # Step 2: 回复使用无认证 (0x00)
        client_sock.sendall(struct.pack("!BB", 0x05, 0x00))

        # Step 3: 读取连接请求
        req = client_sock.recv(4)
        if len(req) < 4:
            client_sock.close()
            return
        ver, cmd, rsv, atyp = struct.unpack("!BBBB", req)
        if ver != 0x05:
            client_sock.close()
            return
        if cmd != 0x01:  # 只支持 CONNECT
            client_sock.sendall(struct.pack("!BBBB", 0x05, 0x07, 0x00, 0x01) + b"\x00" * 4 + b"\x00\x00")
            client_sock.close()
            return

        # 读取目标地址
        if atyp == 0x01:  # IPv4
            addr_bytes = client_sock.recv(4)
            if len(addr_bytes) < 4:
                client_sock.close()
                return
            target_host = socket.inet_ntoa(addr_bytes)
        elif atyp == 0x03:  # 域名
            len_byte = client_sock.recv(1)
            if not len_byte:
                client_sock.close()
                return
            domain_len = ord(len_byte)
            domain = client_sock.recv(domain_len)
            if len(domain) < domain_len:
                client_sock.close()
                return
            target_host = domain.decode("utf-8")
        elif atyp == 0x04:  # IPv6
            addr_bytes = client_sock.recv(16)
            if len(addr_bytes) < 16:
                client_sock.close()
                return
            target_host = socket.inet_ntop(socket.AF_INET6, addr_bytes)
        else:
            client_sock.sendall(struct.pack("!BBBB", 0x05, 0x08, 0x00, 0x01) + b"\x00" * 4 + b"\x00\x00")
            client_sock.close()
            return

        # 读取目标端口
        port_bytes = client_sock.recv(2)
        if len(port_bytes) < 2:
            client_sock.close()
            return
        target_port = struct.unpack("!H", port_bytes)[0]

        print(f"[Client] {client_addr} -> CONNECT {target_host}:{target_port}")

        # Step 4: 通过上游 SOCKS5 代理建立连接
        upstream = socks5_handshake_upstream(target_host, target_port)
        if upstream is None:
            client_sock.sendall(struct.pack("!BBBB", 0x05, 0x01, 0x00, 0x01) + b"\x00" * 4 + b"\x00\x00")
            client_sock.close()
            return

        # Step 5: 回复客户端连接成功
        client_sock.sendall(struct.pack("!BBBB", 0x05, 0x00, 0x00, 0x01) + b"\x00" * 4 + b"\x00\x00")
        print(f"[Tunnel] Established {target_host}:{target_port} via upstream")

        # Step 6: 双向转发
        relay(client_sock, upstream)

    except Exception as e:
        print(f"[Client Handler Error] {e}")
    finally:
        try:
            client_sock.close()
        except Exception:
            pass


def test_upstream():
    """简单测试上游代理 TCP 连通性。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect((UPSTREAM_HOST, UPSTREAM_PORT))
        s.close()
        return True
    except Exception as e:
        print(f"[Error] Cannot connect to upstream proxy {UPSTREAM_HOST}:{UPSTREAM_PORT}: {e}")
        return False


def main():
    print(f"Checking upstream proxy {UPSTREAM_HOST}:{UPSTREAM_PORT} ...")
    if not test_upstream():
        print("Upstream proxy unreachable, exiting.")
        sys.exit(1)
    print("Upstream proxy is reachable.\n")

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((LOCAL_HOST, LOCAL_PORT))
    server.listen(100)
    print(f"Local SOCKS5 proxy listening on {LOCAL_HOST}:{LOCAL_PORT}")
    print(f"Forwarding to {UPSTREAM_HOST}:{UPSTREAM_PORT} (user: {USERNAME})")
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            client_sock, addr = server.accept()
            t = threading.Thread(target=handle_socks5_client, args=(client_sock, addr[0]))
            t.daemon = True
            t.start()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.close()


if __name__ == "__main__":
    main()
