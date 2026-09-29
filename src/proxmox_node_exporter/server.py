"""Minimal, hardened HTTP(S) server for ``/metrics``.

* optional TLS (with certificate hot-reload and optional client certificates)
  and basic auth;
* the TLS handshake happens in the worker thread, under a timeout, so a slow
  client cannot stall ``accept()``;
* a hard cap on concurrent connections (overall and per client address), a
  deadline for every request (enforced by a watchdog, so a client trickling
  bytes cannot hold a connection open) and the request size limits of
  :mod:`http.server` bound resource usage;
* no version banner, ``nosniff`` and ``no-store`` headers on every response.
"""

from __future__ import annotations

import contextlib
import gzip
import itertools
import logging
import socket
import socketserver
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol
from urllib.parse import urlsplit

from .metrics import CONTENT_TYPE
from .webconfig import BasicAuth, TLSContextProvider, WebConfig

log = logging.getLogger(__name__)

_LANDING = b"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Proxmox node exporter</title></head>
<body><h1>Proxmox node exporter</h1><p><a href="/metrics">Metrics</a></p></body></html>
"""


class App(Protocol):
    def render_metrics(self) -> bytes: ...

    def healthy(self) -> bool: ...


def parse_listen_address(value: str, *, allow_ephemeral: bool = False) -> tuple[str, int]:
    """Parse ``host:port``, ``[v6addr]:port`` or ``:port``.

    Port 0 (pick a free port) is only accepted with ``allow_ephemeral``.
    """
    value = value.strip()
    if value.startswith("["):
        host, sep, port = value[1:].partition("]:")
    else:
        host, sep, port = value.rpartition(":")
        if ":" in host:
            raise ValueError(f"IPv6 addresses must be bracketed: {value!r}")
    if not sep:
        raise ValueError(f"listen address must be host:port, got {value!r}")
    try:
        port_number = int(port)
    except ValueError:
        raise ValueError(f"invalid port in {value!r}") from None
    if not (0 if allow_ephemeral else 1) <= port_number < 65536:
        raise ValueError(f"port out of range in {value!r}")
    return host, port_number


class _Deadlines:
    """Shuts down connections whose current request outlives its deadline.

    Socket timeouts apply to each recv() separately, so on their own they do
    not stop a client that sends one byte every few seconds (slowloris).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[int, tuple[float, socket.socket]] = {}
        self._keys = itertools.count()
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._run, name="http-deadlines", daemon=True)
        self._thread.start()

    def arm(self, sock: socket.socket, seconds: float) -> int:
        key = next(self._keys)
        with self._lock:
            self._items[key] = (time.monotonic() + seconds, sock)
        return key

    def disarm(self, key: int) -> None:
        with self._lock:
            self._items.pop(key, None)

    def close(self) -> None:
        self._closed.set()
        self._thread.join(2)

    def _run(self) -> None:
        while not self._closed.wait(0.25):
            now = time.monotonic()
            with self._lock:
                expired = [k for k, (deadline, _) in self._items.items() if deadline <= now]
                socks = [self._items.pop(k)[1] for k in expired]
            for sock in socks:
                log.debug("request deadline exceeded; closing connection")
                with contextlib.suppress(OSError):
                    # socket.socket.shutdown also works on SSLSockets without
                    # touching their TLS state from this thread.
                    socket.socket.shutdown(sock, socket.SHUT_RDWR)


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    server_version = "proxmox-node-exporter"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def handle_one_request(self) -> None:
        # One deadline covers waiting for the request, reading it and
        # writing the response.
        key = self.server.deadlines.arm(self.connection, self.server.request_timeout)
        try:
            super().handle_one_request()
        finally:
            self.server.deadlines.disarm(key)

    def do_GET(self) -> None:
        self._handle(send_body=True)

    def do_HEAD(self) -> None:
        self._handle(send_body=False)

    def _handle(self, send_body: bool) -> None:
        path = urlsplit(self.path).path
        if path == "/healthz":
            ok = self.server.app.healthy()
            self._reply(200 if ok else 503, b"ok\n" if ok else b"unhealthy\n", send_body)
            return
        auth = self.server.auth
        if auth is not None:
            verdict = auth.check(self.headers.get("Authorization"), str(self.client_address[0]))
            if verdict is None:
                self._reply(429, b"too many requests\n", send_body, {"Retry-After": "1"})
                return
            if not verdict:
                challenge = 'Basic realm="proxmox-node-exporter", charset="UTF-8"'
                self._reply(401, b"unauthorized\n", send_body, {"WWW-Authenticate": challenge})
                return
        if path == "/metrics":
            body = self.server.app.render_metrics()
            headers = {"Content-Type": CONTENT_TYPE}
            if "gzip" in self.headers.get("Accept-Encoding", ""):
                body = gzip.compress(body, compresslevel=5)
                headers["Content-Encoding"] = "gzip"
            self._reply(200, body, send_body, headers)
        elif path == "/":
            self._reply(200, _LANDING, send_body, {"Content-Type": "text/html; charset=utf-8"})
        else:
            self._reply(404, b"not found\n", send_body)

    def _reply(
        self, code: int, body: bytes, send_body: bool, headers: dict[str, str] | None = None
    ) -> None:
        self.send_response(code)
        merged = {"Content-Type": "text/plain; charset=utf-8"}
        merged.update(headers or {})
        for name, value in merged.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def end_headers(self) -> None:
        # Here rather than in _reply() so send_error() responses get them too.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("%s - %s", self.address_string(), format % args)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(
        self,
        address: tuple[str, int],
        family: socket.AddressFamily,
        app: App,
        auth: BasicAuth | None,
        tls: TLSContextProvider | None,
        max_connections: int,
        request_timeout: float,
    ) -> None:
        self.address_family = family
        self.app = app
        self.auth = auth
        self.tls = tls
        self.request_timeout = request_timeout
        self._slots = threading.BoundedSemaphore(max_connections)
        self._per_client_max = max(2, max_connections // 4)
        self._per_client: dict[str, int] = {}
        self._clients_lock = threading.Lock()
        self.deadlines = _Deadlines()
        try:
            super().__init__(address, _Handler)
        except BaseException:
            self.deadlines.close()
            raise

    def server_bind(self) -> None:
        if self.address_family == socket.AF_INET6:
            with contextlib.suppress(OSError):
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        # Skip HTTPServer.server_bind(): it does a reverse DNS lookup.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)

    def process_request(self, request: Any, client_address: Any) -> None:
        client = str(client_address[0])
        with self._clients_lock:
            if self._per_client.get(client, 0) >= self._per_client_max:
                log.warning("too many connections from %s, dropping one", client)
                self.shutdown_request(request)
                return
            self._per_client[client] = self._per_client.get(client, 0) + 1
        if not self._slots.acquire(blocking=False):
            log.warning("connection limit reached, dropping connection from %s", client)
            self._release_client(client)
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            self._release_client(client)
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()
            self._release_client(str(client_address[0]))

    def _release_client(self, client: str) -> None:
        with self._clients_lock:
            remaining = self._per_client.get(client, 1) - 1
            if remaining > 0:
                self._per_client[client] = remaining
            else:
                self._per_client.pop(client, None)

    def finish_request(self, request: Any, client_address: Any) -> None:
        request.settimeout(self.request_timeout)
        if self.tls is None:
            _Handler(request, client_address, self)
            return
        key = self.deadlines.arm(request, self.request_timeout)
        try:
            conn = self.tls.get().wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError) as exc:
            log.debug("TLS handshake with %s failed: %s", client_address, exc)
            return
        finally:
            self.deadlines.disarm(key)
        try:
            _Handler(conn, client_address, self)
        finally:
            conn.close()

    def handle_error(self, request: Any, client_address: Any) -> None:
        log.debug("error while serving %s", client_address, exc_info=True)

    def server_close(self) -> None:
        super().server_close()
        self.deadlines.close()


def make_server(
    listen_address: str,
    app: App,
    web_config: WebConfig | None = None,
    *,
    max_connections: int = 32,
    request_timeout: float = 15.0,
) -> ThreadingHTTPServer:
    host, port = parse_listen_address(listen_address, allow_ephemeral=True)
    web_config = web_config or WebConfig()
    auth = BasicAuth(web_config.users) if web_config.users else None
    tls = TLSContextProvider(web_config) if web_config.tls_enabled else None

    def build(bind_host: str, family: socket.AddressFamily) -> _Server:
        return _Server((bind_host, port), family, app, auth, tls, max_connections, request_timeout)

    if host == "":
        try:
            return build("::", socket.AF_INET6)  # dual-stack
        except OSError:
            return build("0.0.0.0", socket.AF_INET)  # noqa: S104 - explicit "all interfaces"
    return build(host, socket.AF_INET6 if ":" in host else socket.AF_INET)
