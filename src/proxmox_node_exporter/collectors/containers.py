"""Docker and Podman containers running directly on the host."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
from typing import Any

from ..metrics import Batch, MetricGroup
from ..runner import CommandError
from .base import Collector

log = logging.getLogger(__name__)

M = MetricGroup("containers")
_C = ("name", "id", "runtime")
COUNT = M.gauge("node_container_count", "Containers by state.", "runtime", "state")
RUNNING = M.gauge("node_container_running", "Whether the container is running.", *_C)
CPU = M.gauge("node_container_cpu_usage_percent", "Container CPU usage.", *_C)
MEMORY = M.gauge("node_container_memory_usage_bytes", "Container memory usage.", *_C)
MEMORY_LIMIT = M.gauge("node_container_memory_limit_bytes", "Container memory limit.", *_C)
NET_RX = M.counter(
    "node_container_network_receive_bytes_total", "Bytes received by the container.", *_C
)
NET_TX = M.counter(
    "node_container_network_transmit_bytes_total", "Bytes sent by the container.", *_C
)

_UNITS = {
    "b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12,
    "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4,
}  # fmt: skip
_SIZE_RE = re.compile(r"^([\d.]+)\s*([A-Za-z]*)$")


def parse_size(text: str) -> float | None:
    match = _SIZE_RE.match(text.strip())
    if not match:
        return None
    factor = _UNITS.get((match.group(2) or "b").lower())
    return None if factor is None else float(match.group(1)) * factor


def parse_pair(text: str | None) -> tuple[float | None, float | None]:
    """'1.2MiB / 7.6GiB' -> (bytes, bytes)."""
    left, sep, right = (text or "").partition("/")
    if not sep:
        return None, None
    return parse_size(left), parse_size(right)


def _get(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in record:
            return record[key]
    return None


def _json_records(text: str) -> list[dict[str, Any]]:
    text = text.strip()
    if not text:
        return []
    if text.startswith("["):
        data = json.loads(text)
        return [r for r in data if isinstance(r, dict)]
    return [json.loads(line) for line in text.splitlines() if line.strip()]


class ContainersCollector(Collector):
    name = "containers"
    description = "Docker/Podman container states, CPU, memory and network"
    default_interval = 30.0

    DOCKER_SOCKET = "/var/run/docker.sock"

    def runtimes(self) -> list[str]:
        found = []
        if self.has_command("docker") and os.path.exists(self.DOCKER_SOCKET):
            found.append("docker")
        if self.has_command("podman"):
            found.append("podman")
        return found

    def detect(self) -> bool:
        return self.ctx.is_root and bool(self.runtimes())

    def collect(self, out: Batch) -> None:
        runtimes = self.runtimes()
        failures = 0
        for runtime in runtimes:
            try:
                self._runtime(out, runtime)
            except (CommandError, ValueError) as exc:
                log.debug("%s: %s", runtime, exc)
                failures += 1
        if runtimes and failures == len(runtimes):
            raise RuntimeError("no container runtime responded")

    def _runtime(self, out: Batch, runtime: str) -> None:
        fmt = ("--format", "json") if runtime == "podman" else ("--format", "{{json .}}")
        listing = _json_records(self.run(runtime, "ps", "-a", "--no-trunc", *fmt).stdout)
        states: dict[str, int] = {"running": 0, "exited": 0}
        names: dict[str, str] = {}
        for record in listing:
            cid = str(_get(record, "ID", "Id", "id") or "")[:12]
            name = _name(_get(record, "Names", "names", "Name"))
            state = str(_get(record, "State", "state") or "unknown").lower()
            states[state] = states.get(state, 0) + 1
            names[cid] = name
            labels = {"name": name, "id": cid, "runtime": runtime}
            out.add(RUNNING, 1 if state == "running" else 0, **labels)
        for state, count in states.items():
            out.add(COUNT, count, runtime=runtime, state=state)
        if states["running"] == 0:
            return
        stats = self.run(runtime, "stats", "--no-stream", *fmt, timeout=30.0)
        for record in _json_records(stats.stdout):
            cid = str(_get(record, "ID", "Id", "id", "Container") or "")[:12]
            name = names.get(cid) or _name(_get(record, "Name", "name"))
            labels = {"name": name, "id": cid, "runtime": runtime}
            cpu = str(_get(record, "CPUPerc", "cpu_percent", "CPU") or "").rstrip("%")
            with contextlib.suppress(ValueError):
                out.add(CPU, float(cpu), **labels)
            used, limit = parse_pair(_get(record, "MemUsage", "mem_usage"))
            out.add(MEMORY, used, **labels)
            out.add(MEMORY_LIMIT, limit, **labels)
            rx, tx = parse_pair(_get(record, "NetIO", "net_io"))
            out.add(NET_RX, rx, **labels)
            out.add(NET_TX, tx, **labels)


def _name(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else ""
    return str(value or "").split(",")[0].lstrip("/")
