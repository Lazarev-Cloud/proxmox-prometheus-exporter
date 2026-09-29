from __future__ import annotations

import errno
import os
import threading
from pathlib import Path
from typing import Callable

import pytest

from conftest import collect, fixture, value, write_tree
from proxmox_node_exporter.collectors import filesystem
from proxmox_node_exporter.collectors.base import Context
from proxmox_node_exporter.collectors.filesystem import FilesystemCollector, Mount, parse_mountinfo


def statvfs_result(
    bsize: int, frsize: int, blocks: int, bfree: int, bavail: int, files: int, ffree: int
) -> os.statvfs_result:
    return os.statvfs_result((bsize, frsize, blocks, bfree, bavail, files, ffree, ffree, 0, 255))


ROOT = statvfs_result(131072, 131072, 7449843, 7000000, 6990000, 14567890, 14000000)
EFI = statvfs_result(4096, 4096, 130812, 128000, 128000, 0, 0)
NFS = statvfs_result(1048576, 4096, 2441406250, 1220703125, 1210000000, 312500000, 300000000)
STATS = {
    "/": ROOT,
    "/rpool": statvfs_result(131072, 131072, 7000000, 6999000, 6999000, 13999000, 13998990),
    "/rpool/data": statvfs_result(131072, 131072, 7000000, 6999000, 6999000, 13999000, 13998000),
    "/boot/efi": EFI,
    "/etc/pve": statvfs_result(4096, 4096, 7630, 7512, 7512, 262144, 261948),
    "/mnt/pve/nas backup": NFS,
    "/mnt/usb": statvfs_result(4096, 4096, 244190208, 200000000, 187000000, 61054976, 61000000),
    "/mnt/data": statvfs_result(131072, 131072, 1000000, 500000, 500000, 900000, 800000),
}


def fake_statvfs(stats: dict[str, os.statvfs_result]) -> Callable[[str], os.statvfs_result]:
    def statvfs(path: str) -> os.statvfs_result:
        try:
            return stats[path]
        except KeyError:
            raise OSError(errno.EHOSTDOWN, os.strerror(errno.EHOSTDOWN), path) from None

    return statvfs


def labels(device: str, mountpoint: str, fstype: str) -> dict[str, str]:
    return {"device": device, "mountpoint": mountpoint, "fstype": fstype}


def mountpoints(samples: dict[str, dict[frozenset[tuple[str, str]], float]]) -> set[str]:
    return {dict(key)["mountpoint"] for key in samples["node_filesystem_readonly"]}


# -- parsing -------------------------------------------------------------------


def test_parse_mountinfo_fields_and_escapes() -> None:
    text = (
        # the example from proc(5): one optional field
        "36 35 98:0 /mnt1 /mnt/parent rw,noatime master:1 - ext3 /dev/root rw,errors=continue\n"
        # two optional fields
        "81 80 0:53 / /mnt/data rw,noatime shared:45 master:44 - zfs tank/data rw,xattr,noacl\n"
        # no optional fields, read-only per-mount options
        "75 28 8:17 / /mnt/usb ro,nosuid,nodev,relatime - ext4 /dev/sdb1 ro\n"
        # space, tab and backslash are octal-escaped
        "92 28 0:57 /a\\040b /mnt/x\\040y\\011z\\134w rw,relatime shared:62 - cifs "
        "//nas/My\\040Share rw\n"
        # garbage lines are skipped
        "not a mountinfo line\n"
        "93 28 0:58 / /mnt/short rw - \n"
    )
    assert parse_mountinfo(text) == [
        Mount("/dev/root", "/mnt/parent", "ext3", "/mnt1", False),
        Mount("tank/data", "/mnt/data", "zfs", "/", False),
        Mount("/dev/sdb1", "/mnt/usb", "ext4", "/", True),
        Mount("//nas/My Share", "/mnt/x y\tz\\w", "cifs", "/a b", False),
    ]


def test_superblock_options_do_not_make_a_mount_readonly() -> None:
    # A read-write bind of a filesystem whose superblock is mounted "ro"
    # elsewhere still reports the per-mount flag.
    (mount,) = parse_mountinfo("40 28 8:17 / /srv rw,relatime - ext4 /dev/sdb1 ro\n")
    assert not mount.readonly


# -- detect / collect ------------------------------------------------------------


def test_detect(tmp_path: Path, make_ctx: Callable[..., Context]) -> None:
    ctx = make_ctx()
    assert not FilesystemCollector(ctx).detect()
    write_tree(tmp_path, {"proc/self/mountinfo": fixture("filesystem/mountinfo-pve.txt")})
    assert FilesystemCollector(ctx).detect()


def test_no_mountinfo_raises(make_ctx: Callable[..., Context]) -> None:
    with pytest.raises(RuntimeError, match="mountinfo"):
        collect(FilesystemCollector(make_ctx()))


@pytest.fixture
def pve_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = fixture("filesystem/mountinfo-pve.txt")
    write_tree(tmp_path, {"proc/1/mountinfo": text, "proc/self/mountinfo": text})
    monkeypatch.setattr(filesystem.os, "statvfs", fake_statvfs(STATS))


@pytest.mark.usefixtures("pve_host")
def test_default_exclusions(make_ctx: Callable[..., Context]) -> None:
    samples = collect(FilesystemCollector(make_ctx()))
    assert mountpoints(samples) == {
        "/",
        "/rpool",
        "/rpool/data",
        "/boot/efi",
        "/etc/pve",
        "/mnt/pve/nas backup",
        "/mnt/usb",
        "/mnt/data",
        "/mnt/pve/cifs",
    }


@pytest.mark.usefixtures("pve_host")
def test_capacity_and_inodes(make_ctx: Callable[..., Context]) -> None:
    samples = collect(FilesystemCollector(make_ctx()))
    root = labels("rpool/ROOT/pve-1", "/", "zfs")
    assert value(samples, "node_filesystem_size_bytes", **root) == 7449843 * 131072
    assert value(samples, "node_filesystem_free_bytes", **root) == 7000000 * 131072
    assert value(samples, "node_filesystem_avail_bytes", **root) == 6990000 * 131072
    assert value(samples, "node_filesystem_files", **root) == 14567890
    assert value(samples, "node_filesystem_files_free", **root) == 14000000
    assert value(samples, "node_filesystem_readonly", **root) == 0
    assert value(samples, "node_filesystem_device_error", **root) == 0

    efi = labels("/dev/sda2", "/boot/efi", "vfat")
    assert value(samples, "node_filesystem_size_bytes", **efi) == 130812 * 4096
    assert value(samples, "node_filesystem_files", **efi) == 0

    # octal escapes decoded in mount point and device; sizes use f_frsize
    nfs = labels("192.168.1.10:/export/pve backup", "/mnt/pve/nas backup", "nfs4")
    assert value(samples, "node_filesystem_size_bytes", **nfs) == 2441406250 * 4096
    assert value(samples, "node_filesystem_avail_bytes", **nfs) == 1210000000 * 4096

    usb = labels("/dev/sdb1", "/mnt/usb", "ext4")
    assert value(samples, "node_filesystem_readonly", **usb) == 1
    assert value(samples, "node_filesystem_device_error", **usb) == 0

    pve = labels("/dev/fuse", "/etc/pve", "fuse")
    assert value(samples, "node_filesystem_size_bytes", **pve) == 7630 * 4096


@pytest.mark.usefixtures("pve_host")
def test_statfs_error_sets_device_error(make_ctx: Callable[..., Context]) -> None:
    samples = collect(FilesystemCollector(make_ctx()))
    cifs = labels("//nas/My Share", "/mnt/pve/cifs", "cifs")
    assert value(samples, "node_filesystem_device_error", **cifs) == 1
    assert value(samples, "node_filesystem_readonly", **cifs) == 0
    for name in (
        "node_filesystem_size_bytes",
        "node_filesystem_free_bytes",
        "node_filesystem_avail_bytes",
        "node_filesystem_files",
        "node_filesystem_files_free",
    ):
        assert value(samples, name, **cifs) is None


@pytest.mark.usefixtures("pve_host")
def test_last_mount_on_a_path_wins(make_ctx: Callable[..., Context]) -> None:
    samples = collect(FilesystemCollector(make_ctx()))
    data = [
        dict(k) for k in samples["node_filesystem_readonly"] if dict(k)["mountpoint"] == "/mnt/data"
    ]
    # ext4 /dev/sdc1 is hidden underneath the ZFS dataset mounted later
    assert data == [labels("tank/data", "/mnt/data", "zfs")]
    assert value(samples, "node_filesystem_size_bytes", **data[0]) == 1000000 * 131072
    assert (
        value(samples, "node_filesystem_readonly", **labels("/dev/sdc1", "/mnt/data", "ext4"))
        is None
    )


def test_excluded_mount_on_top_hides_the_one_below(
    tmp_path: Path, make_ctx: Callable[..., Context], monkeypatch: pytest.MonkeyPatch
) -> None:
    # statfs() on /srv/cache now describes the tmpfs; reporting it with the
    # labels of the ext4 filesystem underneath would be wrong.
    write_tree(
        tmp_path,
        {
            "proc/1/mountinfo": (
                "28 1 253:1 / / rw,relatime shared:1 - ext4 /dev/mapper/pve-root rw\n"
                "80 28 8:33 / /srv/cache rw,relatime shared:44 - ext4 /dev/sdc1 rw\n"
                "95 80 0:70 / /srv/cache rw,nosuid,nodev shared:70 - tmpfs tmpfs rw,size=1g\n"
                "96 28 8:49 / /srv/db rw,relatime shared:71 - tmpfs tmpfs rw,size=1g\n"
                "97 96 8:49 / /srv/db rw,relatime shared:72 - xfs /dev/sdd1 rw\n"
            )
        },
    )
    monkeypatch.setattr(filesystem.os, "statvfs", lambda path: ROOT)
    samples = collect(FilesystemCollector(make_ctx()))
    assert list(samples["node_filesystem_readonly"]) == [
        frozenset(labels("/dev/mapper/pve-root", "/", "ext4").items()),
        frozenset(labels("/dev/sdd1", "/srv/db", "xfs").items()),
    ]


@pytest.mark.usefixtures("pve_host")
def test_custom_exclusions(make_ctx: Callable[..., Context]) -> None:
    ctx = make_ctx(
        filesystem_fs_types_exclude=r"^(nfs4|cifs|proc|sysfs|devpts|cgroup2)$",
        filesystem_mount_points_exclude=r"^/(boot|sys|proc)(/|$)",
    )
    found = mountpoints(collect(FilesystemCollector(ctx)))
    assert "/boot/efi" not in found
    assert "/mnt/pve/nas backup" not in found
    assert "/mnt/pve/cifs" not in found
    assert {"/run", "/dev/shm", "/run/user/0", "/var/lib/lxcfs", "/mnt/usb"} <= found
    assert "/var/lib/kubelet/pods/0a1b2c3d/volumes/kubernetes.io~csi/pvc-1/mount" in found


# -- which mount table ----------------------------------------------------------


def test_pid1_mount_table_is_preferred(
    tmp_path: Path, make_ctx: Callable[..., Context], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(filesystem.os, "statvfs", fake_statvfs(STATS))
    write_tree(
        tmp_path,
        {
            "proc/1/mountinfo": "28 1 0:26 / / rw,relatime shared:1 - zfs rpool/ROOT/pve-1 rw\n",
            "proc/self/mountinfo": fixture("filesystem/mountinfo-sandboxed.txt"),
        },
    )
    samples = collect(FilesystemCollector(make_ctx()))
    assert list(samples["node_filesystem_readonly"]) == [
        frozenset(labels("rpool/ROOT/pve-1", "/", "zfs").items())
    ]


def test_sandbox_bind_mounts_dropped_without_host_view(
    tmp_path: Path, make_ctx: Callable[..., Context], monkeypatch: pytest.MonkeyPatch
) -> None:
    # /proc/1/mountinfo is unreadable (e.g. ProtectProc=invisible), so the
    # service's own table with systemd's sandbox bind mounts is all we get.
    monkeypatch.setattr(filesystem.os, "statvfs", lambda path: ROOT)
    write_tree(tmp_path, {"proc/self/mountinfo": fixture("filesystem/mountinfo-sandboxed.txt")})
    samples = collect(FilesystemCollector(make_ctx()))
    # real filesystems (root "/") stay, even below sandboxed paths
    assert mountpoints(samples) == {"/", "/boot/efi", "/home", "/etc/pve", "/mnt/backup"}
    assert value(samples, "node_filesystem_readonly", **labels("/dev/sdc1", "/home", "ext4")) == 1
    assert (
        value(samples, "node_filesystem_readonly", **labels("/dev/mapper/pve-root", "/", "ext4"))
        == 0
    )


def test_bind_mounts_kept_with_host_view(
    tmp_path: Path, make_ctx: Callable[..., Context], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The same table read from PID 1: bind mounts there are the host's own.
    monkeypatch.setattr(filesystem.os, "statvfs", lambda path: ROOT)
    text = fixture("filesystem/mountinfo-sandboxed.txt")
    write_tree(tmp_path, {"proc/1/mountinfo": text, "proc/self/mountinfo": text})
    found = mountpoints(collect(FilesystemCollector(make_ctx())))
    assert found == {
        "/",
        "/boot/efi",
        "/home",
        "/etc/pve",
        "/mnt/backup",
        "/tmp",
        "/var/tmp",
        "/usr",
        "/boot",
        "/etc",
        "/root",
    }


@pytest.mark.parametrize(("host_view", "expected"), [(True, True), (False, False)])
def test_host_bind_mount_of_home(
    tmp_path: Path,
    make_ctx: Callable[..., Context],
    monkeypatch: pytest.MonkeyPatch,
    host_view: bool,
    expected: bool,
) -> None:
    monkeypatch.setattr(filesystem.os, "statvfs", lambda path: ROOT)
    text = (
        "28 1 0:26 / / rw,relatime shared:1 - zfs rpool/ROOT/pve-1 rw,xattr,noacl\n"
        "64 28 0:40 / /tank rw,noatime shared:33 - zfs tank rw,xattr,noacl\n"
        "90 28 0:40 /home /home rw,noatime shared:33 - zfs tank rw,xattr,noacl\n"
    )
    files = {"proc/self/mountinfo": text}
    if host_view:
        files["proc/1/mountinfo"] = text
    write_tree(tmp_path, files)
    found = mountpoints(collect(FilesystemCollector(make_ctx())))
    assert {"/", "/tank"} <= found
    assert ("/home" in found) is expected


def test_empty_pid1_mountinfo_falls_back_to_self(
    tmp_path: Path, make_ctx: Callable[..., Context], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(filesystem.os, "statvfs", lambda path: ROOT)
    write_tree(
        tmp_path,
        {
            "proc/1/mountinfo": "",
            "proc/self/mountinfo": fixture("filesystem/mountinfo-sandboxed.txt"),
        },
    )
    found = mountpoints(collect(FilesystemCollector(make_ctx())))
    assert "/usr" not in found
    assert "/" in found


# -- stuck mounts ---------------------------------------------------------------


def test_stuck_statfs_is_skipped_until_it_returns(
    tmp_path: Path, make_ctx: Callable[..., Context], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_tree(
        tmp_path,
        {
            "proc/1/mountinfo": (
                "28 1 253:1 / / rw,relatime shared:1 - ext4 /dev/mapper/pve-root rw\n"
                "74 28 0:48 / /mnt/pve/nfs rw,relatime shared:43 - nfs4 10.0.0.5:/export rw,hard\n"
            )
        },
    )
    release = threading.Event()
    nfs_calls: list[str] = []

    def statvfs(path: str) -> os.statvfs_result:
        if path == "/mnt/pve/nfs":
            nfs_calls.append(path)
            release.wait(30)  # a hard NFS mount whose server went away
            return NFS
        return ROOT

    monkeypatch.setattr(filesystem.os, "statvfs", statvfs)
    collector = FilesystemCollector(make_ctx(filesystem_statfs_timeout=0.05))
    nfs = labels("10.0.0.5:/export", "/mnt/pve/nfs", "nfs4")
    root = labels("/dev/mapper/pve-root", "/", "ext4")
    try:
        first = collect(collector)
        assert value(first, "node_filesystem_device_error", **nfs) == 1
        assert value(first, "node_filesystem_readonly", **nfs) == 0
        assert value(first, "node_filesystem_size_bytes", **nfs) is None
        assert value(first, "node_filesystem_device_error", **root) == 0
        assert value(first, "node_filesystem_size_bytes", **root) == 7449843 * 131072
        assert nfs_calls == ["/mnt/pve/nfs"]

        # Still stuck: reported as an error without calling statfs again.
        second = collect(collector)
        assert value(second, "node_filesystem_device_error", **nfs) == 1
        assert value(second, "node_filesystem_size_bytes", **nfs) is None
        assert value(second, "node_filesystem_device_error", **root) == 0
        assert nfs_calls == ["/mnt/pve/nfs"]
    finally:
        release.set()

    for thread in threading.enumerate():
        if thread.name == "statfs:/mnt/pve/nfs":
            thread.join(5)
            assert not thread.is_alive()

    # The stuck call returned: the mount is checked again and recovers.
    third = collect(collector)
    assert nfs_calls == ["/mnt/pve/nfs", "/mnt/pve/nfs"]
    assert value(third, "node_filesystem_device_error", **nfs) == 0
    assert value(third, "node_filesystem_size_bytes", **nfs) == 2441406250 * 4096
    assert value(third, "node_filesystem_files_free", **nfs) == 300000000


def test_failing_statfs_is_retried_every_run(
    tmp_path: Path, make_ctx: Callable[..., Context], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_tree(
        tmp_path,
        {"proc/1/mountinfo": "74 28 0:48 / /mnt/pve/nfs rw - nfs4 10.0.0.5:/export rw,soft\n"},
    )
    calls: list[str] = []

    def statvfs(path: str) -> os.statvfs_result:
        calls.append(path)
        if len(calls) == 1:
            raise OSError(errno.EIO, os.strerror(errno.EIO), path)
        return NFS

    monkeypatch.setattr(filesystem.os, "statvfs", statvfs)
    collector = FilesystemCollector(make_ctx())
    nfs = labels("10.0.0.5:/export", "/mnt/pve/nfs", "nfs4")
    assert value(collect(collector), "node_filesystem_device_error", **nfs) == 1
    assert value(collect(collector), "node_filesystem_device_error", **nfs) == 0
    assert calls == ["/mnt/pve/nfs", "/mnt/pve/nfs"]
