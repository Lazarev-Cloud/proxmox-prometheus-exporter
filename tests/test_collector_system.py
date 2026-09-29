from __future__ import annotations

import os
import platform
import time
from pathlib import Path
from typing import Callable

import pytest

from conftest import collect, value, write_tree
from proxmox_node_exporter.collectors.base import Context
from proxmox_node_exporter.collectors.system import SystemCollector

TICKS = float(os.sysconf("SC_CLK_TCK"))
MODES = {"user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal"}

# Four logical CPUs; the aggregate "cpu" line is the sum of the per-CPU lines.
# Fields: user nice system idle iowait irq softirq steal guest guest_nice
PROC_STAT = """\
cpu  10150 120 3050 2003400 1650 0 280 20 2450 40
cpu0 3000 20 900 500000 400 0 120 5 600 10
cpu1 2500 30 800 501000 350 0 60 5 650 10
cpu2 2400 40 700 501200 500 0 50 5 600 10
cpu3 2250 30 650 501200 400 0 50 5 600 10
intr 123456789 35 9 0 0 0 0 0 0 0 1 0 0 156 0 0 0 0 0 0 0
ctxt 987654321
btime 1727600000
processes 4567890
procs_running 3
procs_blocked 1
softirq 45678901 12 3456789 23 456789 12345 0 34567 1234567 0 2345678
"""

# Second sample: cpu0 ran 150 ticks of user time (100 of them guest time,
# which the kernel also adds to guest), 50 system, 250 idle, 50 iowait;
# cpu1 was idle; cpu2's iowait went backwards (a known kernel quirk); cpu3
# did not change at all.
PROC_STAT_2 = """\
cpu  10390 120 3100 2004150 1690 0 280 20 2550 40
cpu0 3150 20 950 500250 450 0 120 5 700 10
cpu1 2500 30 800 501500 350 0 60 5 650 10
cpu2 2490 40 700 501200 490 0 50 5 600 10
cpu3 2250 30 650 501200 400 0 50 5 600 10
intr 123460000 35 9 0 0 0 0 0 0 0 1 0 0 156 0 0 0 0 0 0 0
ctxt 987660000
btime 1727600000
processes 4567999
procs_running 1
procs_blocked 0
softirq 45679999 12 3456789 23 456789 12345 0 34567 1234567 0 2345678
"""

MEMINFO = """\
MemTotal:       65755380 kB
MemFree:         2345678 kB
MemAvailable:   32877690 kB
Buffers:          123456 kB
Cached:          9876543 kB
SwapCached:          512 kB
Active:         20000000 kB
Inactive:        8000000 kB
Active(anon):   15000000 kB
Inactive(anon):   500000 kB
Active(file):    5000000 kB
Inactive(file):  7500000 kB
Unevictable:       65432 kB
Mlocked:           65432 kB
SwapTotal:       8388604 kB
SwapFree:        6291453 kB
Zswap:                 0 kB
Zswapped:              0 kB
Dirty:              1234 kB
Writeback:             0 kB
AnonPages:      15500000 kB
Mapped:           456789 kB
Shmem:            234567 kB
KReclaimable:     345678 kB
Slab:            1234567 kB
SReclaimable:     345678 kB
SUnreclaim:       888889 kB
KernelStack:       23456 kB
PageTables:        67890 kB
SecPageTables:         0 kB
NFS_Unstable:          0 kB
Bounce:                0 kB
WritebackTmp:          0 kB
CommitLimit:    41266292 kB
Committed_AS:   30123456 kB
VmallocTotal:   34359738367 kB
VmallocUsed:      234567 kB
VmallocChunk:          0 kB
Percpu:            12345 kB
HardwareCorrupted:     0 kB
AnonHugePages:   8388608 kB
ShmemHugePages:        0 kB
ShmemPmdMapped:        0 kB
FileHugePages:         0 kB
FilePmdMapped:         0 kB
Unaccepted:            0 kB
HugePages_Total:      16
HugePages_Free:        4
HugePages_Rsvd:        2
HugePages_Surp:        0
Hugepagesize:       2048 kB
Hugetlb:           32768 kB
DirectMap4k:      567890 kB
DirectMap2M:    12345678 kB
DirectMap1G:    56623104 kB
"""

PRESSURE = {
    "proc/pressure/cpu": (
        "some avg10=1.53 avg60=0.87 avg300=0.40 total=123456789\n"
        "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
    ),
    "proc/pressure/memory": (
        "some avg10=0.00 avg60=0.02 avg300=0.01 total=4567890\n"
        "full avg10=0.00 avg60=0.01 avg300=0.00 total=3456789\n"
    ),
    "proc/pressure/io": (
        "some avg10=0.25 avg60=0.10 avg300=0.05 total=98765432\n"
        "full avg10=0.20 avg60=0.08 avg300=0.04 total=87654321\n"
    ),
    "proc/pressure/irq": "full avg10=0.00 avg60=0.00 avg300=0.00 total=2345678\n",
}

VMSTAT = """\
nr_free_pages 586419
nr_zone_inactive_anon 125000
nr_dirty 308
pgpgin 123456789
pgpgout 234567890
pswpin 1234
pswpout 5678
pgalloc_normal 987654321
pgfault 3456789012
pgmajfault 45678
pgrefill 0
oom_kill 2
"""

CPUINFO = """\
processor\t: 0
vendor_id\t: AuthenticAMD
cpu family\t: 23
model\t\t: 49
model name\t: AMD EPYC 7302P 16-Core Processor
stepping\t: 0
cpu MHz\t\t: 3000.000
cache size\t: 512 KB

processor\t: 1
vendor_id\t: AuthenticAMD
model name\t: AMD EPYC 7302P 16-Core Processor
"""

OS_RELEASE = """\
PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"
NAME="Debian GNU/Linux"
VERSION_ID="12"
ID=debian
"""


def _cpu_sysfs() -> dict[str, str]:
    files: dict[str, str] = {
        "sys/devices/system/cpu/online": "0-3\n",
        "sys/devices/system/cpu/possible": "0-3\n",
        "sys/devices/system/cpu/cpuidle/current_driver": "acpi_idle\n",
    }
    siblings = {0: "0,2", 1: "1,3", 2: "0,2", 3: "1,3"}
    for cpu, sibling_list in siblings.items():
        base = f"sys/devices/system/cpu/cpu{cpu}"
        files[f"{base}/topology/thread_siblings_list"] = sibling_list + "\n"
        files[f"{base}/topology/core_id"] = f"{cpu % 2}\n"
        files[f"{base}/thermal_throttle/core_throttle_count"] = f"{cpu}\n"
        files[f"{base}/thermal_throttle/package_throttle_count"] = "17\n"
    for policy, cur in ((0, 2800000), (1, 1500000), (2, 3000000)):
        base = f"sys/devices/system/cpu/cpufreq/policy{policy}"
        files[f"{base}/scaling_cur_freq"] = f"{cur}\n"
        files[f"{base}/cpuinfo_min_freq"] = "1500000\n"
        files[f"{base}/cpuinfo_max_freq"] = "3000000\n"
        files[f"{base}/scaling_governor"] = "performance\n"
    return files


def _host(tmp_path: Path) -> None:
    files = {
        "proc/stat": PROC_STAT,
        "proc/loadavg": "0.52 0.58 0.59 3/1234 56789\n",
        "proc/meminfo": MEMINFO,
        "proc/vmstat": VMSTAT,
        "proc/uptime": "12345.67 45678.90\n",
        "proc/sys/fs/file-nr": "4096\t0\t9223372036854775807\n",
        "proc/sys/kernel/random/entropy_avail": "256\n",
        "proc/cpuinfo": CPUINFO,
        "proc/1/comm": "systemd\n",
        "proc/42/comm": "kthreadd\n",
        "proc/1337/comm": "pvedaemon\n",
        "proc/net/dev": "",
        "os-release": OS_RELEASE,
        **PRESSURE,
        **_cpu_sysfs(),
    }
    write_tree(tmp_path, files)
    # cpuN/cpufreq is a symlink to the shared policy directory; cpu3 has none.
    for cpu in range(3):
        os.symlink(
            f"../cpufreq/policy{cpu}",
            tmp_path / "sys" / "devices" / "system" / "cpu" / f"cpu{cpu}" / "cpufreq",
        )


@pytest.fixture
def collector(
    tmp_path: Path, make_ctx: Callable[..., Context], monkeypatch: pytest.MonkeyPatch
) -> SystemCollector:
    _host(tmp_path)
    monkeypatch.setattr(SystemCollector, "OS_RELEASE_FILES", (str(tmp_path / "os-release"),))
    return SystemCollector(make_ctx())


def test_detect(tmp_path: Path, make_ctx: Callable[..., Context]) -> None:
    ctx = make_ctx()
    assert not SystemCollector(ctx).detect()
    write_tree(tmp_path, {"proc/stat": PROC_STAT})
    assert SystemCollector(ctx).detect()


def test_missing_proc_stat_raises(make_ctx: Callable[..., Context]) -> None:
    with pytest.raises(RuntimeError, match="/proc/stat"):
        collect(SystemCollector(make_ctx()))


def test_cpu_seconds_per_mode(collector: SystemCollector) -> None:
    samples = collect(collector)
    cpu = samples["node_cpu_seconds_total"]
    # Only per-CPU lines, no aggregate "cpu" series; exactly the 8 modes and
    # no guest modes (guest time is already part of user/nice).
    assert {dict(k)["cpu"] for k in cpu} == {"0", "1", "2", "3"}
    assert {dict(k)["mode"] for k in cpu} == MODES
    assert len(cpu) == 4 * 8
    expected = {
        "user": 3000,
        "nice": 20,
        "system": 900,
        "idle": 500000,
        "iowait": 400,
        "irq": 0,
        "softirq": 120,
        "steal": 5,
    }
    for mode, ticks in expected.items():
        assert value(samples, "node_cpu_seconds_total", cpu="0", mode=mode) == pytest.approx(
            ticks / TICKS
        )
    assert value(samples, "node_cpu_seconds_total", cpu="3", mode="user") == pytest.approx(
        2250 / TICKS
    )


def test_cpu_guest_seconds_are_separate(collector: SystemCollector) -> None:
    samples = collect(collector)
    assert value(samples, "node_cpu_guest_seconds_total", cpu="0", mode="user") == pytest.approx(
        600 / TICKS
    )
    assert value(samples, "node_cpu_guest_seconds_total", cpu="0", mode="nice") == pytest.approx(
        10 / TICKS
    )
    assert value(samples, "node_cpu_guest_seconds_total", cpu="1", mode="user") == pytest.approx(
        650 / TICKS
    )
    assert len(samples["node_cpu_guest_seconds_total"]) == 4 * 2


def test_cpu_usage_between_two_collections(tmp_path: Path, collector: SystemCollector) -> None:
    first = collect(collector)
    assert "node_cpu_usage_percent" not in first  # needs a previous sample

    (tmp_path / "proc" / "stat").write_text(PROC_STAT_2)
    second = collect(collector)
    usage = second["node_cpu_usage_percent"]
    # cpu0: 300 of 500 ticks idle+iowait -> 40 % busy.  Counting the 100
    # guest ticks a second time would give 1 - 300/600 = 50 %.
    assert value(second, "node_cpu_usage_percent", cpu="0") == 40.0
    assert value(second, "node_cpu_usage_percent", cpu="1") == 0.0
    # cpu2: iowait went backwards, the ratio is clamped to 100 %.
    assert value(second, "node_cpu_usage_percent", cpu="2") == 100.0
    # cpu3: no ticks elapsed, so there is nothing to report.
    assert len(usage) == 3

    # Counters keep reporting raw values; guest is still not part of them.
    assert value(second, "node_cpu_seconds_total", cpu="0", mode="user") == pytest.approx(
        3150 / TICKS
    )
    assert value(second, "node_cpu_guest_seconds_total", cpu="0", mode="user") == pytest.approx(
        700 / TICKS
    )

    # A third, identical sample: all deltas are zero.
    third = collect(collector)
    assert "node_cpu_usage_percent" not in third


def test_cpu_usage_rounding(tmp_path: Path, make_ctx: Callable[..., Context]) -> None:
    write_tree(tmp_path, {"proc/stat": "cpu0 0 0 0 0 0 0 0 0 0 0\n"})
    collector = SystemCollector(make_ctx())
    collect(collector)
    # 1 busy tick out of 3 -> 33.33 %
    write_tree(tmp_path, {"proc/stat": "cpu0 1 0 0 2 0 0 0 0 0 0\n"})
    assert value(collect(collector), "node_cpu_usage_percent", cpu="0") == 33.33


@pytest.mark.parametrize(
    ("line", "steal", "guest"),
    [
        # 2.6.0 - 2.6.10: no steal, guest or guest_nice
        ("cpu0 100 0 50 1000 10 0 0", 0.0, 0.0),
        # 2.6.11 - 2.6.23: steal, no guest
        ("cpu0 100 0 50 1000 10 0 0 7", 7.0, 0.0),
        # 2.6.24 - 2.6.32: guest, no guest_nice
        ("cpu0 100 0 50 1000 10 0 0 7 30", 7.0, 30.0),
    ],
)
def test_short_cpu_lines_from_old_kernels(
    tmp_path: Path, make_ctx: Callable[..., Context], line: str, steal: float, guest: float
) -> None:
    write_tree(tmp_path, {"proc/stat": f"cpu  100 0 50 1000 10 0 0\n{line}\nbtime 1\n"})
    samples = collect(SystemCollector(make_ctx()))
    assert value(samples, "node_cpu_seconds_total", cpu="0", mode="user") == pytest.approx(
        100 / TICKS
    )
    assert value(samples, "node_cpu_seconds_total", cpu="0", mode="steal") == pytest.approx(
        steal / TICKS
    )
    assert value(samples, "node_cpu_guest_seconds_total", cpu="0", mode="user") == pytest.approx(
        guest / TICKS
    )
    assert value(samples, "node_cpu_guest_seconds_total", cpu="0", mode="nice") == 0.0
    assert len(samples["node_cpu_seconds_total"]) == 8


def test_kernel_counters(collector: SystemCollector) -> None:
    samples = collect(collector)
    assert value(samples, "node_intr_total") == 123456789
    assert value(samples, "node_context_switches_total") == 987654321
    assert value(samples, "node_boot_time_seconds") == 1727600000
    assert value(samples, "node_forks_total") == 4567890
    assert value(samples, "node_procs_running") == 3
    assert value(samples, "node_procs_blocked") == 1


def test_cpu_count_and_sysfs(collector: SystemCollector) -> None:
    samples = collect(collector)
    assert value(samples, "node_cpu_count", type="logical") == 4
    # thread siblings "0,2" and "1,3" -> two physical cores
    assert value(samples, "node_cpu_count", type="physical") == 2

    # kHz -> Hz
    freq = samples["node_cpu_frequency_hertz"]
    assert value(samples, "node_cpu_frequency_hertz", cpu="0", type="current") == 2.8e9
    assert value(samples, "node_cpu_frequency_hertz", cpu="0", type="min") == 1.5e9
    assert value(samples, "node_cpu_frequency_hertz", cpu="0", type="max") == 3.0e9
    assert value(samples, "node_cpu_frequency_hertz", cpu="1", type="current") == 1.5e9
    assert {dict(k)["cpu"] for k in freq} == {"0", "1", "2"}  # cpu3 has no cpufreq

    assert value(samples, "node_cpu_throttles_total", cpu="2", type="core") == 2
    assert value(samples, "node_cpu_throttles_total", cpu="2", type="package") == 17
    assert len(samples["node_cpu_throttles_total"]) == 8


def test_no_cpu_sysfs(tmp_path: Path, make_ctx: Callable[..., Context]) -> None:
    write_tree(tmp_path, {"proc/stat": PROC_STAT})
    samples = collect(SystemCollector(make_ctx()))
    assert value(samples, "node_cpu_count", type="logical") == 4
    assert value(samples, "node_cpu_count", type="physical") is None
    assert "node_cpu_frequency_hertz" not in samples
    assert "node_cpu_throttles_total" not in samples


def test_loadavg_and_process_counts(collector: SystemCollector) -> None:
    samples = collect(collector)
    assert value(samples, "node_load1") == 0.52
    assert value(samples, "node_load5") == 0.58
    assert value(samples, "node_load15") == 0.59
    assert value(samples, "node_threads_total") == 1234
    # numeric entries in /proc: 1, 42, 1337 ("net", "sys", "pressure" are not)
    assert value(samples, "node_procs_total") == 3


def test_meminfo(collector: SystemCollector) -> None:
    samples = collect(collector)
    kib = 1024
    assert value(samples, "node_memory_MemTotal_bytes") == 65755380 * kib
    assert value(samples, "node_memory_MemAvailable_bytes") == 32877690 * kib
    assert value(samples, "node_memory_Active_anon_bytes") == 15000000 * kib
    assert value(samples, "node_memory_Inactive_file_bytes") == 7500000 * kib
    assert value(samples, "node_memory_Committed_AS_bytes") == 30123456 * kib
    assert value(samples, "node_memory_HardwareCorrupted_bytes") == 0
    assert value(samples, "node_memory_Hugepagesize_bytes") == 2048 * kib
    # HugePages_* are page counts: no unit, no _bytes suffix
    assert value(samples, "node_memory_HugePages_Total") == 16
    assert value(samples, "node_memory_HugePages_Free") == 4
    assert value(samples, "node_memory_HugePages_Rsvd") == 2
    assert value(samples, "node_memory_HugePages_Surp") == 0
    assert "node_memory_HugePages_Total_bytes" not in samples
    # fields outside the exported list are ignored
    assert "node_memory_VmallocTotal_bytes" not in samples
    assert "node_memory_DirectMap2M_bytes" not in samples
    assert value(samples, "node_memory_Shared_bytes") == 234567 * kib
    assert value(samples, "node_memory_pressure_ratio") == 0.5
    assert value(samples, "node_memory_swap_used_percent") == pytest.approx(
        (8388604 - 6291453) / 8388604 * 100
    )


def test_meminfo_without_swap(tmp_path: Path, make_ctx: Callable[..., Context]) -> None:
    write_tree(
        tmp_path,
        {
            "proc/stat": PROC_STAT,
            "proc/meminfo": (
                "MemTotal:        4028424 kB\nMemFree:          300000 kB\n"
                "MemAvailable:    3021318 kB\nSwapTotal:             0 kB\n"
                "SwapFree:              0 kB\n"
            ),
        },
    )
    samples = collect(SystemCollector(make_ctx()))
    assert value(samples, "node_memory_SwapTotal_bytes") == 0
    assert value(samples, "node_memory_swap_used_percent") == 0.0
    assert value(samples, "node_memory_pressure_ratio") == pytest.approx(1 - 3021318 / 4028424)


def test_pressure_stall_information(collector: SystemCollector) -> None:
    samples = collect(collector)
    # total= is in microseconds
    assert value(samples, "node_pressure_cpu_waiting_seconds_total") == pytest.approx(123.456789)
    assert value(samples, "node_pressure_memory_waiting_seconds_total") == pytest.approx(4.56789)
    assert value(samples, "node_pressure_memory_stalled_seconds_total") == pytest.approx(3.456789)
    assert value(samples, "node_pressure_io_waiting_seconds_total") == pytest.approx(98.765432)
    assert value(samples, "node_pressure_io_stalled_seconds_total") == pytest.approx(87.654321)
    assert value(samples, "node_pressure_irq_stalled_seconds_total") == pytest.approx(2.345678)


def test_no_pressure_without_psi(tmp_path: Path, make_ctx: Callable[..., Context]) -> None:
    write_tree(tmp_path, {"proc/stat": PROC_STAT})
    samples = collect(SystemCollector(make_ctx()))
    assert not [name for name in samples if name.startswith("node_pressure_")]


def test_vmstat(collector: SystemCollector) -> None:
    samples = collect(collector)
    assert value(samples, "node_vmstat_pgfault_total") == 3456789012
    assert value(samples, "node_vmstat_pgmajfault_total") == 45678
    assert value(samples, "node_vmstat_pgpgin_total") == 123456789
    assert value(samples, "node_vmstat_pgpgout_total") == 234567890
    assert value(samples, "node_vmstat_pswpin_total") == 1234
    assert value(samples, "node_vmstat_pswpout_total") == 5678
    assert value(samples, "node_vmstat_oom_kill_total") == 2
    assert "node_vmstat_nr_free_pages_total" not in samples


def test_misc_and_host_info(collector: SystemCollector) -> None:
    before = time.time()
    samples = collect(collector)
    after = time.time()
    assert value(samples, "node_uptime_seconds") == 12345.67
    assert value(samples, "node_filefd_allocated") == 4096
    assert value(samples, "node_filefd_maximum") == float(9223372036854775807)
    assert value(samples, "node_entropy_available_bits") == 256
    now = value(samples, "node_time_seconds")
    assert now is not None
    assert before <= now <= after
    assert "node_time_zone_offset_seconds" in samples

    uname = platform.uname()
    assert (
        value(
            samples,
            "node_info",
            hostname=uname.node,
            kernel=uname.release,
            os="Debian GNU/Linux 12 (bookworm)",
            architecture=uname.machine,
            cpu_model="AMD EPYC 7302P 16-Core Processor",
        )
        == 1
    )


def test_host_info_fallbacks(
    tmp_path: Path, make_ctx: Callable[..., Context], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SystemCollector, "OS_RELEASE_FILES", (str(tmp_path / "missing"),))
    write_tree(tmp_path, {"proc/stat": PROC_STAT, "proc/cpuinfo": "processor\t: 0\n"})
    info = collect(SystemCollector(make_ctx()))["node_info"]
    (labels,) = info
    assert dict(labels)["os"] == platform.system()
    assert dict(labels)["cpu_model"] == ""


def test_arm_cpu_model(
    tmp_path: Path, make_ctx: Callable[..., Context], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SystemCollector, "OS_RELEASE_FILES", ())
    cpuinfo = (
        "processor\t: 0\nBogoMIPS\t: 108.00\nCPU implementer\t: 0x41\n\n"
        "Hardware\t: BCM2835\nRevision\t: c03114\nSerial\t\t: 10000000abcdef01\n"
        "Model\t\t: Raspberry Pi 4 Model B Rev 1.4\n"
    )
    write_tree(tmp_path, {"proc/stat": PROC_STAT, "proc/cpuinfo": cpuinfo})
    (labels,) = collect(SystemCollector(make_ctx()))["node_info"]
    assert dict(labels)["cpu_model"] == "Raspberry Pi 4 Model B Rev 1.4"


def test_only_proc_stat(tmp_path: Path, make_ctx: Callable[..., Context]) -> None:
    write_tree(tmp_path, {"proc/stat": PROC_STAT})
    samples = collect(SystemCollector(make_ctx()))
    for name in ("node_load1", "node_threads_total", "node_uptime_seconds", "node_filefd_maximum"):
        assert name not in samples
    assert "node_memory_MemTotal_bytes" not in samples
    assert "node_info" in samples
    assert len(samples["node_cpu_seconds_total"]) == 32
