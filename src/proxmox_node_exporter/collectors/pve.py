"""Proxmox VE: guests, storage, cluster quorum, replication, version, certificates.

Data comes from the Proxmox API, either through ``pvesh`` (default, requires
root) or over HTTPS with an API token (lets the exporter run unprivileged;
a token with the built-in PVEAuditor role is enough).
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import ssl
import stat
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from ..metrics import Batch, MetricGroup
from .base import Collector, Context, read_text

log = logging.getLogger(__name__)

M = MetricGroup("pve")
_VM = ("vmid", "name", "type")
VERSION = M.gauge(
    "pve_version_info", "Proxmox VE version; the value is always 1.", "version", "release"
)
VM_INFO = M.gauge(
    "pve_vm_info", "Guest metadata; the value is always 1.", *_VM, "tags", "template", "pool"
)
VM_COUNT = M.gauge("pve_vm_count", "Guests on this node by type and status.", "type", "status")
VM_STATUS = M.gauge("pve_vm_status", "Whether the guest is running.", *_VM)
VM_UPTIME = M.gauge("pve_vm_uptime_seconds", "Guest uptime.", *_VM)
VM_CPU = M.gauge("pve_vm_cpu_usage_percent", "Guest CPU usage, % of its vCPUs.", *_VM)
VM_CPUS = M.gauge("pve_vm_cpus", "Number of vCPUs assigned to the guest.", *_VM)
VM_MEM_TOTAL = M.gauge("pve_vm_memory_total_bytes", "Memory assigned to the guest.", *_VM)
VM_MEM_USED = M.gauge("pve_vm_memory_used_bytes", "Memory used by the guest.", *_VM)
VM_DISK_TOTAL = M.gauge("pve_vm_disk_total_bytes", "Size of the guest's root disk.", *_VM)
VM_DISK_USED = M.gauge(
    "pve_vm_disk_used_bytes", "Used space on the guest's root disk (containers, agent).", *_VM
)
VM_DISK_READ = M.counter("pve_vm_disk_read_bytes_total", "Bytes read by the guest.", *_VM)
VM_DISK_WRITE = M.counter("pve_vm_disk_write_bytes_total", "Bytes written by the guest.", *_VM)
VM_NET_RX = M.counter("pve_vm_network_receive_bytes_total", "Bytes received by the guest.", *_VM)
VM_NET_TX = M.counter("pve_vm_network_transmit_bytes_total", "Bytes sent by the guest.", *_VM)

_ST = ("storage", "type")
STORAGE_INFO = M.gauge(
    "pve_storage_info", "Storage metadata; the value is always 1.", *_ST, "shared", "content"
)
STORAGE_ACTIVE = M.gauge("pve_storage_active", "Whether the storage is available.", *_ST)
STORAGE_TOTAL = M.gauge("pve_storage_total_bytes", "Storage size.", *_ST)
STORAGE_USED = M.gauge("pve_storage_used_bytes", "Storage space used.", *_ST)
STORAGE_AVAIL = M.gauge("pve_storage_available_bytes", "Storage space available.", *_ST)

CLUSTER_INFO = M.gauge("pve_cluster_info", "Cluster name; the value is always 1.", "cluster")
CLUSTER_QUORATE = M.gauge("pve_cluster_quorate", "Whether the cluster has quorum.")
CLUSTER_NODES = M.gauge("pve_cluster_nodes", "Nodes configured in the cluster.")
CLUSTER_NODES_ONLINE = M.gauge("pve_cluster_nodes_online", "Cluster nodes currently online.")
MEMBER_ONLINE = M.gauge(
    "pve_cluster_member_online", "Whether a cluster member is online.", "member"
)

REPL_LAST_SYNC = M.gauge(
    "pve_replication_last_sync_timestamp_seconds",
    "Time of the last successful storage replication.",
    "job",
    "guest",
    "target",
)
REPL_DURATION = M.gauge(
    "pve_replication_duration_seconds",
    "Duration of the last replication.",
    "job",
    "guest",
    "target",
)
REPL_FAILURES = M.gauge(
    "pve_replication_failures", "Consecutive failed replication runs.", "job", "guest", "target"
)
CERT_EXPIRY = M.gauge(
    "pve_certificate_expiry_timestamp_seconds",
    "Expiry time of the node's TLS certificates.",
    "certificate",
)

_NODE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*\Z")
_TOKEN_RE = re.compile(r"^[^@\s]+@[^!\s]+![A-Za-z0-9._-]+=[A-Za-z0-9-]+$")
_MAX_RESPONSE = 32 * 1024 * 1024
MEMBERS_FILE = "/etc/pve/.members"
REPLICATION_CFG = "/etc/pve/replication.cfg"
PVE_CA_FILE = "/etc/pve/pve-root-ca.pem"


class PveSourceError(Exception):
    pass


class PveshSource:
    """Local API access through pvesh (root only)."""

    def __init__(self, collector: Collector) -> None:
        self._collector = collector

    def get(self, path: str) -> Any:
        result = self._collector.run("pvesh", "get", path, "--output-format", "json", timeout=30)
        try:
            return json.loads(result.stdout or "null")
        except ValueError as exc:
            raise PveSourceError(f"pvesh {path}: invalid JSON") from exc


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class ApiSource:
    """HTTPS access with an API token (works unprivileged)."""

    def __init__(self, url: str, token: str, ca_file: str | None, insecure: bool) -> None:
        self._url = url.rstrip("/")
        self._auth = f"PVEAPIToken={token}"
        context = ssl.create_default_context(cafile=ca_file)
        # Python 3.13 turns on RFC 5280 strict mode, which rejects the Proxmox
        # cluster CA (it has no keyUsage extension). Chain, expiry and host
        # name are still verified.
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
        if insecure:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        # Never send the token through a proxy or follow a redirect with it.
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context),
            _NoRedirect(),
        )

    def get(self, path: str) -> Any:
        request = urllib.request.Request(  # noqa: S310 - the CLI only accepts https:// URLs
            f"{self._url}/api2/json{path}",
            headers={"Authorization": self._auth, "Accept": "application/json"},
        )
        try:
            with self._opener.open(request, timeout=15) as response:
                body = response.read(_MAX_RESPONSE + 1)
        except urllib.error.HTTPError as exc:
            raise PveSourceError(f"API {path}: HTTP {exc.code} {exc.reason}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise PveSourceError(f"API {path}: {exc}") from None
        if len(body) > _MAX_RESPONSE:
            raise PveSourceError(f"API {path}: response too large")
        try:
            return json.loads(body)["data"]
        except (ValueError, KeyError, TypeError) as exc:
            raise PveSourceError(f"API {path}: invalid response") from exc


def read_token_file(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            mode = os.fstat(fh.fileno()).st_mode
            token = fh.read().strip()
    except OSError as exc:
        raise PveSourceError(f"cannot read API token file: {exc}") from exc
    if stat.S_IMODE(mode) & 0o007:
        log.warning("%s is world-readable; restrict it with chmod 640", path)
    if token.startswith("PVEAPIToken="):
        token = token[len("PVEAPIToken=") :]
    if not _TOKEN_RE.match(token):
        raise PveSourceError(f"{path}: expected USER@REALM!TOKENID=SECRET")
    return token


@dataclass
class ClusterStatus:
    local: str | None = None
    name: str | None = None
    quorate: bool | None = None
    configured: int | None = None
    members: dict[str, bool] = field(default_factory=dict)


def parse_members(data: dict[str, Any]) -> ClusterStatus:
    status = ClusterStatus(local=data.get("nodename"))
    cluster = data.get("cluster")
    if isinstance(cluster, dict) and cluster.get("name"):
        status.name = str(cluster["name"])
        status.quorate = bool(cluster.get("quorate"))
        status.configured = int(cluster.get("nodes") or 0) or None
    for name, info in (data.get("nodelist") or {}).items():
        status.members[name] = bool(isinstance(info, dict) and info.get("online"))
    return status


def parse_cluster_status(items: list[dict[str, Any]]) -> ClusterStatus:
    status = ClusterStatus()
    for item in items or []:
        if item.get("type") == "cluster":
            status.name = str(item.get("name"))
            status.quorate = bool(item.get("quorate"))
            status.configured = int(item.get("nodes") or 0) or None
        elif item.get("type") == "node":
            name = str(item.get("name"))
            status.members[name] = bool(item.get("online"))
            if item.get("local"):
                status.local = name
    return status


class PveCollector(Collector):
    name = "pve"
    description = "Proxmox guests, storage, cluster quorum, replication, certificates, version"
    default_interval = 30.0

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        self._source: Any | None = None

    def detect(self) -> bool:
        if self.settings.pve_api_token_file:
            return True
        return self.ctx.is_root and self.has_command("pvesh")

    def source(self) -> Any:
        if self._source is None:
            settings = self.settings
            if settings.pve_api_token_file:
                token = read_token_file(settings.pve_api_token_file)
                ca_file = settings.pve_api_ca_file
                if ca_file is None and os.access(PVE_CA_FILE, os.R_OK):
                    ca_file = PVE_CA_FILE
                self._source = ApiSource(
                    settings.pve_api_url, token, ca_file, settings.pve_api_insecure
                )
            else:
                self._source = PveshSource(self)
        return self._source

    def collect(self, out: Batch) -> None:
        source = self.source()
        cluster = self._cluster_status(source)
        node = self._node_name(cluster)
        self._cluster(out, cluster)
        resources = source.get("/cluster/resources")
        if not isinstance(resources, list):
            raise PveSourceError("/cluster/resources: unexpected response")
        self._guests(out, [r for r in resources if r.get("node") == node])
        self._storage(out, [r for r in resources if r.get("node") == node])
        self._optional(out, source, node)

    def _optional(self, out: Batch, source: Any, node: str) -> None:
        """Slow-changing data, cached; a failure here doesn't fail the collector."""
        version = self._try("version", 3600, lambda: source.get("/version"))
        if isinstance(version, dict) and version.get("version"):
            number = str(version["version"])
            release = str(version.get("release") or ".".join(number.split(".")[:2]))
            out.add(VERSION, 1, version=number, release=release)
        certs = self._try(
            "certificates", 3600, lambda: source.get(f"/nodes/{node}/certificates/info")
        )
        for cert in certs if isinstance(certs, list) else []:
            if cert.get("filename") and cert.get("notafter"):
                out.add(CERT_EXPIRY, cert["notafter"], certificate=cert["filename"])
        jobs = self._try("replication", 120, lambda: self._replication_jobs(source, node))
        self._replication(out, jobs if isinstance(jobs, list) else [])

    def _try(self, key: str, ttl: float, fetch: Callable[[], Any]) -> Any:
        try:
            return self.cached(key, ttl, fetch)
        except Exception as exc:  # noqa: BLE001
            log.debug("pve %s: %s", key, exc)
            return None

    def _replication_jobs(self, source: Any, node: str) -> Any:
        if not self.settings.pve_api_token_file and not (read_text(REPLICATION_CFG) or ""):
            return []  # no jobs configured; skip the pvesh call
        return source.get(f"/nodes/{node}/replication")

    def _cluster_status(self, source: Any) -> ClusterStatus:
        text = read_text(MEMBERS_FILE)
        if text:
            try:
                return parse_members(json.loads(text))
            except (ValueError, TypeError, AttributeError):
                log.debug("cannot parse %s", MEMBERS_FILE)
        return parse_cluster_status(source.get("/cluster/status"))

    def _node_name(self, cluster: ClusterStatus) -> str:
        node = self.settings.pve_node or cluster.local or socket.gethostname().split(".")[0]
        if not _NODE_NAME_RE.match(node):
            raise PveSourceError(f"invalid node name {node!r}")
        return node

    @staticmethod
    def _cluster(out: Batch, cluster: ClusterStatus) -> None:
        if cluster.name is None:
            return  # standalone node
        out.add(CLUSTER_INFO, 1, cluster=cluster.name)
        out.add(CLUSTER_QUORATE, 1 if cluster.quorate else 0)
        out.add(CLUSTER_NODES, cluster.configured or len(cluster.members))
        out.add(CLUSTER_NODES_ONLINE, sum(cluster.members.values()))
        for member, online in cluster.members.items():
            out.add(MEMBER_ONLINE, 1 if online else 0, member=member)

    @staticmethod
    def _guests(out: Batch, resources: list[dict[str, Any]]) -> None:
        counts: dict[tuple[str, str], int] = {
            (kind, status): 0 for kind in ("qemu", "lxc") for status in ("running", "stopped")
        }
        for res in resources:
            kind = res.get("type")
            if kind not in ("qemu", "lxc") or res.get("vmid") is None:
                continue
            status = str(res.get("status") or "unknown")
            if not res.get("template"):  # templates are not guests that can run
                counts[(kind, status)] = counts.get((kind, status), 0) + 1
            labels = {
                "vmid": str(res["vmid"]),
                "name": str(res.get("name") or ""),
                "type": kind,
            }
            tags = ";".join(sorted(t for t in str(res.get("tags") or "").split(";") if t))
            out.add(
                VM_INFO,
                1,
                tags=tags,
                template=str(int(res.get("template") or 0)),
                pool=str(res.get("pool") or ""),
                **labels,
            )
            running = status == "running"
            out.add(VM_STATUS, 1 if running else 0, **labels)
            out.add(VM_UPTIME, res.get("uptime") or 0, **labels)
            out.add(VM_CPU, float(res.get("cpu") or 0) * 100, **labels)
            out.add(VM_CPUS, res.get("maxcpu"), **labels)
            out.add(VM_MEM_TOTAL, res.get("maxmem"), **labels)
            out.add(VM_MEM_USED, res.get("mem") or 0, **labels)
            out.add(VM_DISK_TOTAL, res.get("maxdisk"), **labels)
            out.add(VM_DISK_USED, res.get("disk") or 0, **labels)
            out.add(VM_DISK_READ, res.get("diskread") or 0, **labels)
            out.add(VM_DISK_WRITE, res.get("diskwrite") or 0, **labels)
            out.add(VM_NET_RX, res.get("netin") or 0, **labels)
            out.add(VM_NET_TX, res.get("netout") or 0, **labels)
        for (kind, status), count in counts.items():
            out.add(VM_COUNT, count, type=kind, status=status)

    @staticmethod
    def _storage(out: Batch, resources: list[dict[str, Any]]) -> None:
        for res in resources:
            if res.get("type") != "storage" or not res.get("storage"):
                continue
            labels = {"storage": str(res["storage"]), "type": str(res.get("plugintype") or "")}
            out.add(
                STORAGE_INFO,
                1,
                shared=str(int(res.get("shared") or 0)),
                content=str(res.get("content") or ""),
                **labels,
            )
            active = res.get("status") == "available"
            out.add(STORAGE_ACTIVE, 1 if active else 0, **labels)
            if not active:
                continue
            total = float(res.get("maxdisk") or 0)
            used = float(res.get("disk") or 0)
            out.add(STORAGE_TOTAL, total, **labels)
            out.add(STORAGE_USED, used, **labels)
            out.add(STORAGE_AVAIL, max(0.0, total - used), **labels)

    @staticmethod
    def _replication(out: Batch, jobs: list[dict[str, Any]]) -> None:
        for job in jobs:
            if not job.get("id"):
                continue
            labels = {
                "job": str(job["id"]),
                "guest": str(job.get("guest") or ""),
                "target": str(job.get("target") or ""),
            }
            out.add(REPL_LAST_SYNC, job.get("last_sync") or 0, **labels)
            out.add(REPL_DURATION, job.get("duration"), **labels)
            out.add(REPL_FAILURES, job.get("fail_count") or 0, **labels)
