from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from conftest import collect, fixture, value, write_tree
from proxmox_node_exporter.collectors.base import Context
from proxmox_node_exporter.collectors.mdadm import STATES, MdadmCollector

Samples = dict[str, dict[frozenset[tuple[str, str]], float]]
KIB = 1024


def run(tmp_path: Path, make_ctx: Callable[..., Context], name: str) -> Samples:
    write_tree(tmp_path, {"proc/mdstat": fixture(f"mdadm/{name}")})
    return collect(MdadmCollector(make_ctx()))


def state(samples: Samples, device: str) -> str:
    """The single state set to 1; every state is exported for every array."""
    states = {
        dict(k)["state"]: v for k, v in samples["node_md_state"].items() if ("device", device) in k
    }
    assert set(states) == set(STATES)
    active = [s for s, v in states.items() if v == 1]
    assert sorted(states.values()) == [0] * (len(STATES) - 1) + [1]
    return active[0]


def array(samples: Samples, device: str) -> dict[str, float | None]:
    names = (
        "disks",
        "disks_active",
        "disks_failed",
        "disks_spare",
        "disks_required",
        "degraded",
        "blocks_total",
        "blocks_synced",
        "sync_completed_percent",
        "sync_speed_bytes_per_second",
    )
    return {name: value(samples, f"node_md_{name}", device=device) for name in names}


def devices(samples: Samples) -> set[str]:
    return {dict(k)["device"] for k in samples["node_md_disks"]}


def test_detect(tmp_path: Path, make_ctx: Callable[..., Context]) -> None:
    ctx = make_ctx()
    assert not MdadmCollector(ctx).detect()
    write_tree(tmp_path, {"proc/mdstat": "Personalities : \nunused devices: <none>\n"})
    assert MdadmCollector(ctx).detect()


def test_missing_mdstat_raises(make_ctx: Callable[..., Context]) -> None:
    with pytest.raises(RuntimeError, match="mdstat"):
        collect(MdadmCollector(make_ctx()))


def test_no_arrays(tmp_path: Path, make_ctx: Callable[..., Context]) -> None:
    write_tree(tmp_path, {"proc/mdstat": "Personalities : \nunused devices: <none>\n"})
    assert collect(MdadmCollector(make_ctx())) == {}


def test_healthy_arrays(tmp_path: Path, make_ctx: Callable[..., Context]) -> None:
    samples = run(tmp_path, make_ctx, "mdstat-healthy.txt")
    assert devices(samples) == {"md0", "md1", "md2", "md3", "md4"}
    for device in ("md0", "md1", "md2", "md3", "md4"):
        assert state(samples, device) == "active"
    assert array(samples, "md0") == {
        "disks": 2,
        "disks_active": 2,
        "disks_failed": 0,
        "disks_spare": 0,
        "disks_required": 2,
        "degraded": 0,
        "blocks_total": 1046528,
        "blocks_synced": 1046528,
        "sync_completed_percent": None,
        "sync_speed_bytes_per_second": None,
    }
    assert value(samples, "node_md_blocks_total", device="md1") == 937049088
    raid10 = array(samples, "md2")
    assert raid10["disks"] == 4
    assert raid10["disks_required"] == 4
    assert raid10["degraded"] == 0
    assert raid10["blocks_total"] == raid10["blocks_synced"] == 3906764800


def test_raid0_has_no_redundancy_counts(tmp_path: Path, make_ctx: Callable[..., Context]) -> None:
    samples = run(tmp_path, make_ctx, "mdstat-healthy.txt")
    assert array(samples, "md3") == {
        "disks": 2,
        "disks_active": 2,
        "disks_failed": 0,
        "disks_spare": 0,
        "disks_required": None,
        "degraded": None,
        "blocks_total": 1953262592,
        "blocks_synced": 1953262592,
        "sync_completed_percent": None,
        "sync_speed_bytes_per_second": None,
    }


def test_read_only_array_is_active(tmp_path: Path, make_ctx: Callable[..., Context]) -> None:
    samples = run(tmp_path, make_ctx, "mdstat-healthy.txt")
    assert state(samples, "md4") == "active"
    assert value(samples, "node_md_disks", device="md4") == 2
    assert value(samples, "node_md_blocks_total", device="md4") == 488254464


def test_raid5_recovery_with_failed_and_spare(
    tmp_path: Path, make_ctx: Callable[..., Context]
) -> None:
    samples = run(tmp_path, make_ctx, "mdstat-recovery.txt")
    assert devices(samples) == {"md0", "md1", "md2", "md127"}
    assert state(samples, "md2") == "recovering"
    assert array(samples, "md2") == {
        "disks": 6,
        "disks_active": 4,  # includes sde1, which is being rebuilt
        "disks_failed": 1,
        "disks_spare": 1,
        "disks_required": 4,
        "degraded": 1,
        "blocks_total": 5860147200,
        "blocks_synced": 340047488,
        "sync_completed_percent": 17.4,
        "sync_speed_bytes_per_second": 180883 * KIB,
    }


def test_resync_in_progress(tmp_path: Path, make_ctx: Callable[..., Context]) -> None:
    samples = run(tmp_path, make_ctx, "mdstat-recovery.txt")
    assert state(samples, "md0") == "resync"
    md0 = array(samples, "md0")
    assert md0["blocks_total"] == 1046528
    assert md0["blocks_synced"] == 505728
    assert md0["sync_completed_percent"] == 48.3
    assert md0["sync_speed_bytes_per_second"] == 101145 * KIB
    assert md0["degraded"] == 0


@pytest.mark.parametrize(("device", "blocks"), [("md1", 976629760), ("md127", 1953382464)])
def test_delayed_and_pending_resync(
    tmp_path: Path, make_ctx: Callable[..., Context], device: str, blocks: int
) -> None:
    # md1 waits for md0 on the same disks (resync=DELAYED); md127 is
    # auto-read-only and resyncs on first write (resync=PENDING).
    samples = run(tmp_path, make_ctx, "mdstat-recovery.txt")
    assert state(samples, device) == "resync"
    assert array(samples, device) == {
        "disks": 2,
        "disks_active": 2,
        "disks_failed": 0,
        "disks_spare": 0,
        "disks_required": 2,
        "degraded": 0,
        "blocks_total": blocks,
        "blocks_synced": 0,  # nothing resynced yet, as in node_exporter
        "sync_completed_percent": None,
        "sync_speed_bytes_per_second": None,
    }


def test_inactive_arrays(tmp_path: Path, make_ctx: Callable[..., Context]) -> None:
    samples = run(tmp_path, make_ctx, "mdstat-inactive.txt")
    assert state(samples, "md126") == "inactive"
    assert state(samples, "md127") == "inactive"
    assert array(samples, "md126") == {
        "disks": 1,
        "disks_active": 0,
        "disks_failed": 0,
        "disks_spare": 1,
        "disks_required": None,
        "degraded": None,
        "blocks_total": 976630488,
        "blocks_synced": None,  # not reported for inactive arrays
        "sync_completed_percent": None,
        "sync_speed_bytes_per_second": None,
    }
    md127 = array(samples, "md127")
    assert md127["disks"] == 2
    assert md127["disks_spare"] == 2
    assert md127["disks_active"] == 0
    assert md127["blocks_total"] == 10402


def test_members_with_several_flags(tmp_path: Path, make_ctx: Callable[..., Context]) -> None:
    # A failed write-mostly member prints as "sdb1[1](W)(F)", a write-mostly
    # spare as "sdd1[2](W)(S)".
    samples = run(tmp_path, make_ctx, "mdstat-write-mostly.txt")
    md0 = array(samples, "md0")
    assert (md0["disks"], md0["disks_active"], md0["disks_failed"], md0["disks_spare"]) == (
        2,
        1,
        1,
        0,
    )
    assert md0["degraded"] == 1
    md1 = array(samples, "md1")
    assert (md1["disks"], md1["disks_active"], md1["disks_failed"], md1["disks_spare"]) == (
        3,
        2,
        0,
        1,
    )
    assert md1["degraded"] == 0


def test_check(tmp_path: Path, make_ctx: Callable[..., Context]) -> None:
    samples = run(tmp_path, make_ctx, "mdstat-check.txt")
    assert state(samples, "md0") == "check"
    md0 = array(samples, "md0")
    assert md0["sync_completed_percent"] == 8.5
    assert md0["blocks_synced"] == 166042368
    assert md0["sync_speed_bytes_per_second"] == 206550 * KIB
    assert md0["degraded"] == 0


def test_reshape(tmp_path: Path, make_ctx: Callable[..., Context]) -> None:
    samples = run(tmp_path, make_ctx, "mdstat-reshape.txt")
    assert state(samples, "md0") == "reshape"
    md0 = array(samples, "md0")
    assert md0["disks"] == 5
    assert md0["disks_required"] == 5
    assert md0["sync_completed_percent"] == 1.2
    assert md0["blocks_synced"] == 23441536
    assert md0["sync_speed_bytes_per_second"] == 39598 * KIB
