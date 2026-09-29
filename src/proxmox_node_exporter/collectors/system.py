"""CPU, memory, load, pressure, kernel counters and host identity.

Metric names and semantics follow the Prometheus node_exporter where they
overlap, so its dashboards and recording rules work with this exporter too.
"""

from __future__ import annotations

import os
import platform
import re
import time

from ..metrics import Batch, MetricGroup
from .base import Collector, Context, list_dir, read_int, read_text

M = MetricGroup("system")
INFO = M.gauge(
    "node_info",
    "Host identity; the value is always 1.",
    "hostname",
    "kernel",
    "os",
    "architecture",
    "cpu_model",
)
BOOT_TIME = M.gauge("node_boot_time_seconds", "Boot time as a Unix timestamp.")
UPTIME = M.gauge("node_uptime_seconds", "Seconds since boot.")
TIME = M.gauge("node_time_seconds", "System time as a Unix timestamp.")
TZ_OFFSET = M.gauge("node_time_zone_offset_seconds", "Local time zone offset from UTC.")

CPU_SECONDS = M.counter(
    "node_cpu_seconds_total", "Seconds the CPUs spent in each mode.", "cpu", "mode"
)
CPU_GUEST_SECONDS = M.counter(
    "node_cpu_guest_seconds_total",
    "Seconds the CPUs spent running guests (already included in user/nice).",
    "cpu",
    "mode",
)
CPU_USAGE = M.gauge(
    "node_cpu_usage_percent", "CPU busy percentage since the previous collection.", "cpu"
)
CPU_COUNT = M.gauge("node_cpu_count", "Number of CPUs (logical threads or physical cores).", "type")
CPU_FREQ = M.gauge("node_cpu_frequency_hertz", "CPU frequency (current, min, max).", "cpu", "type")
CPU_THROTTLES = M.counter("node_cpu_throttles_total", "Thermal throttling events.", "cpu", "type")
LOAD1 = M.gauge("node_load1", "1 minute load average.")
LOAD5 = M.gauge("node_load5", "5 minute load average.")
LOAD15 = M.gauge("node_load15", "15 minute load average.")

PROCS_RUNNING = M.gauge("node_procs_running", "Runnable processes.")
PROCS_BLOCKED = M.gauge("node_procs_blocked", "Processes blocked on I/O.")
PROCS_TOTAL = M.gauge("node_procs_total", "Number of processes.")
THREADS_TOTAL = M.gauge("node_threads_total", "Number of kernel scheduling entities (threads).")
FORKS = M.counter("node_forks_total", "Processes created since boot.")
CONTEXT_SWITCHES = M.counter("node_context_switches_total", "Context switches since boot.")
INTERRUPTS = M.counter("node_intr_total", "Interrupts serviced since boot.")

FD_ALLOCATED = M.gauge("node_filefd_allocated", "Allocated file handles.")
FD_MAXIMUM = M.gauge("node_filefd_maximum", "Maximum number of file handles.")
ENTROPY = M.gauge("node_entropy_available_bits", "Available kernel entropy.")

MEMORY_PRESSURE = M.gauge(
    "node_memory_pressure_ratio", "Share of memory in use: 1 - MemAvailable / MemTotal."
)
SWAP_USED_PERCENT = M.gauge("node_memory_swap_used_percent", "Percentage of swap in use.")
MEMORY_SHARED = M.gauge("node_memory_Shared_bytes", "Shared memory (Shmem); kept for 2.x.")

# /proc/meminfo fields exported as node_memory_<Field>[_bytes].
_MEMINFO_FIELDS = (
    "MemTotal", "MemFree", "MemAvailable", "Buffers", "Cached", "SwapCached",
    "Active", "Inactive", "Active_anon", "Inactive_anon", "Active_file", "Inactive_file",
    "Unevictable", "Mlocked", "SwapTotal", "SwapFree", "Dirty", "Writeback", "AnonPages",
    "Mapped", "Shmem", "KReclaimable", "Slab", "SReclaimable", "SUnreclaim", "KernelStack",
    "PageTables", "CommitLimit", "Committed_AS", "VmallocUsed", "AnonHugePages",
    "HardwareCorrupted", "Hugepagesize", "HugePages_Total", "HugePages_Free",
    "HugePages_Rsvd", "HugePages_Surp",
)  # fmt: skip
_MEMINFO = {
    field: (
        M.gauge(f"node_memory_{field}", f"Memory information field {field}.")
        if field.startswith("HugePages_")
        else M.gauge(f"node_memory_{field}_bytes", f"Memory information field {field}.")
    )
    for field in _MEMINFO_FIELDS
}

_PRESSURE = {
    ("cpu", "some"): M.counter(
        "node_pressure_cpu_waiting_seconds_total", "Time some tasks waited for CPU."
    ),
    ("memory", "some"): M.counter(
        "node_pressure_memory_waiting_seconds_total", "Time some tasks waited for memory."
    ),
    ("memory", "full"): M.counter(
        "node_pressure_memory_stalled_seconds_total", "Time all tasks stalled on memory."
    ),
    ("io", "some"): M.counter(
        "node_pressure_io_waiting_seconds_total", "Time some tasks waited for I/O."
    ),
    ("io", "full"): M.counter(
        "node_pressure_io_stalled_seconds_total", "Time all tasks stalled on I/O."
    ),
    ("irq", "full"): M.counter(
        "node_pressure_irq_stalled_seconds_total", "Time all tasks stalled on IRQs."
    ),
}

_VMSTAT = {
    name: M.counter(f"node_vmstat_{name}_total", f"/proc/vmstat counter {name}.")
    for name in ("pgfault", "pgmajfault", "pgpgin", "pgpgout", "pswpin", "pswpout", "oom_kill")
}

# user nice system idle iowait irq softirq steal (guest time is part of user/nice)
_CPU_MODES = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")
_MEMINFO_RE = re.compile(r"^(\S+):\s+(\d+)(\s+kB)?$")


class SystemCollector(Collector):
    name = "system"
    description = "CPU, memory, load, pressure stall info, kernel counters, host info"

    OS_RELEASE_FILES = ("/etc/os-release", "/usr/lib/os-release")

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        self._prev_cpu: dict[str, tuple[float, float]] = {}
        self._ticks = float(os.sysconf("SC_CLK_TCK"))

    def detect(self) -> bool:
        return os.path.exists(self.proc_path("stat"))

    def collect(self, out: Batch) -> None:
        stat = read_text(self.proc_path("stat"))
        if stat is None:
            raise RuntimeError("cannot read /proc/stat")
        self._stat(out, stat)
        self._loadavg(out)
        self._meminfo(out)
        self._pressure(out)
        self._vmstat(out)
        self._cpu_sysfs(out)
        self._misc(out)

    def _stat(self, out: Batch, text: str) -> None:
        logical = 0
        for line in text.splitlines():
            fields = line.split()
            if not fields:
                continue
            key = fields[0]
            if key.startswith("cpu") and key != "cpu":
                logical += 1
                cpu = key[3:]
                values = [float(v) / self._ticks for v in fields[1:]]
                values += [0.0] * (10 - len(values))
                for mode, value in zip(_CPU_MODES, values):
                    out.add(CPU_SECONDS, value, cpu=cpu, mode=mode)
                out.add(CPU_GUEST_SECONDS, values[8], cpu=cpu, mode="user")
                out.add(CPU_GUEST_SECONDS, values[9], cpu=cpu, mode="nice")
                total = sum(values[:8])
                idle = values[3] + values[4]
                prev = self._prev_cpu.get(cpu)
                self._prev_cpu[cpu] = (total, idle)
                if prev is not None and total > prev[0]:
                    busy = 1 - (idle - prev[1]) / (total - prev[0])
                    out.add(CPU_USAGE, round(max(0.0, min(1.0, busy)) * 100, 2), cpu=cpu)
            elif key == "intr":
                out.add(INTERRUPTS, int(fields[1]))
            elif key == "ctxt":
                out.add(CONTEXT_SWITCHES, int(fields[1]))
            elif key == "btime":
                out.add(BOOT_TIME, int(fields[1]))
            elif key == "processes":
                out.add(FORKS, int(fields[1]))
            elif key == "procs_running":
                out.add(PROCS_RUNNING, int(fields[1]))
            elif key == "procs_blocked":
                out.add(PROCS_BLOCKED, int(fields[1]))
        out.add(CPU_COUNT, logical, type="logical")

    def _loadavg(self, out: Batch) -> None:
        text = read_text(self.proc_path("loadavg"))
        if not text:
            return
        fields = text.split()
        out.add(LOAD1, float(fields[0]))
        out.add(LOAD5, float(fields[1]))
        out.add(LOAD15, float(fields[2]))
        if "/" in fields[3]:
            out.add(THREADS_TOTAL, int(fields[3].split("/")[1]))
        out.add(PROCS_TOTAL, sum(1 for d in list_dir(self.settings.procfs) if d.isdigit()))

    def _meminfo(self, out: Batch) -> None:
        text = read_text(self.proc_path("meminfo")) or ""
        values: dict[str, int] = {}
        for line in text.splitlines():
            match = _MEMINFO_RE.match(line.strip())
            if not match:
                continue
            key = match.group(1).replace("(", "_").replace(")", "")
            values[key] = int(match.group(2)) * (1024 if match.group(3) else 1)
        for key, value in values.items():
            spec = _MEMINFO.get(key)
            if spec is not None:
                out.add(spec, value)
        if "Shmem" in values:
            out.add(MEMORY_SHARED, values["Shmem"])
        total = values.get("MemTotal")
        if total and "MemAvailable" in values:
            out.add(MEMORY_PRESSURE, 1 - values["MemAvailable"] / total)
        swap_total = values.get("SwapTotal")
        if swap_total is not None and "SwapFree" in values:
            used = (swap_total - values["SwapFree"]) / swap_total * 100 if swap_total else 0.0
            out.add(SWAP_USED_PERCENT, used)

    def _pressure(self, out: Batch) -> None:
        for resource in ("cpu", "memory", "io", "irq"):
            text = read_text(self.proc_path("pressure", resource))
            if not text:
                continue
            for line in text.splitlines():
                kind, _, rest = line.partition(" ")
                spec = _PRESSURE.get((resource, kind))
                match = re.search(r"total=(\d+)", rest)
                if spec is not None and match:
                    out.add(spec, int(match.group(1)) / 1e6)

    def _vmstat(self, out: Batch) -> None:
        for line in (read_text(self.proc_path("vmstat")) or "").splitlines():
            key, _, value = line.partition(" ")
            spec = _VMSTAT.get(key)
            if spec is not None:
                out.add(spec, int(value))

    def _cpu_sysfs(self, out: Batch) -> None:
        base = self.sys_path("devices", "system", "cpu")
        siblings = set()
        for entry in list_dir(base):
            if not re.match(r"^cpu\d+$", entry):
                continue
            cpu = entry[3:]
            path = os.path.join(base, entry)
            topology = read_text(os.path.join(path, "topology", "thread_siblings_list"))
            if topology:
                siblings.add(topology)
            freq = os.path.join(path, "cpufreq")
            for kind, filename in (
                ("current", "scaling_cur_freq"),
                ("min", "cpuinfo_min_freq"),
                ("max", "cpuinfo_max_freq"),
            ):
                khz = read_int(os.path.join(freq, filename))
                if khz is not None:
                    out.add(CPU_FREQ, khz * 1000, cpu=cpu, type=kind)
            for kind in ("core", "package"):
                count = read_int(os.path.join(path, "thermal_throttle", f"{kind}_throttle_count"))
                if count is not None:
                    out.add(CPU_THROTTLES, count, cpu=cpu, type=kind)
        if siblings:
            out.add(CPU_COUNT, len(siblings), type="physical")

    def _misc(self, out: Batch) -> None:
        uptime = read_text(self.proc_path("uptime"))
        if uptime:
            out.add(UPTIME, float(uptime.split()[0]))
        now = time.time()
        out.add(TIME, now)
        out.add(TZ_OFFSET, time.localtime(now).tm_gmtoff)
        file_nr = read_text(self.proc_path("sys", "fs", "file-nr"))
        if file_nr:
            fields = file_nr.split()
            out.add(FD_ALLOCATED, int(fields[0]))
            out.add(FD_MAXIMUM, int(fields[2]))
        out.add(ENTROPY, read_int(self.proc_path("sys", "kernel", "random", "entropy_avail")))
        uname = platform.uname()
        out.add(
            INFO,
            1,
            hostname=uname.node,
            kernel=uname.release,
            os=self._os_name(),
            architecture=uname.machine,
            cpu_model=self._cpu_model(),
        )

    def _os_name(self) -> str:
        for path in self.OS_RELEASE_FILES:
            text = read_text(path)
            if text:
                for line in text.splitlines():
                    if line.startswith("PRETTY_NAME="):
                        return line.split("=", 1)[1].strip().strip("\"'")
        return platform.system()

    def _cpu_model(self) -> str:
        for line in (read_text(self.proc_path("cpuinfo")) or "").splitlines():
            key, _, value = line.partition(":")
            if key.strip() in ("model name", "Model") and value.strip():
                return value.strip()
        return ""
