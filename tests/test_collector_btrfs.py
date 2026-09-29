from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

import pytest

from conftest import collect, value, write_tree
from proxmox_node_exporter.collectors.base import Context
from proxmox_node_exporter.collectors.btrfs import BtrfsCollector

DATA_UUID = "3a4f6c1e-8b2d-4e7a-9c15-2f0d8e6b7a91"
ROOT_UUID = "c0ffee00-1234-4abc-8def-0123456789ab"
SATA = "pci0000:00/0000:00:17.0/ata{n}/host{h}/target{h}:0:0/{h}:0:0:0/block/{dev}"
NVME = "pci0000:00/0000:00:1d.0/0000:3d:00.0/nvme/nvme0/nvme0n1"

ERROR_STATS_CLEAN = (
    "write_errs 0\nread_errs 0\nflush_errs 0\ncorruption_errs 0\ngeneration_errs 0\n"
)
ERROR_STATS_BAD = "write_errs 3\nread_errs 12\nflush_errs 0\ncorruption_errs 7\ngeneration_errs 1\n"


def allocation(uuid: str, sizes: dict[str, tuple[int, int]], profile: str) -> dict[str, str]:
    files = {}
    for kind, (total, used) in sizes.items():
        base = f"sys/fs/btrfs/{uuid}/allocation/{kind}"
        files.update(
            {
                f"{base}/total_bytes": f"{total}\n",
                f"{base}/bytes_used": f"{used}\n",
                f"{base}/disk_total": f"{total * 2}\n",
                f"{base}/disk_used": f"{used * 2}\n",
                f"{base}/bytes_pinned": "0\n",
                f"{base}/bytes_readonly": "0\n",
                f"{base}/bytes_reserved": "0\n",
                f"{base}/bytes_may_use": "0\n",
                f"{base}/flags": "1\n",
                f"{base}/{profile}/total_bytes": f"{total}\n",
                f"{base}/{profile}/used_bytes": f"{used}\n",
            }
        )
    return files


def devinfo(uuid: str, devid: int, *, missing: int, error_stats: str | None) -> dict[str, str]:
    base = f"sys/fs/btrfs/{uuid}/devinfo/{devid}"
    files = {
        f"{base}/missing": f"{missing}\n",
        f"{base}/in_fs_metadata": "1\n",
        f"{base}/replace_target": "0\n",
        f"{base}/writeable": f"{1 - missing}\n",
        f"{base}/scrub_speed_max": "0\n",
        f"{base}/fsid": f"{uuid}\n",
    }
    if error_stats is not None:
        files[f"{base}/error_stats"] = error_stats
    return files


def link_device(root: Path, uuid: str, name: str, target: str, sectors: int) -> None:
    """devices/<name> is a symlink to the block device's sysfs directory."""
    block = root / "sys" / "devices" / target
    write_tree(block, {"size": f"{sectors}\n", "ro": "0\n", "dev": "8:16\n"})
    devices = root / "sys" / "fs" / "btrfs" / uuid / "devices"
    devices.mkdir(parents=True, exist_ok=True)
    os.symlink(os.path.relpath(block, devices), devices / name)


def build_host(root: Path) -> None:
    write_tree(
        root,
        {
            "sys/fs/btrfs/features/big_metadata": "0\n",
            "sys/fs/btrfs/features/free_space_tree": "0\n",
            "sys/fs/btrfs/features/raid1c34": "0\n",
            # two-disk RAID1 data filesystem, one disk with errors
            f"sys/fs/btrfs/{DATA_UUID}/label": "data\n",
            f"sys/fs/btrfs/{DATA_UUID}/nodesize": "16384\n",
            f"sys/fs/btrfs/{DATA_UUID}/sectorsize": "4096\n",
            f"sys/fs/btrfs/{DATA_UUID}/generation": "123456\n",
            f"sys/fs/btrfs/{DATA_UUID}/allocation/global_rsv_size": "536870912\n",
            **allocation(
                DATA_UUID,
                {
                    "data": (1099511627776, 858993459200),
                    "metadata": (5368709120, 1610612736),
                    "system": (33554432, 163840),
                },
                "raid1",
            ),
            **devinfo(DATA_UUID, 1, missing=0, error_stats=ERROR_STATS_CLEAN),
            **devinfo(DATA_UUID, 2, missing=0, error_stats=ERROR_STATS_BAD),
            # unlabelled root filesystem mounted degraded: devid 2 is gone
            f"sys/fs/btrfs/{ROOT_UUID}/label": "\n",
            **allocation(
                ROOT_UUID,
                {
                    "data": (53687091200, 21474836480),
                    "metadata": (2147483648, 536870912),
                    "system": (33554432, 16384),
                },
                "raid1",
            ),
            **devinfo(ROOT_UUID, 1, missing=0, error_stats=ERROR_STATS_CLEAN),
            **devinfo(ROOT_UUID, 2, missing=1, error_stats=ERROR_STATS_CLEAN),
        },
    )
    link_device(root, DATA_UUID, "sdb", SATA.format(n=2, h=1, dev="sdb"), 7814037168)
    link_device(root, DATA_UUID, "sdc", SATA.format(n=3, h=2, dev="sdc"), 7814037168)
    link_device(root, ROOT_UUID, "nvme0n1p3", f"{NVME}/nvme0n1p3", 1953456128)


@pytest.fixture
def samples(tmp_path: Path, make_ctx: Callable[..., Context]) -> dict:
    build_host(tmp_path)
    return collect(BtrfsCollector(make_ctx()))


def test_detect(tmp_path: Path, make_ctx: Callable[..., Context]) -> None:
    ctx = make_ctx()
    assert not BtrfsCollector(ctx).detect()
    (tmp_path / "sys" / "fs" / "btrfs").mkdir(parents=True)
    assert BtrfsCollector(ctx).detect()


def test_module_loaded_without_filesystems(
    tmp_path: Path, make_ctx: Callable[..., Context]
) -> None:
    write_tree(tmp_path, {"sys/fs/btrfs/features/raid1c34": "0\n"})
    assert collect(BtrfsCollector(make_ctx())) == {}


def test_info_skips_non_uuid_entries(samples: dict) -> None:
    assert samples["node_btrfs_info"] == {
        frozenset({("uuid", DATA_UUID), ("label", "data")}): 1,
        frozenset({("uuid", ROOT_UUID), ("label", "")}): 1,
    }


def test_allocation(samples: dict) -> None:
    data = {"uuid": DATA_UUID, "label": "data"}
    root = {"uuid": ROOT_UUID, "label": ""}
    assert value(samples, "node_btrfs_allocation_bytes", type="data", **data) == 1099511627776
    assert value(samples, "node_btrfs_used_bytes", type="data", **data) == 858993459200
    assert value(samples, "node_btrfs_allocation_bytes", type="metadata", **data) == 5368709120
    assert value(samples, "node_btrfs_used_bytes", type="metadata", **data) == 1610612736
    assert value(samples, "node_btrfs_allocation_bytes", type="system", **data) == 33554432
    assert value(samples, "node_btrfs_used_bytes", type="system", **data) == 163840
    assert value(samples, "node_btrfs_allocation_bytes", type="data", **root) == 53687091200
    assert value(samples, "node_btrfs_used_bytes", type="system", **root) == 16384
    assert len(samples["node_btrfs_allocation_bytes"]) == 6
    assert len(samples["node_btrfs_used_bytes"]) == 6


def test_device_sizes_in_bytes(samples: dict) -> None:
    # size is in 512-byte sectors, read through the devices/<name> symlink
    assert samples["node_btrfs_device_size_bytes"] == {
        frozenset({("uuid", DATA_UUID), ("label", "data"), ("device", "sdb")}): 7814037168 * 512,
        frozenset({("uuid", DATA_UUID), ("label", "data"), ("device", "sdc")}): 7814037168 * 512,
        frozenset({("uuid", ROOT_UUID), ("label", ""), ("device", "nvme0n1p3")}): (
            1953456128 * 512
        ),
    }


def test_device_errors(samples: dict) -> None:
    data = {"uuid": DATA_UUID, "label": "data"}
    expected = {"write": 3, "read": 12, "flush": 0, "corruption": 7, "generation": 1}
    for kind, count in expected.items():
        assert value(samples, "node_btrfs_device_errors_total", devid="2", type=kind, **data) == (
            count
        )
        assert value(samples, "node_btrfs_device_errors_total", devid="1", type=kind, **data) == 0
    types = {dict(k)["type"] for k in samples["node_btrfs_device_errors_total"]}
    assert types == set(expected)
    assert len(samples["node_btrfs_device_errors_total"]) == 4 * 5


def test_missing_device(samples: dict) -> None:
    assert samples["node_btrfs_device_missing"] == {
        frozenset({("uuid", DATA_UUID), ("label", "data"), ("devid", "1")}): 0,
        frozenset({("uuid", DATA_UUID), ("label", "data"), ("devid", "2")}): 0,
        frozenset({("uuid", ROOT_UUID), ("label", ""), ("devid", "1")}): 0,
        frozenset({("uuid", ROOT_UUID), ("label", ""), ("devid", "2")}): 1,
    }
    # the missing device has no entry below devices/
    devices = {dict(k)["device"] for k in samples["node_btrfs_device_size_bytes"]}
    assert devices == {"sdb", "sdc", "nvme0n1p3"}


def test_kernel_without_devinfo(tmp_path: Path, make_ctx: Callable[..., Context]) -> None:
    # devinfo/ appeared in 5.9 and error_stats in 5.14
    write_tree(
        tmp_path,
        {
            f"sys/fs/btrfs/{DATA_UUID}/label": "data\n",
            **allocation(DATA_UUID, {"data": (1073741824, 536870912)}, "single"),
        },
    )
    link_device(tmp_path, DATA_UUID, "sdb1", SATA.format(n=2, h=1, dev="sdb") + "/sdb1", 2048)
    samples = collect(BtrfsCollector(make_ctx()))
    assert set(samples) == {
        "node_btrfs_info",
        "node_btrfs_allocation_bytes",
        "node_btrfs_used_bytes",
        "node_btrfs_device_size_bytes",
    }
    fs = {"uuid": DATA_UUID, "label": "data"}
    assert value(samples, "node_btrfs_device_size_bytes", device="sdb1", **fs) == 2048 * 512
    assert value(samples, "node_btrfs_allocation_bytes", type="metadata", **fs) is None


def test_devinfo_without_error_stats(tmp_path: Path, make_ctx: Callable[..., Context]) -> None:
    write_tree(
        tmp_path,
        {
            f"sys/fs/btrfs/{DATA_UUID}/label": "data\n",
            **devinfo(DATA_UUID, 1, missing=0, error_stats=None),
        },
    )
    samples = collect(BtrfsCollector(make_ctx()))
    assert "node_btrfs_device_errors_total" not in samples
    assert value(samples, "node_btrfs_device_missing", uuid=DATA_UUID, label="data", devid="1") == 0
