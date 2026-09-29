"""Linux software RAID (md) state from /proc/mdstat."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from ..metrics import Batch, MetricGroup
from .base import Collector, read_text

M = MetricGroup("mdadm")
STATE = M.gauge("node_md_state", "Array state (one series per state).", "device", "state")
DISKS = M.gauge("node_md_disks", "Member devices of the array.", "device")
DISKS_ACTIVE = M.gauge("node_md_disks_active", "Active members (not failed or spare).", "device")
DISKS_FAILED = M.gauge("node_md_disks_failed", "Failed members.", "device")
DISKS_SPARE = M.gauge("node_md_disks_spare", "Spare members.", "device")
DISKS_REQUIRED = M.gauge(
    "node_md_disks_required", "Members the array is configured with.", "device"
)
DEGRADED = M.gauge("node_md_degraded", "Missing members (required minus in sync).", "device")
BLOCKS = M.gauge("node_md_blocks_total", "Array size in 1 KiB blocks.", "device")
BLOCKS_SYNCED = M.gauge("node_md_blocks_synced", "Blocks in sync (1 KiB).", "device")
SYNC_PERCENT = M.gauge(
    "node_md_sync_completed_percent", "Progress of a running resync/recovery/check.", "device"
)
SYNC_SPEED = M.gauge(
    "node_md_sync_speed_bytes_per_second", "Speed of a running resync/recovery/check.", "device"
)

STATES = ("active", "inactive", "recovering", "resync", "check", "reshape")
# Members carry zero or more flags, e.g. "sdb1[1](W)(F)" for a failed write-mostly disk.
_MEMBER_RE = re.compile(r"^(\S+?)\[\d+\]((?:\(\w\))*)$")
_SYNC_RE = re.compile(r"(recovery|resync|reshape|check|repair)\s*=\s*([\d.]+)%\s*\((\d+)/(\d+)\)")


@dataclass
class MdArray:
    name: str
    active: bool
    members: int = 0
    failed: int = 0
    spare: int = 0
    blocks: int | None = None
    required: int | None = None
    in_sync: int | None = None
    action: str | None = None
    percent: float | None = None
    synced: int | None = None
    speed_kib: int | None = None


def parse_mdstat(text: str) -> list[MdArray]:
    arrays: list[MdArray] = []
    current: MdArray | None = None
    for line in text.splitlines():
        header = re.match(r"^(md\S*)\s*:\s*(\S+)(.*)$", line)
        if header:
            current = MdArray(header.group(1), header.group(2) == "active")
            arrays.append(current)
            for token in header.group(3).split():
                member = _MEMBER_RE.match(token)
                if not member:
                    continue
                current.members += 1
                if "(F)" in member.group(2):
                    current.failed += 1
                elif "(S)" in member.group(2):
                    current.spare += 1
            continue
        if not line.startswith((" ", "\t")):
            current = None  # blank line or "unused devices:" ends the array
            continue
        if current is None:
            continue
        blocks = re.search(r"(\d+) blocks", line)
        if blocks:
            current.blocks = int(blocks.group(1))
        counts = re.search(r"\[(\d+)/(\d+)\]", line)
        if counts:
            current.required, current.in_sync = int(counts.group(1)), int(counts.group(2))
        sync = _SYNC_RE.search(line)
        if sync:
            current.action = sync.group(1)
            current.percent = float(sync.group(2))
            current.synced = int(sync.group(3))
            speed = re.search(r"speed=(\d+)K/sec", line)
            current.speed_kib = int(speed.group(1)) if speed else None
        elif re.search(r"(resync|recovery)\s*=\s*(DELAYED|PENDING)", line):
            current.action = "resync"
            current.synced = 0  # not started yet; like node_exporter
    return arrays


class MdadmCollector(Collector):
    name = "mdadm"
    description = "Software RAID array state, members and rebuild progress"

    def detect(self) -> bool:
        return os.path.exists(self.proc_path("mdstat"))

    def collect(self, out: Batch) -> None:
        text = read_text(self.proc_path("mdstat"))
        if text is None:
            raise RuntimeError("cannot read /proc/mdstat")
        for array in parse_mdstat(text):
            device = array.name
            if not array.active:
                state = "inactive"
            elif array.action == "recovery":
                state = "recovering"
            elif array.action in ("check", "repair"):
                state = "check"
            elif array.action in ("resync", "reshape"):
                state = array.action
            else:
                state = "active"
            for candidate in STATES:
                out.add(STATE, 1 if candidate == state else 0, device=device, state=candidate)
            out.add(DISKS, array.members, device=device)
            out.add(DISKS_FAILED, array.failed, device=device)
            out.add(DISKS_SPARE, array.spare, device=device)
            out.add(DISKS_ACTIVE, array.members - array.failed - array.spare, device=device)
            out.add(DISKS_REQUIRED, array.required, device=device)
            if array.required is not None and array.in_sync is not None:
                out.add(DEGRADED, array.required - array.in_sync, device=device)
            out.add(BLOCKS, array.blocks, device=device)
            synced = array.synced if array.synced is not None else array.blocks
            out.add(BLOCKS_SYNCED, synced if array.active else None, device=device)
            out.add(SYNC_PERCENT, array.percent, device=device)
            if array.speed_kib is not None:
                out.add(SYNC_SPEED, array.speed_kib * 1024, device=device)
