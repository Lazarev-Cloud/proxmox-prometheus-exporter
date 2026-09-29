"""ZFS ARC statistics, pool health/capacity/scrubs/errors and dataset usage."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field

from ..metrics import Batch, MetricGroup
from .base import Collector, read_text, to_float

M = MetricGroup("zfs")
_ARC = {
    "size": M.gauge("node_zfs_arc_size_bytes", "ARC size."),
    "c": M.gauge("node_zfs_arc_c_bytes", "ARC target size."),
    "c_min": M.gauge("node_zfs_arc_c_min_bytes", "ARC minimum size."),
    "c_max": M.gauge("node_zfs_arc_c_max_bytes", "ARC maximum size."),
    "mru_size": M.gauge("node_zfs_arc_mru_size_bytes", "ARC most-recently-used list size."),
    "mfu_size": M.gauge("node_zfs_arc_mfu_size_bytes", "ARC most-frequently-used list size."),
    "hits": M.counter("node_zfs_arc_hits_total", "ARC hits."),
    "misses": M.counter("node_zfs_arc_misses_total", "ARC misses."),
    "l2_hits": M.counter("node_zfs_l2arc_hits_total", "L2ARC hits."),
    "l2_misses": M.counter("node_zfs_l2arc_misses_total", "L2ARC misses."),
    "l2_size": M.gauge("node_zfs_l2arc_size_bytes", "L2ARC size."),
}
ARC_HIT_RATIO = M.gauge("node_zfs_arc_hit_ratio", "ARC hit ratio since boot (0-1).")

POOL_HEALTH = M.gauge(
    "node_zfs_zpool_health",
    "Pool health: 0 online, 1 degraded, 2 faulted, 3 offline, 4 unavail, 5 removed, 6 suspended.",
    "pool",
)
POOL_STATE = M.gauge("node_zfs_zpool_state", "Pool state (one series per state).", "pool", "state")
POOL_SIZE = M.gauge("node_zfs_zpool_size_bytes", "Pool size.", "pool")
POOL_ALLOC = M.gauge("node_zfs_zpool_allocated_bytes", "Pool space allocated.", "pool")
POOL_FREE = M.gauge("node_zfs_zpool_free_bytes", "Pool space free.", "pool")
POOL_FRAG = M.gauge(
    "node_zfs_zpool_fragmentation_percent", "Pool free-space fragmentation.", "pool"
)
POOL_DEDUP = M.gauge("node_zfs_zpool_deduplication_ratio", "Pool deduplication ratio.", "pool")
SCRUB_STATE = M.gauge(
    "node_zfs_zpool_scrub_state",
    "Current or last scan (scrub/resilver) state (one series per state).",
    "pool",
    "state",
)
SCAN_PROGRESS = M.gauge(
    "node_zfs_zpool_scan_progress_percent", "Progress of a running scrub or resilver.", "pool"
)
SCAN_ERRORS = M.gauge(
    "node_zfs_zpool_scan_errors", "Errors found by the last completed scan.", "pool"
)
LAST_SCRUB = M.gauge(
    "node_zfs_zpool_last_scrub_timestamp_seconds",
    "Completion time of the last finished scrub.",
    "pool",
)
POOL_ERRORS = M.counter(
    "node_zfs_zpool_errors_total",
    "Read/write/checksum errors, summed over all vdevs of the pool.",
    "pool",
    "type",
)
VDEV_ERRORS = M.counter(
    "node_zfs_zpool_vdev_errors_total",
    "Read/write/checksum errors of a vdev.",
    "pool",
    "vdev",
    "type",
)
VDEV_ONLINE = M.gauge("node_zfs_zpool_vdev_online", "Whether a vdev is ONLINE.", "pool", "vdev")
DATA_ERRORS = M.gauge(
    "node_zfs_zpool_data_errors", "Files with permanent (unrecoverable) data errors.", "pool"
)
DS_USED = M.gauge("node_zfs_dataset_used_bytes", "Dataset space used.", "dataset", "type")
DS_AVAIL = M.gauge(
    "node_zfs_dataset_available_bytes", "Dataset space available.", "dataset", "type"
)
DS_REFER = M.gauge(
    "node_zfs_dataset_referenced_bytes", "Dataset space referenced.", "dataset", "type"
)

POOL_STATES = ("ONLINE", "DEGRADED", "FAULTED", "OFFLINE", "UNAVAIL", "REMOVED", "SUSPENDED")
SCAN_STATES = (
    "none",
    "scrub_in_progress",
    "scrub_paused",
    "scrub_finished",
    "scrub_canceled",
    "resilver_in_progress",
    "resilver_finished",
)
_ERROR_TYPES = ("read", "write", "checksum")
_KEY_RE = re.compile(r"^\s*(pool|state|status|action|see|scan|config|errors|remove|checkpoint):"
                     r"\s?(.*)$")  # fmt: skip


@dataclass
class PoolStatus:
    name: str
    state: str = ""
    scan: str = ""
    vdevs: list[tuple[str, str, tuple[int, int, int]]] = field(default_factory=list)
    data_errors: int | None = None
    listing_files: bool = False
    listed_files: int = 0


def _data_errors(value: str) -> int | None:
    """Number of files with permanent errors from the ``errors:`` line.

    None (not exported) when the line can't be interpreted, e.g. "List of
    errors unavailable" for unprivileged users, rather than a false 0.
    """
    if value.startswith("No known data errors"):
        return 0
    found = re.match(r"(\d+) data errors?", value)
    if found:
        return int(found.group(1))
    if value.startswith("Permanent errors have been detected"):
        return 1  # at least one; the file list that follows is counted
    return None


def parse_zpool_status(text: str) -> list[PoolStatus]:
    pools: list[PoolStatus] = []
    current: PoolStatus | None = None
    key = ""
    for raw in text.splitlines():
        match = _KEY_RE.match(raw)
        if match:
            key, value = match.group(1), match.group(2).strip()
            if key == "pool":
                current = PoolStatus(value)
                pools.append(current)
            elif current is None:
                continue
            elif key == "state":
                current.state = value
            elif key == "scan":
                current.scan = value
            elif key == "errors":
                current.data_errors = _data_errors(value)
                current.listing_files = value.startswith("Permanent errors")
            continue
        if current is None:
            continue
        line = raw.strip()
        if key == "scan" and line:
            current.scan += " " + line
        elif key == "errors" and line and current.listing_files:
            # With -v, each affected file follows on its own line.
            current.listed_files += 1
            current.data_errors = max(current.data_errors or 0, current.listed_files)
        elif key == "config":
            fields = line.split()
            if len(fields) >= 5 and fields[0] != "NAME" and all(f.isdigit() for f in fields[2:5]):
                errors = (int(fields[2]), int(fields[3]), int(fields[4]))
                current.vdevs.append((fields[0], fields[1], errors))
    return pools


def parse_scan(scan: str) -> tuple[str, float | None, float | None, int | None]:
    """Return (state, progress_percent, finished_timestamp, errors)."""
    scan = " ".join(scan.split())
    # Sequential resilvers (dRAID rebuilds, attach/replace -s) name the top-level vdev:
    # "resilver (draid2:4d:9c:1s-0) in progress since ...".
    scan = re.sub(r"^(resilver(?:ed)?) \([^)]*\)", r"\1", scan)
    progress = None
    done = re.search(r"([\d.]+)% done", scan)
    if done:
        progress = float(done.group(1))
    errors_match = re.search(r"with (\d+) errors", scan)
    errors = int(errors_match.group(1)) if errors_match else None
    finished = None
    when = re.search(r" on (\w{3} \w{3} \d+ \d+:\d+:\d+ \d{4})", scan)
    if when:
        try:
            finished = float(time.mktime(time.strptime(when.group(1), "%a %b %d %H:%M:%S %Y")))
        except ValueError:
            finished = None
    if scan.startswith("scrub in progress"):
        return "scrub_in_progress", progress, None, None
    if scan.startswith("scrub paused"):
        return "scrub_paused", progress, None, None
    if scan.startswith("scrub repaired"):
        return "scrub_finished", None, finished, errors
    if scan.startswith("scrub canceled"):
        return "scrub_canceled", None, None, None
    if scan.startswith("resilver in progress"):
        return "resilver_in_progress", progress, None, None
    if scan.startswith("resilvered"):
        return "resilver_finished", None, None, errors
    return "none", None, None, None


class ZfsCollector(Collector):
    name = "zfs"
    description = "ZFS ARC, pool health/capacity/scrubs/errors and dataset usage"
    default_interval = 30.0

    def detect(self) -> bool:
        return os.path.exists(self.proc_path("spl", "kstat", "zfs")) or self.has_command("zpool")

    def collect(self, out: Batch) -> None:
        self._arcstats(out)
        if not self.has_command("zpool"):
            return
        self._zpool_list(out)
        self._zpool_status(out)
        if self.settings.zfs_datasets and self.has_command("zfs"):
            self._datasets(out)

    def _arcstats(self, out: Batch) -> None:
        text = read_text(self.proc_path("spl", "kstat", "zfs", "arcstats"))
        if not text:
            return
        stats: dict[str, int] = {}
        for line in text.splitlines()[2:]:
            fields = line.split()
            if len(fields) == 3 and fields[2].lstrip("-").isdigit():
                stats[fields[0]] = int(fields[2])
        for key, spec in _ARC.items():
            if key in stats:
                out.add(spec, stats[key])
        lookups = stats.get("hits", 0) + stats.get("misses", 0)
        if lookups:
            out.add(ARC_HIT_RATIO, stats["hits"] / lookups)

    def _zpool_list(self, out: Batch) -> None:
        result = self.run(
            "zpool", "list", "-Hp", "-o", "name,size,allocated,free,fragmentation,dedupratio,health"
        )
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) != 7:
                continue
            pool, size, alloc, free, frag, dedup, health = fields
            out.add(POOL_SIZE, to_float(size), pool=pool)
            out.add(POOL_ALLOC, to_float(alloc), pool=pool)
            out.add(POOL_FREE, to_float(free), pool=pool)
            out.add(POOL_FRAG, to_float(frag.rstrip("%")), pool=pool)
            out.add(POOL_DEDUP, to_float(dedup.rstrip("x")), pool=pool)
            self._health(out, pool, health)

    @staticmethod
    def _health(out: Batch, pool: str, health: str) -> None:
        health = health.upper()
        if health in POOL_STATES:
            out.add(POOL_HEALTH, POOL_STATES.index(health), pool=pool)
        for state in POOL_STATES:
            out.add(POOL_STATE, 1 if state == health else 0, pool=pool, state=state.lower())

    def _zpool_status(self, out: Batch) -> None:
        result = self.run("zpool", "status", "-p", timeout=30.0)
        for pool in parse_zpool_status(result.stdout):
            if pool.state:
                self._health(out, pool.name, pool.state)
            state, progress, finished, errors = parse_scan(pool.scan)
            for candidate in SCAN_STATES:
                out.add(
                    SCRUB_STATE, 1 if candidate == state else 0, pool=pool.name, state=candidate
                )
            out.add(SCAN_PROGRESS, progress, pool=pool.name)
            out.add(LAST_SCRUB, finished, pool=pool.name)
            out.add(SCAN_ERRORS, errors, pool=pool.name)
            out.add(DATA_ERRORS, pool.data_errors, pool=pool.name)
            totals = [0, 0, 0]
            for vdev, vdev_state, counts in pool.vdevs:
                out.add(VDEV_ONLINE, 1 if vdev_state == "ONLINE" else 0, pool=pool.name, vdev=vdev)
                for i, kind in enumerate(_ERROR_TYPES):
                    totals[i] += counts[i]
                    out.add(VDEV_ERRORS, counts[i], pool=pool.name, vdev=vdev, type=kind)
            for i, kind in enumerate(_ERROR_TYPES):
                out.add(POOL_ERRORS, totals[i], pool=pool.name, type=kind)

    def _datasets(self, out: Batch) -> None:
        result = self.run(
            "zfs", "list", "-Hp", "-t", "filesystem,volume", "-o", "name,used,avail,refer,type",
            timeout=30.0,
        )  # fmt: skip
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) != 5:
                continue
            dataset, used, avail, refer, kind = fields
            out.add(DS_USED, to_float(used), dataset=dataset, type=kind)
            out.add(DS_AVAIL, to_float(avail), dataset=dataset, type=kind)
            out.add(DS_REFER, to_float(refer), dataset=dataset, type=kind)
