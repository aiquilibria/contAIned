"""
contAIned egress proxy — minimal filtering HTTP/HTTPS proxy.

Runs inside the contAIned Docker image as a sidecar container alongside the
agent.  All outbound HTTP and HTTPS traffic from the agent container is routed
through this proxy via the HTTP_PROXY / HTTPS_PROXY environment variables
injected by DockerRunner.

Allowed domains are passed as positional CLI arguments:

    python3 -m contained.proxy api.anthropic.com pypi.org

A request is allowed when the target host equals an allowed domain or is a
subdomain of it (e.g. ``foo.api.anthropic.com`` is allowed when
``api.anthropic.com`` is in the list).  Everything else gets a 403 Forbidden.

HTTPS connections use the HTTP CONNECT tunnelling method: the proxy checks the
target host, then relays the raw TLS stream without inspecting it.  Plain HTTP
requests are forwarded after the same host check.

Limitation
----------
This proxy enforces filtering only for processes that honour HTTP_PROXY /
HTTPS_PROXY environment variables.  Code that dials outbound sockets directly
(ignoring those variables) can bypass it.  Full enforcement requires iptables
redirect rules on the Docker bridge — see docs/egress-and-exfiltration-protection.md.
"""

from __future__ import annotations

import socket
import sys
import threading
import urllib.parse


def _is_allowed(host: str, allowed: list[str]) -> bool:
    """Return True if host matches an allowed domain or is a subdomain of one."""
    host = host.lower()
    for domain in allowed:
        domain = domain.lower()
        if host == domain or host.endswith("." + domain):
            return True
    return False


def _relay(src: socket.socket, dst: socket.socket) -> None:
    """Forward bytes from src → dst until the connection closes."""
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _read_request_head(conn: socket.socket) -> bytes:
    """Read bytes from conn until the HTTP header terminator \\r\\n\\r\\n."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = conn.recv(4096)
        if not chunk:
            break
        buf += chunk
        if len(buf) > 65536:
            break  # guard against oversized / malformed headers
    return buf


def _handle(conn: socket.socket, allowed: list[str]) -> None:
    """Handle a single proxy client connection."""
    try:
        raw = _read_request_head(conn)
        if not raw:
            return

        sep = raw.find(b"\r\n\r\n")
        head_bytes = raw[:sep] if sep != -1 else raw
        body_start = raw[sep + 4 :] if sep != -1 else b""

        head = head_bytes.decode("latin-1", errors="replace")
        first_line = head.split("\r\n", 1)[0]
        parts = first_line.split()
        if len(parts) < 2:
            return

        method, target = parts[0], parts[1]

        if method == "CONNECT":
            # ── HTTPS tunnel ──────────────────────────────────────────────────
            # target is "host:port"
            host, _, port_str = target.rpartition(":")
            port = int(port_str) if port_str.isdigit() else 443

            if not _is_allowed(host, allowed):
                conn.sendall(
                    b"HTTP/1.1 403 Forbidden\r\n"
                    b"Content-Length: 0\r\n"
                    b"X-Proxy-Reason: domain not in egress allowlist\r\n\r\n"
                )
                return

            try:
                remote = socket.create_connection((host, port), timeout=30)
            except OSError:
                conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                return

            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")

            # Bidirectional relay — run one direction in a background thread.
            t = threading.Thread(target=_relay, args=(remote, conn), daemon=True)
            t.start()
            _relay(conn, remote)
            t.join(timeout=120)

        else:
            # ── Plain HTTP ────────────────────────────────────────────────────
            parsed = urllib.parse.urlparse(target)
            host = parsed.hostname or ""
            port = parsed.port or 80

            if not _is_allowed(host, allowed):
                conn.sendall(
                    b"HTTP/1.1 403 Forbidden\r\n"
                    b"Content-Length: 0\r\n"
                    b"X-Proxy-Reason: domain not in egress allowlist\r\n\r\n"
                )
                return

            try:
                remote = socket.create_connection((host, port), timeout=30)
            except OSError:
                conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                return

            remote.sendall(raw)
            if body_start:
                remote.sendall(body_start)
            _relay(remote, conn)
            remote.close()

    except Exception:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main(allowed_domains: list[str]) -> None:
    """Start the proxy server and block forever."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", 3128))
    server.listen(128)
    while True:
        conn, _ = server.accept()
        threading.Thread(
            target=_handle, args=(conn, allowed_domains), daemon=True
        ).start()


if __name__ == "__main__":
    allowed = sys.argv[1:]
    if not allowed:
        print(
            "usage: python3 -m contained.proxy <domain> [<domain> ...]",
            file=sys.stderr,
        )
        sys.exit(1)
    main(allowed)
