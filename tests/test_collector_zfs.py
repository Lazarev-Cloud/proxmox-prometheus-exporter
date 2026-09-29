from __future__ import annotations

import calendar
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Callable

import pytest

from conftest import FakeRunner, Samples, collect, fixture, value, write_tree
from proxmox_node_exporter.collectors.base import Context
from proxmox_node_exporter.collectors.zfs import (
    POOL_STATES,
    SCAN_STATES,
    ZfsCollector,
    parse_scan,
    parse_zpool_status,
)
from proxmox_node_exporter.runner import CommandError

ZPOOL_LIST = (
    "zpool", "list", "-Hp", "-o", "name,size,allocated,free,fragmentation,dedupratio,health",
)  # fmt: skip
ZPOOL_STATUS = ("zpool", "status", "-p")
ZFS_LIST = (
    "zfs", "list", "-Hp", "-t", "filesystem,volume", "-o", "name,used,avail,refer,type",
)  # fmt: skip

# Values of the tracked counters in fixtures/zfs/arcstats.txt.
ARC_HITS = 2361530466
ARC_MISSES = 40217843


def utc(text: str) -> float:
    return float(calendar.timegm(time.strptime(text, "%Y-%m-%d %H:%M:%S")))


@pytest.fixture
def set_tz(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], None]]:
    """zpool prints ctime() in local time; pin the time zone the collector parses in."""

    def apply(name: str) -> None:
        monkeypatch.setenv("TZ", name)
        time.tzset()

    yield apply
    monkeypatch.undo()
    time.tzset()


def zfs_runner(status: str = "zfs/zpool_status.txt") -> FakeRunner:
    return FakeRunner(
        {
            ("zpool", "list"): fixture("zfs/zpool_list.txt"),
            ("zpool", "status"): fixture(status),
            ("zfs", "list"): fixture("zfs/zfs_list.txt"),
        }
    )


def write_arcstats(tmp_path: Path) -> None:
    write_tree(tmp_path / "proc", {"spl/kstat/zfs/arcstats": fixture("zfs/arcstats.txt")})


@pytest.fixture
def samples(
    make_ctx: Callable[..., Context], tmp_path: Path, set_tz: Callable[[str], None]
) -> Samples:
    set_tz("UTC")
    write_arcstats(tmp_path)
    return collect(ZfsCollector(make_ctx(zfs_runner())))


def one_hot(samples: Samples, name: str, states: tuple[str, ...], **labels: str) -> str:
    """Return the single state set to 1, checking every other state is 0."""
    active = [s for s in states if value(samples, name, state=s, **labels) == 1]
    assert len(active) == 1, active
    for state in states:
        assert value(samples, name, state=state, **labels) in (0, 1)
    return active[0]


# -- detection -------------------------------------------------------------------------


def test_detect_with_kstat_directory_only(make_ctx: Callable[..., Context], tmp_path: Path) -> None:
    (tmp_path / "proc" / "spl" / "kstat" / "zfs").mkdir(parents=True)
    assert ZfsCollector(make_ctx(FakeRunner(), is_root=False)).detect()


def test_detect_with_zpool_only(make_ctx: Callable[..., Context]) -> None:
    assert ZfsCollector(make_ctx(FakeRunner({ZPOOL_LIST: ""}))).detect()


def test_detect_without_zfs(make_ctx: Callable[..., Context]) -> None:
    assert not ZfsCollector(make_ctx(FakeRunner())).detect()


# -- ARC -------------------------------------------------------------------------------


def test_arcstats(samples: Samples) -> None:
    assert value(samples, "node_zfs_arc_size_bytes") == 16790138512
    assert value(samples, "node_zfs_arc_c_bytes") == 16813524992
    assert value(samples, "node_zfs_arc_c_min_bytes") == 1050845312
    assert value(samples, "node_zfs_arc_c_max_bytes") == 16813524992
    assert value(samples, "node_zfs_arc_mru_size_bytes") == 5821386752
    assert value(samples, "node_zfs_arc_mfu_size_bytes") == 9338470400
    assert value(samples, "node_zfs_arc_hits_total") == ARC_HITS
    assert value(samples, "node_zfs_arc_misses_total") == ARC_MISSES
    assert value(samples, "node_zfs_l2arc_hits_total") == 1234567
    assert value(samples, "node_zfs_l2arc_misses_total") == 7654321
    assert value(samples, "node_zfs_l2arc_size_bytes") == 214748364800
    assert value(samples, "node_zfs_arc_hit_ratio") == pytest.approx(
        ARC_HITS / (ARC_HITS + ARC_MISSES)
    )


def test_arcstats_without_zpool_command(make_ctx: Callable[..., Context], tmp_path: Path) -> None:
    write_arcstats(tmp_path)
    runner = FakeRunner()
    samples = collect(ZfsCollector(make_ctx(runner)))
    assert value(samples, "node_zfs_arc_size_bytes") == 16790138512
    assert not any(name.startswith("node_zfs_zpool") for name in samples)
    assert runner.calls == []


def test_arcstats_without_lookups_has_no_hit_ratio(
    make_ctx: Callable[..., Context], tmp_path: Path
) -> None:
    write_tree(
        tmp_path / "proc",
        {
            "spl/kstat/zfs/arcstats": "9 1 0x01 3 816 4907542925 2081223618634807\n"
            "name                            type data\n"
            "hits                            4    0\n"
            "misses                          4    0\n"
            "size                            4    1048576\n"
        },
    )
    samples = collect(ZfsCollector(make_ctx(FakeRunner())))
    assert value(samples, "node_zfs_arc_size_bytes") == 1048576
    assert value(samples, "node_zfs_arc_hits_total") == 0
    assert "node_zfs_arc_hit_ratio" not in samples


# -- zpool list ------------------------------------------------------------------------


def test_commands_run(make_ctx: Callable[..., Context]) -> None:
    runner = zfs_runner()
    collect(ZfsCollector(make_ctx(runner)))
    assert runner.calls == [ZPOOL_LIST, ZPOOL_STATUS, ZFS_LIST]


def test_pool_capacity(samples: Samples) -> None:
    assert value(samples, "node_zfs_zpool_size_bytes", pool="rpool") == 996432412672
    assert value(samples, "node_zfs_zpool_allocated_bytes", pool="rpool") == 52438347776
    assert value(samples, "node_zfs_zpool_free_bytes", pool="rpool") == 943994064896
    assert value(samples, "node_zfs_zpool_fragmentation_percent", pool="rpool") == 4
    assert value(samples, "node_zfs_zpool_deduplication_ratio", pool="rpool") == 1.0
    assert value(samples, "node_zfs_zpool_size_bytes", pool="tank") == 23991687217152
    assert value(samples, "node_zfs_zpool_fragmentation_percent", pool="tank") == 17
    assert value(samples, "node_zfs_zpool_deduplication_ratio", pool="fast") == 1.35
    assert {dict(k)["pool"] for k in samples["node_zfs_zpool_size_bytes"]} == {
        "rpool",
        "tank",
        "backup",
        "fast",
    }


@pytest.mark.parametrize(
    ("pool", "health"),
    [("rpool", "online"), ("tank", "degraded"), ("backup", "degraded"), ("fast", "online")],
)
def test_pool_health(samples: Samples, pool: str, health: str) -> None:
    states = tuple(s.lower() for s in POOL_STATES)
    assert one_hot(samples, "node_zfs_zpool_state", states, pool=pool) == health
    assert value(samples, "node_zfs_zpool_health", pool=pool) == states.index(health)
    assert len([k for k in samples["node_zfs_zpool_state"] if ("pool", pool) in k]) == 7


def test_unavailable_pool_reports_health_only(make_ctx: Callable[..., Context]) -> None:
    # zpool prints "-" for every property of a pool that cannot be opened.
    runner = FakeRunner(
        {
            ("zpool", "list"): "old\t-\t-\t-\t-\t-\tFAULTED\n"
            "hung\t1992864825344\t411937685504\t1580927139840\t2\t1.00\tSUSPENDED\n",
            ("zpool", "status"): "",
        }
    )
    samples = collect(ZfsCollector(make_ctx(runner)))
    assert value(samples, "node_zfs_zpool_size_bytes", pool="old") is None
    assert value(samples, "node_zfs_zpool_fragmentation_percent", pool="old") is None
    assert value(samples, "node_zfs_zpool_deduplication_ratio", pool="old") is None
    assert value(samples, "node_zfs_zpool_health", pool="old") == 2
    assert value(samples, "node_zfs_zpool_state", pool="old", state="faulted") == 1
    assert value(samples, "node_zfs_zpool_health", pool="hung") == 6
    assert value(samples, "node_zfs_zpool_state", pool="hung", state="suspended") == 1


def test_no_pools(make_ctx: Callable[..., Context]) -> None:
    runner = FakeRunner({("zpool", "list"): "", ("zpool", "status"): "no pools available\n"})
    samples = collect(ZfsCollector(make_ctx(runner, zfs_datasets=False)))
    assert samples == {}


def test_zpool_failure_fails_the_collector(make_ctx: Callable[..., Context]) -> None:
    runner = FakeRunner({("zpool", "list"): CommandError("zpool: timed out after 10s")})
    with pytest.raises(CommandError):
        collect(ZfsCollector(make_ctx(runner)))


# -- zpool status ----------------------------------------------------------------------


def test_healthy_mirror_with_finished_scrub(samples: Samples) -> None:
    assert one_hot(samples, "node_zfs_zpool_scrub_state", SCAN_STATES, pool="rpool") == (
        "scrub_finished"
    )
    assert value(samples, "node_zfs_zpool_last_scrub_timestamp_seconds", pool="rpool") == utc(
        "2024-09-08 00:25:24"
    )
    assert value(samples, "node_zfs_zpool_scan_errors", pool="rpool") == 0
    assert value(samples, "node_zfs_zpool_scan_progress_percent", pool="rpool") is None
    assert value(samples, "node_zfs_zpool_data_errors", pool="rpool") == 0
    disks = [
        "ata-Samsung_SSD_870_EVO_1TB_S6PTNM0T712345A-part3",
        "ata-Samsung_SSD_870_EVO_1TB_S6PTNM0T712346B-part3",
    ]
    for vdev in ["rpool", "mirror-0", *disks]:
        assert value(samples, "node_zfs_zpool_vdev_online", pool="rpool", vdev=vdev) == 1
        for kind in ("read", "write", "checksum"):
            labels = {"pool": "rpool", "vdev": vdev, "type": kind}
            assert value(samples, "node_zfs_zpool_vdev_errors_total", **labels) == 0
    for kind in ("read", "write", "checksum"):
        assert value(samples, "node_zfs_zpool_errors_total", pool="rpool", type=kind) == 0


def test_degraded_raidz_with_faulted_and_unavail_disks(samples: Samples) -> None:
    def vdev(name: str, metric: str = "node_zfs_zpool_vdev_online", **kw: str) -> float | None:
        return value(samples, metric, pool="tank", vdev=name, **kw)

    assert vdev("tank") == 0
    assert vdev("raidz2-0") == 0
    assert vdev("wwn-0x5000c500d1a2b3c4") == 1
    assert vdev("wwn-0x5000c500d1a2b3c5") == 0  # FAULTED, too many errors
    assert vdev("6135942587126573451") == 0  # UNAVAIL, "was /dev/..."
    errors = "node_zfs_zpool_vdev_errors_total"
    assert vdev("wwn-0x5000c500d1a2b3c5", errors, type="read") == 14
    assert vdev("wwn-0x5000c500d1a2b3c5", errors, type="write") == 312
    assert vdev("wwn-0x5000c500d1a2b3c5", errors, type="checksum") == 0
    assert vdev("wwn-0x5000c500d1a2b3c6", errors, type="checksum") == 7
    assert vdev("wwn-0x5000c500d1a2b3c8", errors, type="read") == 3
    assert value(samples, "node_zfs_zpool_errors_total", pool="tank", type="read") == 17
    assert value(samples, "node_zfs_zpool_errors_total", pool="tank", type="write") == 312
    assert value(samples, "node_zfs_zpool_errors_total", pool="tank", type="checksum") == 7
    assert value(samples, "node_zfs_zpool_data_errors", pool="tank") == 3
    # 1 pool + 1 raidz + 6 disks
    assert len([k for k in samples["node_zfs_zpool_vdev_online"] if ("pool", "tank") in k]) == 8


def test_scrub_in_progress(samples: Samples) -> None:
    assert one_hot(samples, "node_zfs_zpool_scrub_state", SCAN_STATES, pool="tank") == (
        "scrub_in_progress"
    )
    assert value(samples, "node_zfs_zpool_scan_progress_percent", pool="tank") == 46.15
    assert value(samples, "node_zfs_zpool_last_scrub_timestamp_seconds", pool="tank") is None
    assert value(samples, "node_zfs_zpool_scan_errors", pool="tank") is None


def test_resilver_in_progress(samples: Samples) -> None:
    assert one_hot(samples, "node_zfs_zpool_scrub_state", SCAN_STATES, pool="backup") == (
        "resilver_in_progress"
    )
    assert value(samples, "node_zfs_zpool_scan_progress_percent", pool="backup") == 54.62
    online = {
        dict(k)["vdev"]: v
        for k, v in samples["node_zfs_zpool_vdev_online"].items()
        if ("pool", "backup") in k
    }
    assert online == {
        "backup": 0,
        "mirror-0": 0,
        "replacing-0": 0,
        "ata-WDC_WD40EFRX-68N32N0_WD-WCC7K1234567": 0,  # OFFLINE
        "ata-WDC_WD40EFPX-68C6CN0_WD-WX12D34E5678": 1,  # (resilvering)
        "ata-WDC_WD40EFRX-68N32N0_WD-WCC7K7654321": 1,
    }


def test_logs_cache_and_spares_sections(samples: Samples) -> None:
    assert one_hot(samples, "node_zfs_zpool_scrub_state", SCAN_STATES, pool="fast") == "none"
    assert value(samples, "node_zfs_zpool_scan_progress_percent", pool="fast") is None
    assert value(samples, "node_zfs_zpool_last_scrub_timestamp_seconds", pool="fast") is None
    vdevs = {
        dict(k)["vdev"] for k in samples["node_zfs_zpool_vdev_online"] if ("pool", "fast") in k
    }
    # Log and cache devices are real vdevs with error counters; the section headers and
    # the (counter-less) AVAIL spares are not.
    assert vdevs == {"fast", "mirror-0", "sda", "sdb", "mirror-1", "sdc", "sdd"} | {
        "mirror-2",
        "nvme0n1p1",
        "nvme1n1p1",
        "nvme0n1p2",
        "nvme1n1p2",
    }
    assert value(samples, "node_zfs_zpool_data_errors", pool="fast") == 0


def test_draid_sequential_resilver(
    make_ctx: Callable[..., Context], set_tz: Callable[[str], None]
) -> None:
    set_tz("UTC")
    runner = FakeRunner(
        {("zpool", "list"): "", ("zpool", "status"): fixture("zfs/zpool_status_draid.txt")}
    )
    samples = collect(ZfsCollector(make_ctx(runner, zfs_datasets=False)))
    # The second "scan:" line (the active rebuild) describes the current state.
    assert one_hot(samples, "node_zfs_zpool_scrub_state", SCAN_STATES, pool="vault") == (
        "resilver_in_progress"
    )
    assert value(samples, "node_zfs_zpool_scan_progress_percent", pool="vault") == 51.87
    assert value(samples, "node_zfs_zpool_health", pool="vault") == 1
    # The distributed spare is listed twice: in the tree (ONLINE) and under "spares"
    # (INUSE, no counters). The tree entry must win.
    assert value(samples, "node_zfs_zpool_vdev_online", pool="vault", vdev="draid2-0-0") == 1
    assert value(samples, "node_zfs_zpool_vdev_online", pool="vault", vdev="sde") == 0
    assert (
        value(samples, "node_zfs_zpool_vdev_errors_total", pool="vault", vdev="sde", type="write")
        == 58
    )
    assert value(samples, "node_zfs_zpool_errors_total", pool="vault", type="write") == 58


def test_parse_zpool_status_structure() -> None:
    pools = {p.name: p for p in parse_zpool_status(fixture("zfs/zpool_status.txt"))}
    assert list(pools) == ["rpool", "tank", "backup", "fast"]
    assert pools["tank"].state == "DEGRADED"
    assert pools["tank"].data_errors == 3
    assert pools["rpool"].data_errors == 0
    assert (
        "6135942587126573451",
        "UNAVAIL",
        (0, 0, 0),
    ) in pools["tank"].vdevs
    # Multi-line status/action text is not mistaken for scan text or vdevs.
    assert "features" not in pools["rpool"].scan
    assert pools["tank"].scan.startswith("scrub in progress since Sun Sep 29 02:00:01 2024")


@pytest.mark.parametrize(
    ("scan", "expected"),
    [
        ("none requested", ("none", None, None, None)),
        ("", ("none", None, None, None)),
        (
            "scrub repaired 0B in 00:01:23 with 0 errors on Sun Sep  8 00:25:24 2024",
            ("scrub_finished", None, utc("2024-09-08 00:25:24"), 0),
        ),
        (
            "scrub repaired 1.50M in 1 days 02:03:04 with 2 errors on Tue Oct  1 03:04:05 2024",
            ("scrub_finished", None, utc("2024-10-01 03:04:05"), 2),
        ),
        (
            "scrub in progress since Sun Sep 29 02:00:01 2024\n"
            "\t1.02T / 10.9T scanned at 1.21G/s, 0B / 10.9T issued\n"
            "\t0B repaired, 0.00% done, no estimated completion time",
            ("scrub_in_progress", 0.0, None, None),
        ),
        (
            "scrub paused since Mon Sep 30 12:00:00 2024\n"
            "\tscrub started on Mon Sep 30 02:00:01 2024\n"
            "\t3.21T / 10.9T scanned, 2.87T / 10.9T issued\n"
            "\t0B repaired, 26.33% done",
            ("scrub_paused", 26.33, None, None),
        ),
        ("scrub canceled on Mon Sep 30 12:00:00 2024", ("scrub_canceled", None, None, None)),
        (
            "resilver in progress since Sat Sep 28 22:14:05 2024\n"
            "\t2.31T / 3.62T scanned at 612M/s, 1.98T / 3.62T issued at 525M/s\n"
            "\t1.01T resilvered, 54.62% done, 00:54:31 to go",
            ("resilver_in_progress", 54.62, None, None),
        ),
        (
            "resilvered 1.01T in 02:10:15 with 0 errors on Sun Sep 29 00:24:20 2024",
            ("resilver_finished", None, None, 0),
        ),
        # Sequential resilver / dRAID rebuild (vdev_rebuild), which names the top-level vdev.
        (
            "resilver (draid2:4d:9c:1s-0) in progress since Mon Sep 30 09:12:44 2024\n"
            "\t5.12T / 9.87T scanned at 3.02G/s, 5.10T / 9.87T issued at 3.01G/s\n"
            "\t312G resilvered, 51.87% done, 00:27:02 to go",
            ("resilver_in_progress", 51.87, None, None),
        ),
        (
            "resilvered (mirror-0) 1.21T in 00:45:12 with 1 errors on Mon Sep 30 09:57:56 2024",
            ("resilver_finished", None, None, 1),
        ),
    ],
)
def test_parse_scan(
    set_tz: Callable[[str], None],
    scan: str,
    expected: tuple[str, float | None, float | None, int | None],
) -> None:
    set_tz("UTC")
    assert parse_scan(scan) == expected


def test_last_scrub_is_local_time(set_tz: Callable[[str], None]) -> None:
    # ctime() output is local time; 8 Sep is summer time (UTC+2) in this zone.
    set_tz("CET-1CEST,M3.5.0,M10.5.0/3")
    _, _, finished, _ = parse_scan(
        "scrub repaired 0B in 00:01:23 with 0 errors on Sun Sep  8 00:25:24 2024"
    )
    assert finished == utc("2024-09-08 00:25:24") - 2 * 3600


# -- datasets --------------------------------------------------------------------------


def test_datasets(samples: Samples) -> None:
    fs = {"dataset": "rpool/ROOT/pve-1", "type": "filesystem"}
    assert value(samples, "node_zfs_dataset_used_bytes", **fs) == 8123358208
    assert value(samples, "node_zfs_dataset_available_bytes", **fs) == 904827174912
    assert value(samples, "node_zfs_dataset_referenced_bytes", **fs) == 8123358208
    zvol = {"dataset": "rpool/data/vm-100-disk-0", "type": "volume"}
    assert value(samples, "node_zfs_dataset_used_bytes", **zvol) == 34359738368
    assert value(samples, "node_zfs_dataset_available_bytes", **zvol) == 920348192768
    assert value(samples, "node_zfs_dataset_referenced_bytes", **zvol) == 18838519808
    # Dataset names may contain spaces; the output is tab separated.
    spaced = {"dataset": "tank/media files", "type": "filesystem"}
    assert value(samples, "node_zfs_dataset_used_bytes", **spaced) == 6597069766656
    assert len(samples["node_zfs_dataset_used_bytes"]) == 8


def test_datasets_can_be_disabled(make_ctx: Callable[..., Context]) -> None:
    runner = zfs_runner()
    samples = collect(ZfsCollector(make_ctx(runner, zfs_datasets=False)))
    assert ZFS_LIST not in runner.calls
    assert "node_zfs_dataset_used_bytes" not in samples
    assert value(samples, "node_zfs_zpool_size_bytes", pool="rpool") == 996432412672


_ERRORS_TEMPLATE = """\
  pool: tank
 state: ONLINE
status: One or more devices has experienced an error resulting in data
\tcorruption.  Applications may be affected.
action: Restore the file in question if possible.  Otherwise restore the
\tentire pool from backup.
   see: https://openzfs.github.io/openzfs-docs/msg/ZFS-8000-8A
  scan: scrub repaired 0B in 00:10:02 with 2 errors on Sun Sep  8 00:34:03 2024
config:

\tNAME        STATE     READ WRITE CKSUM
\ttank        ONLINE       0     0     0
\t  sda       ONLINE       0     0     4

errors: {errors}
"""


@pytest.mark.parametrize(
    ("errors", "expected"),
    [
        ("No known data errors", 0),
        ("1 data error, use '-v' for a list", 1),
        ("12 data errors, use '-v' for a list", 12),
        # zpool status -v lists the files instead of a count.
        (
            "Permanent errors have been detected in the following files:\n\n"
            "        tank/data/vm-100-disk-0:<0x1>\n"
            "        /tank/media/movie.mkv\n",
            2,
        ),
        # The list can be unavailable; still never report a clean pool.
        ("Permanent errors have been detected in the following files:\n", 1),
        # Unknown wording (e.g. insufficient privileges): no value, not a false 0.
        ("List of errors unavailable: permission denied", None),
    ],
)
def test_data_errors_line_forms(errors: str, expected: int | None) -> None:
    (pool,) = parse_zpool_status(_ERRORS_TEMPLATE.format(errors=errors))
    assert pool.data_errors == expected
