from __future__ import annotations

import json
import logging
import socket
import ssl
import threading
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import pytest

from conftest import FakeRunner, Samples, collect, fixture, value
from proxmox_node_exporter.collectors import pve
from proxmox_node_exporter.collectors.base import Context
from proxmox_node_exporter.collectors.pve import (
    ApiSource,
    PveCollector,
    PveshSource,
    PveSourceError,
    parse_cluster_status,
    parse_members,
    read_token_file,
)
from proxmox_node_exporter.runner import CommandError

TOKEN = "monitoring@pve!exporter=5f3c9a0e-7b1d-4c2e-8f6a-1d2e3f4a5b6c"
OPTIONAL = ("/version", "/nodes/pve1/certificates/info", "/nodes/pve1/replication")


def load(name: str) -> Any:
    return json.loads(fixture(f"pve/{name}"))


def pvesh(path: str) -> tuple[str, ...]:
    return ("pvesh", "get", path, "--output-format", "json")


@dataclass
class PveFiles:
    members: Path
    replication: Path
    ca: Path


@pytest.fixture
def files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> PveFiles:
    """Point the /etc/pve paths at (initially missing) files below tmp_path."""
    etc = tmp_path / "etc-pve"
    etc.mkdir()
    paths = PveFiles(etc / ".members", etc / "replication.cfg", etc / "pve-root-ca.pem")
    monkeypatch.setattr(pve, "MEMBERS_FILE", str(paths.members))
    monkeypatch.setattr(pve, "REPLICATION_CFG", str(paths.replication))
    monkeypatch.setattr(pve, "PVE_CA_FILE", str(paths.ca))
    return paths


@pytest.fixture
def clustered(files: PveFiles) -> PveFiles:
    files.members.write_text(fixture("pve/members_cluster.json"))
    files.replication.write_text(fixture("pve/replication.cfg"))
    return files


def pvesh_runner(node: str = "pve1", **overrides: Any) -> FakeRunner:
    responses: dict[tuple[str, ...], Any] = {
        pvesh("/cluster/resources"): fixture("pve/cluster_resources.json"),
        pvesh("/cluster/status"): fixture("pve/cluster_status.json"),
        pvesh("/version"): fixture("pve/version.json"),
        pvesh(f"/nodes/{node}/certificates/info"): fixture("pve/certificates_info.json"),
        pvesh(f"/nodes/{node}/replication"): fixture("pve/replication.json"),
    }
    for path, response in overrides.items():
        responses[pvesh(path)] = response
    return FakeRunner(responses)


def called(runner: FakeRunner) -> list[str]:
    return [argv[2] for argv in runner.calls]


def guest(vmid: str, name: str, kind: str) -> dict[str, str]:
    return {"vmid": vmid, "name": name, "type": kind}


WEB01 = guest("100", "web01", "qemu")
WIN11 = guest("101", "win11", "qemu")
TEMPLATE = guest("9000", "debian12-cloud", "qemu")
DNS = guest("200", "dns", "lxc")
RUNNER = guest("201", "ci-runner", "lxc")


def assert_pve1_guests_and_storage(samples: Samples) -> None:
    # Only guests of the local node; pve2's guests (102, 202) are filtered out.
    assert {dict(k)["vmid"] for k in samples["pve_vm_status"]} == {
        "100",
        "101",
        "9000",
        "200",
        "201",
    }
    info = "pve_vm_info"
    assert value(samples, info, **WEB01, tags="prod;web", template="0", pool="production") == 1
    assert value(samples, info, **TEMPLATE, tags="template", template="1", pool="") == 1
    assert value(samples, info, **RUNNER, tags="", template="0", pool="") == 1
    assert value(samples, "pve_vm_status", **WEB01) == 1
    assert value(samples, "pve_vm_status", **WIN11) == 0
    assert value(samples, "pve_vm_status", **TEMPLATE) == 0
    assert value(samples, "pve_vm_status", **DNS) == 1
    assert value(samples, "pve_vm_uptime_seconds", **WEB01) == 1209600
    assert value(samples, "pve_vm_uptime_seconds", **WIN11) == 0
    assert value(samples, "pve_vm_cpu_usage_percent", **WEB01) == pytest.approx(4.21853010033445)
    assert value(samples, "pve_vm_cpu_usage_percent", **WIN11) == 0
    assert value(samples, "pve_vm_cpus", **WEB01) == 4
    assert value(samples, "pve_vm_memory_total_bytes", **WEB01) == 8589934592
    assert value(samples, "pve_vm_memory_used_bytes", **WEB01) == 5712486400
    assert value(samples, "pve_vm_disk_total_bytes", **WEB01) == 68719476736
    assert value(samples, "pve_vm_disk_used_bytes", **WEB01) == 0
    assert value(samples, "pve_vm_disk_used_bytes", **DNS) == 1924358144
    assert value(samples, "pve_vm_disk_read_bytes_total", **WEB01) == 8263901696
    assert value(samples, "pve_vm_disk_write_bytes_total", **WEB01) == 41293459456
    assert value(samples, "pve_vm_network_receive_bytes_total", **WEB01) == 152306981344
    assert value(samples, "pve_vm_network_transmit_bytes_total", **WEB01) == 98423011210
    assert value(samples, "pve_vm_count", type="qemu", status="running") == 1
    # The template is not counted as a stopped guest.
    assert value(samples, "pve_vm_count", type="qemu", status="stopped") == 1
    assert value(samples, "pve_vm_count", type="lxc", status="running") == 1
    assert value(samples, "pve_vm_count", type="lxc", status="stopped") == 1

    local = {"storage": "local", "type": "dir"}
    assert value(samples, "pve_storage_info", **local, shared="0", content="iso,vztmpl,backup") == 1
    assert value(samples, "pve_storage_active", **local) == 1
    # pve2's "local" storage has the same labels; the local node's values must win.
    assert value(samples, "pve_storage_total_bytes", **local) == 100861726720
    assert value(samples, "pve_storage_used_bytes", **local) == 8123358208
    assert value(samples, "pve_storage_available_bytes", **local) == 100861726720 - 8123358208
    nfs = {"storage": "nfs-backup", "type": "nfs"}
    assert value(samples, "pve_storage_info", **nfs, shared="1", content="backup") == 1
    assert value(samples, "pve_storage_used_bytes", **nfs) == 1520000000000
    usb = {"storage": "usb-backup", "type": "dir"}
    assert value(samples, "pve_storage_active", **usb) == 0
    assert value(samples, "pve_storage_total_bytes", **usb) is None
    assert len(samples["pve_storage_active"]) == 4


def assert_cluster(samples: Samples) -> None:
    assert value(samples, "pve_cluster_info", cluster="homelab") == 1
    assert value(samples, "pve_cluster_quorate") == 1
    assert value(samples, "pve_cluster_nodes") == 3
    assert value(samples, "pve_cluster_nodes_online") == 2
    assert value(samples, "pve_cluster_member_online", member="pve1") == 1
    assert value(samples, "pve_cluster_member_online", member="pve2") == 1
    assert value(samples, "pve_cluster_member_online", member="pve3") == 0


def assert_optional(samples: Samples) -> None:
    assert value(samples, "pve_version_info", version="8.2.7", release="8.2") == 1
    expiry = "pve_certificate_expiry_timestamp_seconds"
    assert value(samples, expiry, certificate="pve-root-ca.pem") == 1994512345
    assert value(samples, expiry, certificate="pve-ssl.pem") == 1742224345
    assert value(samples, expiry, certificate="pveproxy-ssl.pem") == 1735689600
    ok = {"job": "100-0", "guest": "100", "target": "pve2"}
    assert value(samples, "pve_replication_last_sync_timestamp_seconds", **ok) == 1727584201
    assert value(samples, "pve_replication_duration_seconds", **ok) == 4.216512
    assert value(samples, "pve_replication_failures", **ok) == 0
    failing = {"job": "200-0", "guest": "200", "target": "pve2"}
    assert value(samples, "pve_replication_failures", **failing) == 3
    assert value(samples, "pve_replication_last_sync_timestamp_seconds", **failing) == 1727497801


def assert_no_optional(samples: Samples) -> None:
    assert "pve_version_info" not in samples
    assert "pve_certificate_expiry_timestamp_seconds" not in samples
    assert "pve_replication_failures" not in samples


# -- detection -------------------------------------------------------------------------


def test_detect_root_with_pvesh(make_ctx: Callable[..., Context]) -> None:
    assert PveCollector(make_ctx(pvesh_runner())).detect()


def test_detect_needs_root_for_pvesh(make_ctx: Callable[..., Context]) -> None:
    assert not PveCollector(make_ctx(pvesh_runner(), is_root=False)).detect()


def test_detect_without_pvesh(make_ctx: Callable[..., Context]) -> None:
    assert not PveCollector(make_ctx(FakeRunner())).detect()


def test_detect_with_api_token_unprivileged(make_ctx: Callable[..., Context]) -> None:
    ctx = make_ctx(FakeRunner(), is_root=False, pve_api_token_file="/etc/token")
    assert PveCollector(ctx).detect()


# -- pvesh -----------------------------------------------------------------------------


def test_pvesh_clustered_node(clustered: PveFiles, make_ctx: Callable[..., Context]) -> None:
    runner = pvesh_runner()
    samples = collect(PveCollector(make_ctx(runner)))
    assert_pve1_guests_and_storage(samples)
    assert_cluster(samples)
    assert_optional(samples)
    # Quorum comes from /etc/pve/.members; no API call is needed for it.
    assert called(runner) == ["/cluster/resources", *OPTIONAL]
    assert all(argv[3:] == ("--output-format", "json") for argv in runner.calls)


def test_optional_data_is_cached(clustered: PveFiles, make_ctx: Callable[..., Context]) -> None:
    runner = pvesh_runner()
    collector = PveCollector(make_ctx(runner))
    collect(collector)
    samples = collect(collector)
    assert_optional(samples)
    assert called(runner).count("/cluster/resources") == 2
    for path in OPTIONAL:
        assert called(runner).count(path) == 1


def test_standalone_node(files: PveFiles, make_ctx: Callable[..., Context]) -> None:
    files.members.write_text(fixture("pve/members_standalone.json"))
    resources = [
        dict(r, node="pve") for r in load("cluster_resources.json") if r.get("node") == "pve1"
    ]
    runner = pvesh_runner(node="pve", **{"/cluster/resources": json.dumps(resources)})
    samples = collect(PveCollector(make_ctx(runner)))
    assert not any(name.startswith("pve_cluster") for name in samples)
    assert_pve1_guests_and_storage(samples)
    # No replication.cfg: the (slow) replication call is skipped entirely.
    assert called(runner) == ["/cluster/resources", "/version", "/nodes/pve/certificates/info"]
    assert "pve_replication_failures" not in samples


def test_members_file_unreadable_falls_back_to_cluster_status(
    files: PveFiles, make_ctx: Callable[..., Context]
) -> None:
    files.replication.write_text(fixture("pve/replication.cfg"))
    runner = pvesh_runner()
    samples = collect(PveCollector(make_ctx(runner)))
    assert called(runner)[0] == "/cluster/status"
    assert_cluster(samples)
    assert_pve1_guests_and_storage(samples)


def test_corrupt_members_file_falls_back_to_cluster_status(
    files: PveFiles, make_ctx: Callable[..., Context]
) -> None:
    files.members.write_text('{\n"nodename": "pve1",\n"version": 12,\n"cluster": { "name"')
    runner = pvesh_runner()
    samples = collect(PveCollector(make_ctx(runner)))
    assert "/cluster/status" in called(runner)
    assert_cluster(samples)


def test_standalone_cluster_status(files: PveFiles, make_ctx: Callable[..., Context]) -> None:
    runner = pvesh_runner(
        node="pve", **{"/cluster/status": fixture("pve/cluster_status_standalone.json")}
    )
    samples = collect(PveCollector(make_ctx(runner)))
    assert not any(name.startswith("pve_cluster") for name in samples)
    assert "/nodes/pve/certificates/info" in called(runner)
    # pve1/pve2 resources belong to other nodes.
    assert value(samples, "pve_vm_count", type="qemu", status="running") == 0
    assert "pve_vm_status" not in samples


def test_node_setting_overrides_local_name(
    clustered: PveFiles, make_ctx: Callable[..., Context]
) -> None:
    runner = pvesh_runner(node="pve2")
    samples = collect(PveCollector(make_ctx(runner, pve_node="pve2")))
    assert {dict(k)["vmid"] for k in samples["pve_vm_status"]} == {"102", "202"}
    assert value(samples, "pve_storage_used_bytes", storage="local", type="dir") == 6123358208
    assert "/nodes/pve2/certificates/info" in called(runner)


def test_hostname_fallback(
    files: PveFiles, make_ctx: Callable[..., Context], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pve.socket, "gethostname", lambda: "pve1.example.lan")
    runner = pvesh_runner(**{"/cluster/status": "[]"})
    samples = collect(PveCollector(make_ctx(runner)))
    assert value(samples, "pve_vm_status", **WEB01) == 1


@pytest.mark.parametrize("node", ["pve1;reboot", "../nodes", "pve 1", "-pve1", "pve1\n"])
def test_invalid_node_name_is_rejected(
    files: PveFiles, make_ctx: Callable[..., Context], node: str
) -> None:
    files.members.write_text(json.dumps({"nodename": node, "version": 0}))
    runner = pvesh_runner()
    with pytest.raises(PveSourceError, match="invalid node name"):
        collect(PveCollector(make_ctx(runner, pve_node=node)))
    assert runner.calls == []


def test_optional_failures_do_not_fail_the_collector(
    clustered: PveFiles, make_ctx: Callable[..., Context]
) -> None:
    runner = pvesh_runner(
        **{
            "/version": CommandError("pvesh: exit status 255: Connection refused"),
            "/nodes/pve1/certificates/info": "not json",
            "/nodes/pve1/replication": CommandError("pvesh: timed out after 30s"),
        }
    )
    collector = PveCollector(make_ctx(runner))
    samples = collect(collector)
    assert_pve1_guests_and_storage(samples)
    assert_cluster(samples)
    assert_no_optional(samples)
    # Failures are not cached; the next run retries.
    collect(collector)
    assert called(runner).count("/version") == 2


def test_resources_failure_fails_the_collector(
    clustered: PveFiles, make_ctx: Callable[..., Context]
) -> None:
    runner = pvesh_runner(**{"/cluster/resources": CommandError("pvesh: exit status 2")})
    with pytest.raises(CommandError):
        collect(PveCollector(make_ctx(runner)))


@pytest.mark.parametrize(("output", "error"), [("<html>", "invalid JSON"), ("{}", "unexpected")])
def test_bad_resources_fail_the_collector(
    clustered: PveFiles, make_ctx: Callable[..., Context], output: str, error: str
) -> None:
    runner = pvesh_runner(**{"/cluster/resources": output})
    with pytest.raises(PveSourceError, match=error):
        collect(PveCollector(make_ctx(runner)))


def test_parsers() -> None:
    cluster = parse_members(load("members_cluster.json"))
    assert (cluster.local, cluster.name, cluster.quorate, cluster.configured) == (
        "pve1",
        "homelab",
        True,
        3,
    )
    assert cluster.members == {"pve1": True, "pve2": True, "pve3": False}
    assert parse_cluster_status(load("cluster_status.json")) == cluster
    alone = parse_members(load("members_standalone.json"))
    assert (alone.local, alone.name, alone.members) == ("pve", None, {})
    status = parse_cluster_status(load("cluster_status_standalone.json"))
    assert (status.local, status.name, status.members) == ("pve", None, {"pve": True})


# -- API token file --------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [TOKEN, f"{TOKEN}\n", f"  {TOKEN}\r\n", f"PVEAPIToken={TOKEN}\n"],
)
def test_read_token_file(tmp_path: Path, content: str) -> None:
    path = tmp_path / "token"
    path.write_text(content)
    path.chmod(0o640)
    assert read_token_file(str(path)) == TOKEN


@pytest.mark.parametrize(
    "content",
    [
        "",
        "hunter2",
        "monitoring@pve",
        "monitoring@pve!exporter",
        "monitoring@pve!exporter=",
        "monitoring!exporter=5f3c9a0e-7b1d-4c2e-8f6a-1d2e3f4a5b6c",
        "monitoring@pve!exp orter=5f3c9a0e-7b1d-4c2e-8f6a-1d2e3f4a5b6c",
        "monitoring@pve!exporter=5f3c9a0e 7b1d",
        f"PVEAPIToken {TOKEN}",
        f"{TOKEN}\r\nX-Injected: 1",
        f"{TOKEN}\n{TOKEN}",
        "PVEAuthCookie=PVE:root@pam:66F8A1B2::c2lnbmF0dXJl",
    ],
)
def test_read_token_file_rejects_garbage(tmp_path: Path, content: str) -> None:
    path = tmp_path / "token"
    path.write_text(content)
    path.chmod(0o600)
    with pytest.raises(PveSourceError, match="expected USER@REALM!TOKENID=SECRET"):
        read_token_file(str(path))


def test_read_token_file_missing(tmp_path: Path) -> None:
    with pytest.raises(PveSourceError, match="cannot read API token file"):
        read_token_file(str(tmp_path / "missing"))


def test_world_readable_token_file_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "token"
    path.write_text(TOKEN)
    path.chmod(0o644)
    with caplog.at_level(logging.WARNING, logger=pve.__name__):
        assert read_token_file(str(path)) == TOKEN
    assert "world-readable" in caplog.text
    caplog.clear()
    path.chmod(0o640)
    with caplog.at_level(logging.WARNING, logger=pve.__name__):
        read_token_file(str(path))
    assert caplog.text == ""


# -- source selection ------------------------------------------------------------------


@dataclass
class RecordedSource:
    args: tuple[Any, ...]


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "token"
    path.write_text(TOKEN + "\n")
    path.chmod(0o600)
    return path


def test_source_uses_pvesh_by_default(make_ctx: Callable[..., Context]) -> None:
    collector = PveCollector(make_ctx(pvesh_runner()))
    assert isinstance(collector.source(), PveshSource)
    assert collector.source() is collector.source()


def test_source_uses_cluster_ca_when_readable(
    files: PveFiles,
    token_file: Path,
    make_ctx: Callable[..., Context],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pve, "ApiSource", lambda *args: RecordedSource(args))
    ctx = make_ctx(is_root=False, pve_api_token_file=str(token_file))
    assert PveCollector(ctx).source().args == ("https://127.0.0.1:8006", TOKEN, None, False)
    files.ca.write_text("-----BEGIN CERTIFICATE-----\n")
    assert PveCollector(ctx).source().args == (
        "https://127.0.0.1:8006",
        TOKEN,
        str(files.ca),
        False,
    )
    ctx = make_ctx(
        is_root=False,
        pve_api_token_file=str(token_file),
        pve_api_url="https://pve.example.com:8006",
        pve_api_ca_file="/etc/ssl/certs/internal-ca.pem",
        pve_api_insecure=True,
    )
    assert PveCollector(ctx).source().args == (
        "https://pve.example.com:8006",
        TOKEN,
        "/etc/ssl/certs/internal-ca.pem",
        True,
    )


def test_source_with_bad_token_file_fails(
    files: PveFiles, tmp_path: Path, make_ctx: Callable[..., Context]
) -> None:
    bad = tmp_path / "token"
    bad.write_text("root@pam:password")
    collector = PveCollector(make_ctx(is_root=False, pve_api_token_file=str(bad)))
    with pytest.raises(PveSourceError):
        collect(collector)


def test_insecure_disables_verification() -> None:
    def context(source: ApiSource) -> ssl.SSLContext:
        handler = next(
            h for h in source._opener.handlers if isinstance(h, urllib.request.HTTPSHandler)
        )
        ctx: ssl.SSLContext = handler._context  # type: ignore[attr-defined]
        return ctx

    strict = context(ApiSource("https://127.0.0.1:8006", TOKEN, None, False))
    assert strict.verify_mode == ssl.CERT_REQUIRED
    assert strict.check_hostname
    # The Proxmox cluster CA has no keyUsage, which Python 3.13's strict mode rejects.
    assert not strict.verify_flags & ssl.VERIFY_X509_STRICT
    insecure = context(ApiSource("https://127.0.0.1:8006", TOKEN, None, True))
    assert insecure.verify_mode == ssl.CERT_NONE
    assert not insecure.check_hostname


# -- HTTPS API (served over plain HTTP locally) ----------------------------------------


@dataclass
class Route:
    status: int
    body: bytes
    reason: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Request:
    path: str
    authorization: str | None
    accept: str | None


@dataclass
class ApiServer:
    url: str = ""
    routes: dict[str, Route] = field(default_factory=dict)
    requests: list[Request] = field(default_factory=list)

    def data(self, path: str, data: Any) -> None:
        body = json.dumps({"data": data}).encode()
        self.routes[f"/api2/json{path}"] = Route(
            200, body, headers={"Content-Type": "application/json;charset=UTF-8"}
        )

    def error(self, path: str, status: int, reason: str) -> None:
        # pveproxy reports errors in the status line and returns {"data":null}.
        self.routes[f"/api2/json{path}"] = Route(status, b'{"data":null}', reason)

    @property
    def paths(self) -> list[str]:
        return [r.path for r in self.requests]


@pytest.fixture
def api() -> Iterator[ApiServer]:
    state = ApiServer()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            state.requests.append(
                Request(self.path, self.headers.get("Authorization"), self.headers.get("Accept"))
            )
            route = state.routes.get(
                self.path, Route(501, b'{"data":null}', "Method not implemented")
            )
            self.send_response(route.status, route.reason)
            for name, header in route.headers.items():
                self.send_header(name, header)
            self.send_header("Content-Length", str(len(route.body)))
            self.end_headers()
            self.wfile.write(route.body)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture(autouse=True)
def no_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A proxy from the environment must never see the token: point one at a dead port."""
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)


def test_api_get(api: ApiServer) -> None:
    api.data("/version", load("version.json"))
    source = ApiSource(api.url + "/", TOKEN, None, False)
    assert source.get("/version") == load("version.json")
    assert api.requests == [
        Request("/api2/json/version", f"PVEAPIToken={TOKEN}", "application/json")
    ]


def test_api_null_data(api: ApiServer) -> None:
    api.data("/nodes/pve1/replication", None)
    assert ApiSource(api.url, TOKEN, None, False).get("/nodes/pve1/replication") is None


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, "invalid token value!"),
        (403, "Permission check failed (/, Sys.Audit)"),
        (500, "Internal Server Error"),
        (595, "Connection refused"),
    ],
)
def test_api_http_errors(api: ApiServer, status: int, reason: str) -> None:
    api.error("/cluster/resources", status, reason)
    with pytest.raises(PveSourceError, match=f"HTTP {status}"):
        ApiSource(api.url, TOKEN, None, False).get("/cluster/resources")


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_api_does_not_follow_redirects(api: ApiServer, status: int) -> None:
    api.data("/elsewhere", {"stolen": True})
    api.routes["/api2/json/version"] = Route(
        status, b"", headers={"Location": f"{api.url}/api2/json/elsewhere"}
    )
    with pytest.raises(PveSourceError, match=f"HTTP {status}"):
        ApiSource(api.url, TOKEN, None, False).get("/version")
    assert api.paths == ["/api2/json/version"]


@pytest.mark.parametrize("body", [b"<html>502 Bad Gateway</html>", b'{"errors":{}}', b"[1,2]"])
def test_api_invalid_response(api: ApiServer, body: bytes) -> None:
    api.routes["/api2/json/version"] = Route(200, body)
    with pytest.raises(PveSourceError, match="invalid response"):
        ApiSource(api.url, TOKEN, None, False).get("/version")


def test_api_response_size_is_limited(api: ApiServer, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pve, "_MAX_RESPONSE", 64)
    api.data("/cluster/resources", load("cluster_resources.json"))
    with pytest.raises(PveSourceError, match="too large"):
        ApiSource(api.url, TOKEN, None, False).get("/cluster/resources")


def test_api_connection_refused() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with pytest.raises(PveSourceError, match="API /version"):
        ApiSource(f"http://127.0.0.1:{port}", TOKEN, None, False).get("/version")


def serve_cluster(api: ApiServer) -> None:
    api.data("/cluster/status", load("cluster_status.json"))
    api.data("/cluster/resources", load("cluster_resources.json"))
    api.data("/version", load("version.json"))
    api.data("/nodes/pve1/certificates/info", load("certificates_info.json"))
    api.data("/nodes/pve1/replication", load("replication.json"))


def test_collector_over_api(
    api: ApiServer, files: PveFiles, token_file: Path, make_ctx: Callable[..., Context]
) -> None:
    serve_cluster(api)
    runner = FakeRunner()
    ctx = make_ctx(runner, is_root=False, pve_api_token_file=str(token_file), pve_api_url=api.url)
    collector = PveCollector(ctx)
    assert collector.detect()
    samples = collect(collector)
    assert_pve1_guests_and_storage(samples)
    assert_cluster(samples)
    assert_optional(samples)
    assert runner.calls == []  # never falls back to pvesh
    # Without /etc/pve/.members (unprivileged) quorum comes from /cluster/status, and
    # replication is queried without looking at replication.cfg.
    assert api.paths == [
        "/api2/json/cluster/status",
        "/api2/json/cluster/resources",
        *(f"/api2/json{p}" for p in OPTIONAL),
    ]
    assert {r.authorization for r in api.requests} == {f"PVEAPIToken={TOKEN}"}


def test_collector_over_api_with_optional_failures(
    api: ApiServer, files: PveFiles, token_file: Path, make_ctx: Callable[..., Context]
) -> None:
    serve_cluster(api)
    api.error("/version", 500, "Internal Server Error")
    api.error(
        "/nodes/pve1/certificates/info", 403, "Permission check failed (/nodes/pve1, Sys.Audit)"
    )
    api.routes["/api2/json/nodes/pve1/replication"] = Route(200, b"garbage")
    ctx = make_ctx(is_root=False, pve_api_token_file=str(token_file), pve_api_url=api.url)
    samples = collect(PveCollector(ctx))
    assert_pve1_guests_and_storage(samples)
    assert_cluster(samples)
    assert_no_optional(samples)


def test_collector_over_api_resources_failure(
    api: ApiServer, files: PveFiles, token_file: Path, make_ctx: Callable[..., Context]
) -> None:
    serve_cluster(api)
    api.error("/cluster/resources", 403, "Permission check failed")
    ctx = make_ctx(is_root=False, pve_api_token_file=str(token_file), pve_api_url=api.url)
    with pytest.raises(PveSourceError, match="HTTP 403"):
        collect(PveCollector(ctx))
