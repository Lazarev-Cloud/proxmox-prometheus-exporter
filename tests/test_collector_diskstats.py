from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

import pytest

from conftest import collect, fixture, value, write_tree
from proxmox_node_exporter.collectors.base import Context
from proxmox_node_exporter.collectors.diskstats import DiskstatsCollector

# kernel < 4.18: 11 counters after the device name (14 fields in total)
DISKSTATS_14 = """\
   8       0 sda 187234 45123 12345678 98765 234567 123456 23456789 456789 0 345678 567890
   8       1 sda1 102 0 4232 21 0 0 0 0 0 44 21
 253       0 dm-0 4000 0 100000 2000 8000 0 200000 30000 0 20000 32000
"""

# kernel 4.18 - 5.4: discard counters added (18 fields)
DISKSTATS_18 = (
    "   8       0 sda 187234 45123 12345678 98765 234567 123456 23456789 456789 0 345678 567890"
    " 1234 0 567890 123\n"
)

SDA_BASE = {
    "node_disk_reads_completed_total": 187234,
    "node_disk_reads_merged_total": 45123,
    "node_disk_read_bytes_total": 12345678 * 512,
    "node_disk_read_time_seconds_total": 98.765,
    "node_disk_writes_completed_total": 234567,
    "node_disk_writes_merged_total": 123456,
    "node_disk_written_bytes_total": 23456789 * 512,
    "node_disk_write_time_seconds_total": 456.789,
    "node_disk_io_now": 0,
    "node_disk_io_time_seconds_total": 345.678,
    "node_disk_io_time_weighted_seconds_total": 567.89,
}
SDA_DISCARD = {
    "node_disk_discards_completed_total": 1234,
    "node_disk_discards_merged_total": 0,
    "node_disk_discarded_sectors_total": 567890,
    "node_disk_discard_time_seconds_total": 0.123,
}
SDA_FLUSH = {
    "node_disk_flush_requests_total": 45678,
    "node_disk_flush_requests_time_seconds_total": 9.012,
}


def devices(samples: dict[str, dict[frozenset[tuple[str, str]], float]]) -> set[str]:
    return {dict(key)["device"] for key in samples["node_disk_reads_completed_total"]}


def check(samples: dict[str, dict[frozenset[tuple[str, str]], float]], expected: dict) -> None:
    for name, want in expected.items():
        assert value(samples, name, device="sda") == pytest.approx(want), name


@pytest.fixture
def host(tmp_path: Path) -> Path:
    write_tree(
        tmp_path,
        {
            "proc/diskstats": fixture("diskstats/diskstats-6.8.txt"),
            "sys/devices/virtual/block/dm-0/dm/name": "pve-root\n",
            "sys/devices/virtual/block/dm-0/dm/uuid": "LVM-abcdef\n",
            "sys/devices/virtual/block/dm-1/dm/name": "pve-vm--100--disk--0\n",
            # dm-2 is being torn down: no dm/name any more
            "sys/devices/virtual/block/dm-2/size": "0\n",
        },
    )
    (tmp_path / "sys" / "block").mkdir()
    for dm in ("dm-0", "dm-1", "dm-2"):
        os.symlink(f"../devices/virtual/block/{dm}", tmp_path / "sys" / "block" / dm)
    return tmp_path


def test_detect(tmp_path: Path, make_ctx: Callable[..., Context]) -> None:
    ctx = make_ctx()
    assert not DiskstatsCollector(ctx).detect()
    write_tree(tmp_path, {"proc/diskstats": DISKSTATS_14})
    assert DiskstatsCollector(ctx).detect()


def test_missing_file_raises(make_ctx: Callable[..., Context]) -> None:
    with pytest.raises(RuntimeError, match="diskstats"):
        collect(DiskstatsCollector(make_ctx()))


@pytest.mark.usefixtures("host")
def test_modern_kernel_all_fields(make_ctx: Callable[..., Context]) -> None:
    samples = collect(DiskstatsCollector(make_ctx()))
    check(samples, {**SDA_BASE, **SDA_DISCARD, **SDA_FLUSH})
    assert value(samples, "node_disk_io_now", device="nvme0n1") == 2
    assert value(samples, "node_disk_read_bytes_total", device="nvme0n1") == 2000 * 512
    assert value(samples, "node_disk_written_bytes_total", device="nvme0n1") == 6000 * 512
    # discarded sectors stay in sectors (like node_exporter)
    assert value(samples, "node_disk_discarded_sectors_total", device="nvme0n1") == 204800
    assert value(samples, "node_disk_flush_requests_time_seconds_total", device="nvme0n1") == (
        pytest.approx(0.13)
    )
    assert value(samples, "node_disk_written_bytes_total", device="md127") == 104000 * 512


@pytest.mark.usefixtures("host")
def test_default_device_exclusions(make_ctx: Callable[..., Context]) -> None:
    samples = collect(DiskstatsCollector(make_ctx()))
    # partitions (sda1, nvme0n1p1, zd0p1), loop, ram, zram and nbd are dropped
    assert devices(samples) == {"sda", "nvme0n1", "dm-0", "dm-1", "dm-2", "zd0", "md127", "sr0"}
    for family in samples.values():
        for key in family:
            assert dict(key)["device"] in devices(samples)


@pytest.mark.usefixtures("host")
def test_device_mapper_names(make_ctx: Callable[..., Context]) -> None:
    samples = collect(DiskstatsCollector(make_ctx()))
    assert samples["node_disk_device_mapper_info"] == {
        frozenset({("device", "dm-0"), ("name", "pve-root")}): 1,
        frozenset({("device", "dm-1"), ("name", "pve-vm--100--disk--0")}): 1,
    }


def test_old_kernel_14_fields(tmp_path: Path, make_ctx: Callable[..., Context]) -> None:
    # 2.6 kernels also listed partitions with only four counters; those lines
    # are ignored.
    text = DISKSTATS_14 + "   8    2 sda2 1234 56789 5678 45678\n"
    write_tree(tmp_path, {"proc/diskstats": text})
    samples = collect(DiskstatsCollector(make_ctx()))
    check(samples, SDA_BASE)
    assert devices(samples) == {"sda", "dm-0"}
    for name in (*SDA_DISCARD, *SDA_FLUSH):
        assert name not in samples
    # no sysfs entry for dm-0: no device-mapper name
    assert "node_disk_device_mapper_info" not in samples


def test_kernel_18_fields(tmp_path: Path, make_ctx: Callable[..., Context]) -> None:
    write_tree(tmp_path, {"proc/diskstats": DISKSTATS_18})
    samples = collect(DiskstatsCollector(make_ctx()))
    check(samples, {**SDA_BASE, **SDA_DISCARD})
    for name in SDA_FLUSH:
        assert name not in samples


@pytest.mark.parametrize(
    "device",
    [
        "loop0",
        "loop12",
        "ram0",
        "zram1",
        "fd0",
        "nbd15",
        "sda1",
        "sdaa12",
        "hda1",
        "vda2",
        "xvda1",
        "nvme0n1p1",
        "nvme10n2p12",
        "zd16p1",
    ],
)
def test_excluded_by_default(tmp_path: Path, make_ctx: Callable[..., Context], device: str) -> None:
    write_tree(tmp_path, {"proc/diskstats": f"   8 1 {device} {' '.join(['1'] * 17)}\n"})
    assert collect(DiskstatsCollector(make_ctx())) == {}


@pytest.mark.parametrize(
    "device",
    ["sda", "sdaa", "vda", "xvda", "nvme0n1", "nvme1n2", "zd16", "dm-3", "md0", "sr0", "rbd0"],
)
def test_included_by_default(tmp_path: Path, make_ctx: Callable[..., Context], device: str) -> None:
    write_tree(tmp_path, {"proc/diskstats": f"   8 0 {device} {' '.join(['1'] * 17)}\n"})
    samples = collect(DiskstatsCollector(make_ctx()))
    assert devices(samples) == {device}
    assert len(samples) == 17


@pytest.mark.usefixtures("host")
def test_custom_exclude(make_ctx: Callable[..., Context]) -> None:
    samples = collect(DiskstatsCollector(make_ctx(diskstats_device_exclude=r"^(dm-\d+|sr\d+)$")))
    assert devices(samples) == {
        "loop0",
        "loop1",
        "ram0",
        "sda",
        "sda1",
        "sda2",
        "sda3",
        "nvme0n1",
        "nvme0n1p1",
        "zd0",
        "zd0p1",
        "md127",
        "zram0",
        "nbd0",
    }
    assert "node_disk_device_mapper_info" not in samples
