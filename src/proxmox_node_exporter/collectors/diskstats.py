"""Block device I/O statistics from /proc/diskstats."""

from __future__ import annotations

import os
import re

from ..metrics import Batch, MetricGroup, MetricSpec
from .base import Collector, Context, read_text

M = MetricGroup("diskstats")
SECTOR = 512  # /proc/diskstats always counts 512-byte sectors

# (field index after the device name, metric, scale)
_FIELDS: tuple[tuple[int, MetricSpec, float], ...] = (
    (0, M.counter("node_disk_reads_completed_total", "Reads completed.", "device"), 1),
    (1, M.counter("node_disk_reads_merged_total", "Reads merged.", "device"), 1),
    (2, M.counter("node_disk_read_bytes_total", "Bytes read.", "device"), SECTOR),
    (3, M.counter("node_disk_read_time_seconds_total", "Time spent reading.", "device"), 1e-3),
    (4, M.counter("node_disk_writes_completed_total", "Writes completed.", "device"), 1),
    (5, M.counter("node_disk_writes_merged_total", "Writes merged.", "device"), 1),
    (6, M.counter("node_disk_written_bytes_total", "Bytes written.", "device"), SECTOR),
    (7, M.counter("node_disk_write_time_seconds_total", "Time spent writing.", "device"), 1e-3),
    (8, M.gauge("node_disk_io_now", "I/Os currently in progress.", "device"), 1),
    (9, M.counter("node_disk_io_time_seconds_total", "Time spent doing I/O.", "device"), 1e-3),
    (
        10,
        M.counter("node_disk_io_time_weighted_seconds_total", "Weighted time doing I/O.", "device"),
        1e-3,
    ),
    (11, M.counter("node_disk_discards_completed_total", "Discards completed.", "device"), 1),
    (12, M.counter("node_disk_discards_merged_total", "Discards merged.", "device"), 1),
    (13, M.counter("node_disk_discarded_sectors_total", "Sectors discarded.", "device"), 1),
    (
        14,
        M.counter("node_disk_discard_time_seconds_total", "Time spent discarding.", "device"),
        1e-3,
    ),
    (15, M.counter("node_disk_flush_requests_total", "Flush requests completed.", "device"), 1),
    (
        16,
        M.counter("node_disk_flush_requests_time_seconds_total", "Time spent flushing.", "device"),
        1e-3,
    ),
)
DM_INFO = M.gauge(
    "node_disk_device_mapper_info",
    "Device-mapper name of a dm-N device (LVM volume); the value is always 1.",
    "device",
    "name",
)


class DiskstatsCollector(Collector):
    name = "diskstats"
    description = "Disk I/O: throughput, IOPS, latency, queue, utilisation"

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        self._exclude = re.compile(self.settings.diskstats_device_exclude)

    def detect(self) -> bool:
        return os.path.exists(self.proc_path("diskstats"))

    def collect(self, out: Batch) -> None:
        text = read_text(self.proc_path("diskstats"))
        if text is None:
            raise RuntimeError("cannot read /proc/diskstats")
        for line in text.splitlines():
            parts = line.split()
            if len(parts) < 14:
                continue
            device = parts[2]
            if self._exclude.search(device):
                continue
            values = parts[3:]
            for index, spec, scale in _FIELDS:
                if index < len(values):
                    out.add(spec, int(values[index]) * scale, device=device)
            if device.startswith("dm-"):
                name = read_text(self.sys_path("block", device, "dm", "name"))
                if name:
                    out.add(DM_INFO, 1, device=device, name=name)
