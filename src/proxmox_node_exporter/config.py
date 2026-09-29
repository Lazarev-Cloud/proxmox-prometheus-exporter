"""Runtime settings shared by the CLI and the collectors."""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_LISTEN_ADDRESS = ":9101"
DEFAULT_INTERVAL = 15.0

# Pseudo and virtual filesystems that carry no useful capacity information.
DEFAULT_FS_TYPES_EXCLUDE = (
    r"^(autofs|binfmt_misc|bpf|cgroup2?|configfs|debugfs|devpts|devtmpfs|efivarfs|"
    r"fusectl|fuse\.lxcfs|hugetlbfs|iso9660|mqueue|nsfs|overlay|proc|procfs|pstore|"
    r"ramfs|rpc_pipefs|securityfs|selinuxfs|squashfs|erofs|sysfs|tmpfs|tracefs)$"
)
DEFAULT_MOUNT_POINTS_EXCLUDE = (
    r"^/(dev|proc|run|sys|var/lib/docker/.+|var/lib/containers/storage/.+|"
    r"var/lib/kubelet/.+)($|/)"
)
# RAM disks, loop devices and partitions (whose I/O is already counted on the
# parent disk), including partitions inside ZFS zvols and on SD/eMMC cards.
DEFAULT_DISK_DEVICE_EXCLUDE = (
    r"^(z?ram|loop|fd|nbd|(h|s|v|xv)d[a-z]+|nvme\d+n\d+p|mmcblk\d+p|zd\d+p)\d+$"
)
# Loopback and the per-guest firewall helper interfaces (fwbr/fwln/fwpr), which
# carry the same traffic as the guest's tap/veth interface.
DEFAULT_NETWORK_DEVICE_EXCLUDE = r"^(lo|fw(br|ln)\d+i\d+|fwpr\d+p\d+)$"
DEFAULT_SYSTEMD_UNIT_INCLUDE = r".+\.service$"
DEFAULT_SYSTEMD_UNIT_EXCLUDE = (
    r"^(autovt|getty|serial-getty|user|user-runtime-dir|modprobe|systemd-fsck|"
    r"lvm2-pvscan|systemd-backlight|systemd-cryptsetup)@.*$"
)
DEFAULT_PVE_API_URL = "https://127.0.0.1:8006"


@dataclass
class Settings:
    procfs: str = "/proc"
    sysfs: str = "/sys"
    interval: float = DEFAULT_INTERVAL
    intervals: dict[str, float] = field(default_factory=dict)
    collectors: list[str] | None = None
    disabled_collectors: list[str] = field(default_factory=list)

    filesystem_fs_types_exclude: str = DEFAULT_FS_TYPES_EXCLUDE
    filesystem_mount_points_exclude: str = DEFAULT_MOUNT_POINTS_EXCLUDE
    filesystem_statfs_timeout: float = 5.0
    diskstats_device_exclude: str = DEFAULT_DISK_DEVICE_EXCLUDE
    network_device_exclude: str = DEFAULT_NETWORK_DEVICE_EXCLUDE
    systemd_unit_include: str = DEFAULT_SYSTEMD_UNIT_INCLUDE
    systemd_unit_exclude: str = DEFAULT_SYSTEMD_UNIT_EXCLUDE
    zfs_datasets: bool = True

    pve_node: str | None = None
    pve_api_url: str = DEFAULT_PVE_API_URL
    pve_api_token_file: str | None = None
    pve_api_ca_file: str | None = None
    pve_api_insecure: bool = False

    ups_targets: list[str] = field(default_factory=list)
