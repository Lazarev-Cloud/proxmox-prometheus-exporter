"""Filesystem capacity and inode usage.

statfs() on a dead NFS server or a suspended ZFS pool blocks forever, so each
call runs in a helper thread with a timeout; a mount that times out is
reported through ``node_filesystem_device_error`` and skipped until the stuck
call returns.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass

from ..metrics import Batch, MetricGroup
from .base import Collector, Context, read_text

log = logging.getLogger(__name__)

M = MetricGroup("filesystem")
_LABELS = ("device", "mountpoint", "fstype")
SIZE = M.gauge("node_filesystem_size_bytes", "Filesystem size.", *_LABELS)
FREE = M.gauge("node_filesystem_free_bytes", "Free space, including root-reserved.", *_LABELS)
AVAIL = M.gauge("node_filesystem_avail_bytes", "Space available to non-root users.", *_LABELS)
FILES = M.gauge("node_filesystem_files", "Total inodes.", *_LABELS)
FILES_FREE = M.gauge("node_filesystem_files_free", "Free inodes.", *_LABELS)
READONLY = M.gauge("node_filesystem_readonly", "Whether the filesystem is read-only.", *_LABELS)
DEVICE_ERROR = M.gauge(
    "node_filesystem_device_error",
    "Whether statfs() failed or timed out for this filesystem.",
    *_LABELS,
)

# Directories systemd's sandboxing bind-mounts into the service's own mount
# namespace; they only show up when the host's mount table is unreadable.
_SANDBOX_PATHS = re.compile(r"^/(usr|boot|efi|etc|home|root|tmp|var/tmp|run/user)(/|$)")
_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")


@dataclass(frozen=True)
class Mount:
    device: str
    mountpoint: str
    fstype: str
    root: str
    readonly: bool


def _unescape(text: str) -> str:
    return _OCTAL_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), text)


def parse_mountinfo(text: str) -> list[Mount]:
    mounts = []
    for line in text.splitlines():
        left, sep, right = line.partition(" - ")
        if not sep:
            continue
        lfields = left.split()
        rfields = right.split()
        if len(lfields) < 6 or len(rfields) < 2:
            continue
        options = lfields[5].split(",")
        mounts.append(
            Mount(
                device=_unescape(rfields[1]),
                mountpoint=_unescape(lfields[4]),
                fstype=rfields[0],
                root=_unescape(lfields[3]),
                readonly="ro" in options,
            )
        )
    return mounts


class FilesystemCollector(Collector):
    name = "filesystem"
    description = "Filesystem size, free space and inodes (with stuck-mount protection)"

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        self._stuck: dict[str, threading.Thread] = {}
        self._fs_exclude = re.compile(self.settings.filesystem_fs_types_exclude)
        self._mp_exclude = re.compile(self.settings.filesystem_mount_points_exclude)

    def detect(self) -> bool:
        return os.path.exists(self.proc_path("self", "mountinfo"))

    def mounts(self) -> list[Mount]:
        # PID 1's table is the host's view; ours may be altered by sandboxing.
        text = read_text(self.proc_path("1", "mountinfo"))
        host_view = bool(text)
        if not text:
            text = read_text(self.proc_path("self", "mountinfo"))
        if not text:
            raise RuntimeError("cannot read mountinfo")
        selected: dict[str, Mount] = {}
        for mount in parse_mountinfo(text):
            if self._mp_exclude.search(mount.mountpoint):
                continue
            if not host_view and mount.root != "/" and _SANDBOX_PATHS.match(mount.mountpoint):
                continue
            selected[mount.mountpoint] = mount  # the last mount on a path wins
        # Filter types only now: an excluded filesystem mounted on top still
        # hides the one below it, and statfs() would describe the top one.
        return [m for m in selected.values() if not self._fs_exclude.search(m.fstype)]

    def collect(self, out: Batch) -> None:
        mounts = self.mounts()
        # Forget stuck statfs() calls on paths that were unmounted since.
        current = {m.mountpoint for m in mounts}
        for path in [p for p, t in self._stuck.items() if p not in current and not t.is_alive()]:
            del self._stuck[path]
        for mount in mounts:
            labels = {
                "device": mount.device,
                "mountpoint": mount.mountpoint,
                "fstype": mount.fstype,
            }
            out.add(READONLY, 1 if mount.readonly else 0, **labels)
            st = self._statvfs(mount.mountpoint)
            out.add(DEVICE_ERROR, 0 if st is not None else 1, **labels)
            if st is None:
                continue
            out.add(SIZE, st.f_blocks * st.f_frsize, **labels)
            out.add(FREE, st.f_bfree * st.f_frsize, **labels)
            out.add(AVAIL, st.f_bavail * st.f_frsize, **labels)
            out.add(FILES, st.f_files, **labels)
            out.add(FILES_FREE, st.f_ffree, **labels)

    def _statvfs(self, path: str) -> os.statvfs_result | None:
        stuck = self._stuck.get(path)
        if stuck is not None:
            if stuck.is_alive():
                return None
            del self._stuck[path]
            log.info("statfs on %s returned again", path)

        result: list[os.statvfs_result] = []

        def target() -> None:
            try:
                result.append(os.statvfs(path))
            except OSError as exc:
                log.debug("statfs %s: %s", path, exc)

        thread = threading.Thread(target=target, name=f"statfs:{path}", daemon=True)
        thread.start()
        thread.join(self.settings.filesystem_statfs_timeout)
        if thread.is_alive():
            log.warning("statfs on %s timed out; skipping it until it returns", path)
            self._stuck[path] = thread
            return None
        return result[0] if result else None
