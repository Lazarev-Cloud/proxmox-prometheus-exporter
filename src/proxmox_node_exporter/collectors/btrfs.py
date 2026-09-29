"""Btrfs space allocation and device error counters from sysfs."""

from __future__ import annotations

import os
import re

from ..metrics import Batch, MetricGroup
from .base import Collector, list_dir, read_int, read_text

M = MetricGroup("btrfs")
INFO = M.gauge("node_btrfs_info", "Btrfs filesystem; the value is always 1.", "uuid", "label")
ALLOCATED = M.gauge(
    "node_btrfs_allocation_bytes", "Space allocated to chunks by type.", "uuid", "label", "type"
)
USED = M.gauge(
    "node_btrfs_used_bytes", "Space used inside chunks by type.", "uuid", "label", "type"
)
DEVICE_SIZE = M.gauge(
    "node_btrfs_device_size_bytes", "Size of a member device.", "uuid", "label", "device"
)
DEVICE_ERRORS = M.counter(
    "node_btrfs_device_errors_total",
    "Device error counters (write, read, flush, corruption, generation).",
    "uuid",
    "label",
    "devid",
    "type",
)
DEVICE_MISSING = M.gauge(
    "node_btrfs_device_missing", "Whether a member device is missing.", "uuid", "label", "devid"
)

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class BtrfsCollector(Collector):
    name = "btrfs"
    description = "Btrfs chunk allocation and per-device error counters"

    def detect(self) -> bool:
        return os.path.isdir(self.sys_path("fs", "btrfs"))

    def collect(self, out: Batch) -> None:
        base = self.sys_path("fs", "btrfs")
        for uuid in list_dir(base):
            if not _UUID_RE.match(uuid):
                continue
            path = os.path.join(base, uuid)
            label = read_text(os.path.join(path, "label")) or ""
            fs = {"uuid": uuid, "label": label}
            out.add(INFO, 1, **fs)
            for kind in ("data", "metadata", "system"):
                alloc = os.path.join(path, "allocation", kind)
                out.add(ALLOCATED, read_int(os.path.join(alloc, "total_bytes")), type=kind, **fs)
                out.add(USED, read_int(os.path.join(alloc, "bytes_used")), type=kind, **fs)
            for device in list_dir(os.path.join(path, "devices")):
                sectors = read_int(os.path.join(path, "devices", device, "size"))
                if sectors is not None:
                    out.add(DEVICE_SIZE, sectors * 512, device=device, **fs)
            for devid in list_dir(os.path.join(path, "devinfo")):
                info = os.path.join(path, "devinfo", devid)
                out.add(DEVICE_MISSING, read_int(os.path.join(info, "missing")), devid=devid, **fs)
                for line in (read_text(os.path.join(info, "error_stats")) or "").splitlines():
                    name, _, value = line.partition(" ")
                    if name.endswith("_errs") and value.strip().isdigit():
                        out.add(
                            DEVICE_ERRORS, int(value), devid=devid, type=name[: -len("_errs")], **fs
                        )
