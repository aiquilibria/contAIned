"""
Unit tests for contained.proxy — pure-logic and socket-pair tests.

Excluded: main() (binds port 3128, requires root/privileged port).
"""

import socket
import threading

from contained.proxy import _handle, _is_allowed, _read_request_head

# ── _is_allowed ───────────────────────────────────────────────────────────────


class TestIsAllowed:
    def test_exact_match(self):
        assert _is_allowed("api.anthropic.com", ["api.anthropic.com"]) is True

    def test_subdomain_allowed(self):
        assert _is_allowed("foo.api.anthropic.com", ["api.anthropic.com"]) is True

    def test_deep_subdomain_allowed(self):
        assert _is_allowed("a.b.pypi.org", ["pypi.org"]) is True

    def test_different_domain_blocked(self):
        assert _is_allowed("evil.com", ["api.anthropic.com"]) is False

    def test_empty_allowed_list(self):
        assert _is_allowed("api.anthropic.com", []) is False

    def test_partial_suffix_not_matched(self):
        # "notpypi.org" must not match "pypi.org"
        assert _is_allowed("notpypi.org", ["pypi.org"]) is False

    def test_case_insensitive_host(self):
        assert _is_allowed("API.Anthropic.COM", ["api.anthropic.com"]) is True

    def test_case_insensitive_domain(self):
        assert _is_allowed("api.anthropic.com", ["API.Anthropic.COM"]) is True

    def test_multiple_allowed_domains_first_matches(self):
        assert _is_allowed("pypi.org", ["github.com", "pypi.org"]) is True

    def test_multiple_allowed_domains_none_matches(self):
        assert _is_allowed("evil.com", ["github.com", "pypi.org"]) is False

    def test_empty_host(self):
        assert _is_allowed("", ["api.anthropic.com"]) is False


# ── _read_request_head ────────────────────────────────────────────────────────


class TestReadRequestHead:
    """Uses socket.socketpair() for a real in-process pipe — no network."""

    def _pair(self):
        """Return (client, server) connected socket pair."""
        return socket.socketpair()

    def test_reads_until_header_terminator(self):
        client, server = self._pair()
        try:
            data = b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n"
            client.sendall(data)
            client.close()
            result = _read_request_head(server)
            assert result == data
        finally:
            server.close()

    def test_returns_partial_when_connection_closes(self):
        client, server = self._pair()
        try:
            client.sendall(b"GET / HTTP/1.1\r\nHost: example")
            client.close()  # close without sending \r\n\r\n
            result = _read_request_head(server)
            assert b"GET / HTTP/1.1" in result
        finally:
            server.close()

    def test_returns_empty_bytes_on_immediate_close(self):
        client, server = self._pair()
        try:
            client.close()
            result = _read_request_head(server)
            assert result == b""
        finally:
            server.close()

    def test_stops_at_first_header_terminator(self):
        """Data after \\r\\n\\r\\n is included in buffer but terminates the loop."""
        client, server = self._pair()
        try:
            # Send header + body bytes in one write
            client.sendall(b"POST / HTTP/1.1\r\n\r\nBODY")
            client.close()
            result = _read_request_head(server)
            assert b"\r\n\r\n" in result
        finally:
            server.close()

    def test_header_size_guard(self):
        """Headers larger than 65536 bytes are cut off."""
        client, server = self._pair()

        def send_large():
            # Send > 65536 bytes without a \r\n\r\n terminator
            try:
                client.sendall(b"X" * 70000)
            except OSError:
                pass
            finally:
                client.close()

        t = threading.Thread(target=send_large, daemon=True)
        t.start()
        result = _read_request_head(server)
        t.join(timeout=5)
        server.close()
        # Guard triggers after appending a recv(4096) chunk, so max is 65536 + 4096
        assert len(result) <= 65536 + 4096


# ── _handle — CONNECT (HTTPS tunnel) ─────────────────────────────────────────


class TestHandleConnect:
    """Tests for the CONNECT method path inside _handle."""

    def _make_client(self) -> tuple[socket.socket, socket.socket]:
        """Return (client_end, server_end) socket pair."""
        return socket.socketpair()

    def _send_connect(self, sock: socket.socket, target: str) -> None:
        sock.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())

    def test_connect_blocked_domain_returns_403(self):
        client, server = self._make_client()
        try:
            self._send_connect(client, "evil.com:443")
            client.shutdown(socket.SHUT_WR)
            _handle(server, ["api.anthropic.com"])
            response = b""
            client.settimeout(2)
            try:
                while True:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    response += chunk
            except OSError:
                pass
            assert b"403" in response
            assert b"domain not in egress allowlist" in response
        finally:
            client.close()

    def test_connect_malformed_request_closes_cleanly(self):
        client, server = self._make_client()
        try:
            # Send garbage with no proper request line
            client.sendall(b"NOTHTTP\r\n\r\n")
            client.close()
            _handle(server, ["api.anthropic.com"])  # must not raise
        finally:
            pass

    def test_connect_empty_request_closes_cleanly(self):
        client, server = self._make_client()
        try:
            client.close()
            _handle(server, ["api.anthropic.com"])  # must not raise
        finally:
            pass


# ── _handle — plain HTTP ──────────────────────────────────────────────────────


class TestHandlePlainHttp:
    """Tests for the plain HTTP path inside _handle."""

    def _make_client(self) -> tuple[socket.socket, socket.socket]:
        return socket.socketpair()

    def test_http_blocked_domain_returns_403(self):
        client, server = self._make_client()
        try:
            request = b"GET http://evil.com/path HTTP/1.1\r\nHost: evil.com\r\n\r\n"
            client.sendall(request)
            client.shutdown(socket.SHUT_WR)
            _handle(server, ["api.anthropic.com"])
            response = b""
            client.settimeout(2)
            try:
                while True:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    response += chunk
            except OSError:
                pass
            assert b"403" in response
            assert b"domain not in egress allowlist" in response
        finally:
            client.close()

    def test_http_request_too_few_parts_closes_cleanly(self):
        client, server = self._make_client()
        try:
            # First line has only one token — not a valid HTTP request
            client.sendall(b"BADREQUEST\r\n\r\n")
            client.close()
            _handle(server, ["api.anthropic.com"])  # must not raise
        finally:
            pass
