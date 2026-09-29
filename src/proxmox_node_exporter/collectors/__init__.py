"""All collectors, in the order they are listed and started."""

from .base import Collector
from .btrfs import BtrfsCollector
from .containers import ContainersCollector
from .diskstats import DiskstatsCollector
from .filesystem import FilesystemCollector
from .gpu import GpuCollector
from .hwmon import HwmonCollector
from .ipmi import IpmiCollector
from .mdadm import MdadmCollector
from .network import NetworkCollector
from .pve import PveCollector
from .smart import SmartCollector
from .system import SystemCollector
from .systemd import SystemdCollector
from .ups import UpsCollector
from .zfs import ZfsCollector

ALL_COLLECTORS: list[type[Collector]] = [
    SystemCollector,
    FilesystemCollector,
    DiskstatsCollector,
    NetworkCollector,
    HwmonCollector,
    PveCollector,
    ZfsCollector,
    SmartCollector,
    GpuCollector,
    IpmiCollector,
    MdadmCollector,
    BtrfsCollector,
    SystemdCollector,
    UpsCollector,
    ContainersCollector,
]
