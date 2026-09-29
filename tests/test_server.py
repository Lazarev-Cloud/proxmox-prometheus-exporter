"""End-to-end tests for the HTTP(S) server, driven through real sockets."""

from __future__ import annotations

import base64
import errno
import gzip
import http.client
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Callable

import pytest

from proxmox_node_exporter import server as server_module
from proxmox_node_exporter.server import make_server, parse_listen_address
from proxmox_node_exporter.webconfig import TLSContextProvider, WebConfig, hash_password

BODY = b"".join(b'test_server_sample{i="%d"} %d\n' % (i, i) for i in range(300))
PASSWORD = "s3cret-pass"
AUTH = WebConfig(users={"prom": hash_password(PASSWORD, iterations=1000)})
Response = tuple[int, dict[str, str], bytes]
Certs = dict[str, tuple[Path, Path]]


class FakeApp:
    def __init__(self) -> None:
        self.body = BODY
        self.is_healthy = True
        self.renders = 0

    def render_metrics(self) -> bytes:
        self.renders += 1
        return self.body

    def healthy(self) -> bool:
        return self.is_healthy


def basic(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def fetch(
    port: int,
    path: str = "/metrics",
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    context: ssl.SSLContext | None = None,
    host: str = "127.0.0.1",
    timeout: float = 5.0,
) -> Response:
    conn: http.client.HTTPConnection
    if context is None:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
    else:
        conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=context)
    try:
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


def try_status(port: int, path: str = "/healthz", **kwargs: Any) -> int | None:
    try:
        return fetch(port, path, timeout=2.0, **kwargs)[0]
    except (http.client.HTTPException, OSError):
        return None


def recv_all(sock: socket.socket) -> bytes:
    chunks = []
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    except ConnectionResetError:
        pass
    return b"".join(chunks)


def raw_exchange(port: int, data: bytes, timeout: float = 5.0) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(data)
        return recv_all(sock)


def eventually(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


Serve = Callable[..., Any]


@pytest.fixture
def serve() -> Iterator[Serve]:
    started: list[tuple[Any, threading.Thread]] = []

    def start(
        app: FakeApp | None = None,
        web_config: WebConfig | None = None,
        listen: str = "127.0.0.1:0",
        **kwargs: Any,
    ) -> Any:
        srv = make_server(listen, app or FakeApp(), web_config, **kwargs)
        thread = threading.Thread(
            target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        started.append((srv, thread))
        return srv

    yield start
    for srv, thread in started:
        srv.shutdown()
        srv.server_close()
        thread.join(5)
        assert not thread.is_alive()


def port_of(srv: Any) -> int:
    return int(srv.server_address[1])


# -- plain HTTP ----------------------------------------------------------------


def test_metrics(serve: Serve) -> None:
    app = FakeApp()
    port = port_of(serve(app))
    status, headers, body = fetch(port, "/metrics")
    assert status == 200
    assert headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    assert headers["content-length"] == str(len(BODY))
    assert "content-encoding" not in headers
    assert body == BODY
    assert fetch(port, "/metrics?debug=1")[2] == BODY
    assert app.renders == 2


def test_metrics_gzip(serve: Serve) -> None:
    port = port_of(serve())
    status, headers, body = fetch(port, "/metrics", headers={"Accept-Encoding": "deflate, gzip"})
    assert status == 200
    assert headers["content-encoding"] == "gzip"
    assert headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    assert headers["content-length"] == str(len(body))
    assert len(body) < len(BODY)
    assert gzip.decompress(body) == BODY
    assert "content-encoding" not in fetch(port, headers={"Accept-Encoding": "identity"})[1]


def test_head_has_no_body(serve: Serve) -> None:
    port = port_of(serve())
    response = raw_exchange(port, b"HEAD /metrics HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    head, sep, rest = response.partition(b"\r\n\r\n")
    assert sep
    assert head.startswith(b"HTTP/1.1 200 ")
    assert f"Content-Length: {len(BODY)}".encode() in head
    assert rest == b""

    # Keep-alive stays in sync after HEAD (no stray body bytes on the wire).
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("HEAD", "/healthz")
        first = conn.getresponse()
        assert first.status == 200
        assert first.read() == b""
        conn.request("GET", "/healthz")
        second = conn.getresponse()
        assert (second.status, second.read()) == (200, b"ok\n")
    finally:
        conn.close()


def test_healthz(serve: Serve) -> None:
    app = FakeApp()
    port = port_of(serve(app))
    assert fetch(port, "/healthz")[::2] == (200, b"ok\n")
    app.is_healthy = False
    status, headers, body = fetch(port, "/healthz")
    assert (status, body) == (503, b"unhealthy\n")
    assert headers["content-type"] == "text/plain; charset=utf-8"
    assert app.renders == 0


def test_landing_page(serve: Serve) -> None:
    status, headers, body = fetch(port_of(serve()), "/")
    assert status == 200
    assert headers["content-type"] == "text/html; charset=utf-8"
    assert b'<a href="/metrics">' in body


def test_unknown_path(serve: Serve) -> None:
    app = FakeApp()
    port = port_of(serve(app))
    for path in ("/nope", "/metrics/", "/metrics.txt", "/%6detrics", "/healthz/"):
        assert fetch(port, path)[::2] == (404, b"not found\n"), path
    assert app.renders == 0


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "OPTIONS"])
def test_other_methods_not_implemented(serve: Serve, method: str) -> None:
    app = FakeApp()
    assert fetch(port_of(serve(app)), "/metrics", method=method)[0] == 501
    assert app.renders == 0


@pytest.mark.parametrize(
    ("method", "path", "auth", "status"),
    [
        ("GET", "/metrics", True, 200),
        ("GET", "/", True, 200),
        ("GET", "/healthz", False, 200),
        ("GET", "/missing", True, 404),
        ("GET", "/metrics", False, 401),
        ("POST", "/metrics", True, 501),
    ],
)
def test_security_headers_on_every_response(
    serve: Serve, method: str, path: str, auth: bool, status: int
) -> None:
    port = port_of(serve(web_config=AUTH))
    headers = basic("prom", PASSWORD) if auth else {}
    got, response_headers, _ = fetch(port, path, method=method, headers=headers)
    assert got == status
    assert response_headers["x-content-type-options"] == "nosniff"
    assert response_headers["cache-control"] == "no-store"
    server = response_headers["server"]
    assert server.strip() == "proxmox-node-exporter"
    assert "python" not in server.lower()


def test_oversized_request_line_is_refused(serve: Serve) -> None:
    port = port_of(serve())
    response = raw_exchange(port, b"GET /" + b"a" * 70000 + b" HTTP/1.1\r\nHost: x\r\n\r\n")
    assert response.startswith(b"HTTP/1.1 414 ")
    assert b"X-Content-Type-Options: nosniff" in response
    assert fetch(port, "/healthz")[0] == 200


def test_error_page_escapes_request_data(serve: Serve) -> None:
    port = port_of(serve())
    response = raw_exchange(port, b"<script>alert(1)</script> /metrics HTTP/1.1\r\n\r\n")
    head, _, body = response.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 501 ")
    assert b"X-Content-Type-Options: nosniff" in head
    assert b"<script>" not in body


# -- basic auth ------------------------------------------------------------------


def test_basic_auth(serve: Serve) -> None:
    app = FakeApp()
    port = port_of(serve(app, AUTH))
    for headers in ({}, basic("prom", "wrong"), basic("nobody", PASSWORD), basic("Prom", PASSWORD)):
        status, response_headers, body = fetch(port, "/metrics", headers=headers)
        assert (status, body) == (401, b"unauthorized\n")
        assert response_headers["www-authenticate"] == (
            'Basic realm="proxmox-node-exporter", charset="UTF-8"'
        )
    assert app.renders == 0
    status, _, body = fetch(port, "/metrics", headers=basic("prom", PASSWORD))
    assert (status, body) == (200, BODY)
    assert app.renders == 1
    # Everything but /healthz is protected, so paths cannot be probed.
    assert fetch(port, "/")[0] == 401
    assert fetch(port, "/missing")[0] == 401
    assert fetch(port, "/missing", headers=basic("prom", PASSWORD))[0] == 404
    assert fetch(port, "/", headers=basic("prom", PASSWORD))[0] == 200


def test_healthz_needs_no_auth(serve: Serve) -> None:
    app = FakeApp()
    port = port_of(serve(app, AUTH))
    assert fetch(port, "/healthz")[::2] == (200, b"ok\n")
    app.is_healthy = False
    assert fetch(port, "/healthz")[0] == 503


def test_non_ascii_authorization_header_is_rejected(serve: Serve) -> None:
    port = port_of(serve(web_config=AUTH))
    response = raw_exchange(
        port,
        b"GET /metrics HTTP/1.1\r\nHost: x\r\nAuthorization: Basic \xe9\xe9\xe9\xe9\r\n"
        b"Connection: close\r\n\r\n",
    )
    assert response.startswith(b"HTTP/1.1 401 ")


def test_failed_logins_are_rate_limited(serve: Serve) -> None:
    app = FakeApp()
    port = port_of(serve(app, AUTH))
    good = basic("prom", PASSWORD)
    assert fetch(port, headers=good)[0] == 200  # now cached
    results = [fetch(port, headers=basic("prom", f"guess{i}")) for i in range(15)]
    statuses = [r[0] for r in results]
    assert statuses[0] == 401
    assert 429 in statuses
    limited = results[statuses.index(429)]
    assert limited[1]["retry-after"] == "1"
    assert limited[2] == b"too many requests\n"
    # A scraper whose credentials were already verified is not locked out.
    assert fetch(port, headers=good)[0] == 200
    assert app.renders == 2


# -- TLS -------------------------------------------------------------------------


def _openssl(*args: str | Path) -> None:
    subprocess.run(["openssl", *map(str, args)], check=True, capture_output=True)


def _make_ca(directory: Path, name: str) -> tuple[Path, Path]:
    crt, key = directory / f"{name}.crt", directory / f"{name}.key"
    _openssl(
        "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
        "-keyout", key, "-out", crt, "-days", "2", "-subj", f"/CN={name}",
    )  # fmt: skip
    return crt, key


def _make_leaf(directory: Path, name: str, ca: tuple[Path, Path], serial: int) -> tuple[Path, Path]:
    crt, key = directory / f"{name}.crt", directory / f"{name}.key"
    csr, ext = directory / f"{name}.csr", directory / f"{name}.ext"
    ext.write_text(
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature\n"
        "extendedKeyUsage=serverAuth,clientAuth\n"
        "subjectAltName=IP:127.0.0.1,IP:::1,DNS:localhost\n"
        "authorityKeyIdentifier=keyid\n"
        "subjectKeyIdentifier=hash\n"
    )
    _openssl(
        "req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
        "-keyout", key, "-out", csr, "-subj", f"/CN={name}",
    )  # fmt: skip
    _openssl(
        "x509", "-req", "-in", csr, "-CA", ca[0], "-CAkey", ca[1], "-set_serial", str(serial),
        "-days", "2", "-extfile", ext, "-out", crt,
    )  # fmt: skip
    return crt, key


@pytest.fixture(scope="module")
def certs(tmp_path_factory: pytest.TempPathFactory) -> Certs:
    directory = tmp_path_factory.mktemp("tls")
    ca = _make_ca(directory, "test-ca")
    other_ca = _make_ca(directory, "other-ca")
    return {
        "ca": ca,
        "server": _make_leaf(directory, "server-a", ca, 2),
        "server-b": _make_leaf(directory, "server-b", ca, 3),
        "client": _make_leaf(directory, "client", ca, 4),
        "rogue-client": _make_leaf(directory, "rogue", other_ca, 5),
    }


def tls_config(certs: Certs, **kwargs: Any) -> WebConfig:
    crt, key = certs["server"]
    return WebConfig(tls_cert_file=str(crt), tls_key_file=str(key), **kwargs)


def client_context(
    certs: Certs, client: str | None = None, maximum: ssl.TLSVersion | None = None
) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=str(certs["ca"][0]))
    if client is not None:
        crt, key = certs[client]
        ctx.load_cert_chain(str(crt), str(key))
    if maximum is not None:
        ctx.maximum_version = maximum
    return ctx


def peer_certificate(port: int, ctx: ssl.SSLContext) -> bytes:
    with (
        socket.create_connection(("127.0.0.1", port), timeout=5) as raw,
        ctx.wrap_socket(raw, server_hostname="127.0.0.1") as tls,
    ):
        der = tls.getpeercert(binary_form=True)
    assert der is not None
    return der


def der_of(path: Path) -> bytes:
    return ssl.PEM_cert_to_DER_cert(path.read_text())


def test_https(serve: Serve, certs: Certs) -> None:
    port = port_of(serve(web_config=tls_config(certs)))
    ctx = client_context(certs)
    status, headers, body = fetch(port, "/metrics", context=ctx)
    assert (status, body) == (200, BODY)
    assert headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    assert peer_certificate(port, ctx) == der_of(certs["server"][0])


def test_https_with_basic_auth(serve: Serve, certs: Certs) -> None:
    config = tls_config(certs)
    config.users = dict(AUTH.users)
    port = port_of(serve(web_config=config))
    ctx = client_context(certs)
    assert fetch(port, context=ctx)[0] == 401
    assert fetch(port, context=ctx, headers=basic("prom", PASSWORD))[0] == 200


def test_plain_http_to_tls_port_fails_fast(serve: Serve, certs: Certs) -> None:
    port = port_of(serve(web_config=tls_config(certs), request_timeout=5.0))
    start = time.monotonic()
    with pytest.raises((http.client.HTTPException, ConnectionError, ssl.SSLError)):
        fetch(port, "/metrics", timeout=3.0)
    assert time.monotonic() - start < 2.5
    assert fetch(port, context=client_context(certs))[0] == 200


def test_stalled_handshake_does_not_block_other_clients(serve: Serve, certs: Certs) -> None:
    port = port_of(serve(web_config=tls_config(certs), request_timeout=5.0))
    with socket.create_connection(("127.0.0.1", port), timeout=5):
        # Handshakes run in worker threads, so an idle client cannot stall accept().
        start = time.monotonic()
        assert fetch(port, context=client_context(certs), timeout=3.0)[0] == 200
        assert time.monotonic() - start < 2.0


def test_stalled_handshake_times_out(serve: Serve, certs: Certs) -> None:
    port = port_of(serve(web_config=tls_config(certs), request_timeout=0.3))
    with socket.create_connection(("127.0.0.1", port), timeout=5) as idle:
        start = time.monotonic()
        assert recv_all(idle) == b""
        assert time.monotonic() - start < 2.5
    assert fetch(port, context=client_context(certs))[0] == 200


def test_mutual_tls(serve: Serve, certs: Certs) -> None:
    config = tls_config(certs, tls_client_ca_file=str(certs["ca"][0]))
    port = port_of(serve(web_config=config))
    for client in (None, "rogue-client"):
        with pytest.raises((http.client.HTTPException, ConnectionError, ssl.SSLError)):
            fetch(port, context=client_context(certs, client), timeout=3.0)
    assert fetch(port, context=client_context(certs, "client"))[::2] == (200, BODY)


def test_tls_min_version(serve: Serve, certs: Certs) -> None:
    port = port_of(serve(web_config=tls_config(certs, tls_min_version="TLSv1.3")))
    with pytest.raises(ssl.SSLError):
        fetch(port, context=client_context(certs, maximum=ssl.TLSVersion.TLSv1_2), timeout=3.0)
    assert fetch(port, context=client_context(certs))[0] == 200


def test_tls_certificate_hot_reload(
    serve: Serve, certs: Certs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(TLSContextProvider, "_CHECK_EVERY", 0.0)
    crt, key = tmp_path / "tls.crt", tmp_path / "tls.key"
    shutil.copyfile(certs["server"][0], crt)
    shutil.copyfile(certs["server"][1], key)
    port = port_of(serve(web_config=WebConfig(tls_cert_file=str(crt), tls_key_file=str(key))))
    ctx = client_context(certs)
    assert peer_certificate(port, ctx) == der_of(certs["server"][0])

    for src, dst in zip(certs["server-b"], (crt, key)):
        shutil.copyfile(src, dst)
        st = os.stat(dst)
        os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert peer_certificate(port, ctx) == der_of(certs["server-b"][0])

    crt.write_text("broken\n")  # a bad renewal keeps the working certificate
    st = os.stat(crt)
    os.utime(crt, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000_000))
    assert peer_certificate(port, ctx) == der_of(certs["server-b"][0])
    assert fetch(port, context=ctx)[0] == 200


# -- resource limits ---------------------------------------------------------------


def test_connection_cap(serve: Serve) -> None:
    port = port_of(serve(max_connections=1, request_timeout=5.0))
    held = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        held.sendall(b"GET /metrics HTTP/1.1\r\nHost: x\r\n")  # headers not finished
        start = time.monotonic()
        with socket.create_connection(("127.0.0.1", port), timeout=3) as extra:
            assert recv_all(extra) == b""  # dropped at once, not queued
        assert time.monotonic() - start < 2.0
        held.sendall(b"Connection: close\r\n\r\n")
        assert recv_all(held).startswith(b"HTTP/1.1 200 ")
    finally:
        held.close()
    # The slot is released once the held connection is done.
    assert eventually(lambda: try_status(port) == 200)


def test_idle_connection_times_out(serve: Serve) -> None:
    port = port_of(serve(request_timeout=0.3))
    for prefix in (b"", b"GET /metrics HTTP/1.1\r\nHost: x\r\n"):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            start = time.monotonic()
            sock.sendall(prefix)
            assert recv_all(sock) == b""
            assert time.monotonic() - start < 2.5
    assert fetch(port, "/healthz")[0] == 200


def test_keep_alive_connection_times_out(serve: Serve) -> None:
    port = port_of(serve(request_timeout=0.3))
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
        start = time.monotonic()
        response = recv_all(sock)
        assert response.startswith(b"HTTP/1.1 200 ")
        assert response.endswith(b"\r\n\r\nok\n")
        assert time.monotonic() - start < 2.5


# -- listen address --------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (":9101", ("", 9101)),
        ("0.0.0.0:9101", ("0.0.0.0", 9101)),  # noqa: S104
        ("127.0.0.1:1", ("127.0.0.1", 1)),
        ("localhost:65535", ("localhost", 65535)),
        ("[::1]:9101", ("::1", 9101)),
        ("[::]:9101", ("::", 9101)),
        ("[fe80::1%eth0]:9101", ("fe80::1%eth0", 9101)),
        (" :9101\n", ("", 9101)),
    ],
)
def test_parse_listen_address(value: str, expected: tuple[str, int]) -> None:
    assert parse_listen_address(value) == expected


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ("::1:9101", "must be bracketed"),
        ("::", "must be bracketed"),
        ("9101", "must be host:port"),
        ("127.0.0.1", "must be host:port"),
        ("", "must be host:port"),
        ("[::1]", "must be host:port"),
        ("[::1]9101", "must be host:port"),
        ("127.0.0.1:", "invalid port"),
        (":http", "invalid port"),
        ("[::1]:", "invalid port"),
        (":9101.5", "invalid port"),
        (":0", "out of range"),
        (":-1", "out of range"),
        (":65536", "out of range"),
        (":70000", "out of range"),
    ],
)
def test_parse_listen_address_invalid(value: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        parse_listen_address(value)


def test_ephemeral_port_only_when_allowed() -> None:
    assert parse_listen_address(":0", allow_ephemeral=True) == ("", 0)
    assert parse_listen_address("[::1]:0", allow_ephemeral=True) == ("::1", 0)
    for value in (":-1", ":65536"):
        with pytest.raises(ValueError, match="out of range"):
            parse_listen_address(value, allow_ephemeral=True)


def test_make_server_rejects_bad_addresses() -> None:
    for value in ("::1:9101", "127.0.0.1", ":70000"):
        with pytest.raises(ValueError, match=r"bracketed|host:port|out of range"):
            make_server(value, FakeApp())


def test_specific_ipv4_address(serve: Serve) -> None:
    srv = serve(listen="127.0.0.1:0")
    assert srv.address_family == socket.AF_INET
    assert srv.server_address[0] == "127.0.0.1"
    assert port_of(srv) > 0


def test_empty_host_binds_all_interfaces(serve: Serve) -> None:
    srv = serve(listen=":0")
    if srv.address_family == socket.AF_INET6:
        assert srv.server_address[0] == "::"
        assert srv.socket.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY) == 0
    else:
        assert srv.address_family == socket.AF_INET
        assert srv.server_address[0] == "0.0.0.0"  # noqa: S104
    # IPv4 clients reach it either way (dual-stack or the IPv4 fallback).
    assert fetch(port_of(srv), "/healthz")[0] == 200


def test_empty_host_falls_back_to_ipv4(serve: Serve, monkeypatch: pytest.MonkeyPatch) -> None:
    original = server_module._Server.server_bind

    def no_ipv6(self: Any) -> None:
        if self.address_family == socket.AF_INET6:
            raise OSError(errno.EAFNOSUPPORT, "Address family not supported by protocol")
        original(self)

    monkeypatch.setattr(server_module._Server, "server_bind", no_ipv6)
    srv = serve(listen=":0")
    assert srv.address_family == socket.AF_INET
    assert srv.server_address[0] == "0.0.0.0"  # noqa: S104
    assert fetch(port_of(srv), "/healthz")[0] == 200


def _has_ipv6_loopback() -> bool:
    try:
        with socket.socket(socket.AF_INET6) as sock:
            sock.bind(("::1", 0))
    except OSError:
        return False
    return True


@pytest.mark.skipif(not _has_ipv6_loopback(), reason="no IPv6 loopback")
def test_ipv6_address(serve: Serve) -> None:
    srv = serve(listen="[::1]:0")
    assert srv.address_family == socket.AF_INET6
    assert fetch(port_of(srv), "/healthz", host="::1")[0] == 200


def test_port_in_use(serve: Serve) -> None:
    srv = serve()
    with pytest.raises(OSError, match="in use") as excinfo:
        make_server(f"127.0.0.1:{port_of(srv)}", FakeApp())
    assert excinfo.value.errno == errno.EADDRINUSE


def test_trickling_client_is_cut_off_at_the_deadline(serve: Serve) -> None:
    """Slowloris: each byte arrives well within the socket timeout, but the
    request as a whole must still finish within request_timeout."""
    port = port_of(serve(request_timeout=0.6))
    stop = threading.Event()
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:

        def trickle() -> None:
            try:
                sock.sendall(b"GET /metrics HTTP/1.1\r\n")
                while not stop.wait(0.2):
                    sock.sendall(b"X-Slow: 1\r\n")
            except OSError:
                pass

        sender = threading.Thread(target=trickle, daemon=True)
        sender.start()
        start = time.monotonic()
        try:
            data = sock.recv(1024)
        except OSError:
            data = b""
        elapsed = time.monotonic() - start
        stop.set()
        sender.join(2)
    assert data == b""
    assert elapsed < 2.0
    assert fetch(port, "/healthz")[0] == 200


def test_connections_are_capped_per_client(serve: Serve) -> None:
    port = port_of(serve(max_connections=8, request_timeout=5.0))  # 2 per client address
    held = [socket.create_connection(("127.0.0.1", port), timeout=5) for _ in range(2)]
    try:
        for sock in held:
            sock.sendall(b"GET /metrics HTTP/1.1\r\nHost: x\r\n")  # headers not finished
        with socket.create_connection(("127.0.0.1", port), timeout=3) as extra:
            assert recv_all(extra) == b""  # third connection from the same address
    finally:
        for sock in held:
            sock.close()
    assert eventually(lambda: try_status(port) == 200)
