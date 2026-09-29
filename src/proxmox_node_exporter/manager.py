"""Runs collectors on their own schedule and serves their latest results.

Collection is decoupled from scraping: each collector runs in its own thread
at its configured interval and publishes an immutable snapshot.  A scrape
only serialises the snapshots, so it is fast, and an unauthenticated client
hammering ``/metrics`` can never make the exporter spawn processes.
"""

from __future__ import annotations

import logging
import os
import platform
import resource
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from . import __version__
from .collectors.base import Collector, list_dir, read_text
from .metrics import Batch, Family, MetricGroup

log = logging.getLogger(__name__)

M = MetricGroup("exporter")
COLLECTOR_ENABLED = M.gauge(
    "proxmox_exporter_collector_enabled",
    "Whether a collector is enabled on this host (supported and not disabled).",
    "collector",
)
COLLECTOR_SUCCESS = M.gauge(
    "proxmox_exporter_collector_success",
    "Whether the last run of the collector succeeded.",
    "collector",
)
COLLECTOR_DURATION = M.gauge(
    "proxmox_exporter_collector_duration_seconds",
    "Duration of the last collector run.",
    "collector",
)
COLLECTOR_LAST_SUCCESS = M.gauge(
    "proxmox_exporter_collector_last_success_timestamp_seconds",
    "Unix time of the last successful collector run.",
    "collector",
)
COLLECTOR_INTERVAL = M.gauge(
    "proxmox_exporter_collector_interval_seconds",
    "Configured interval between collector runs.",
    "collector",
)
COLLECTOR_RUNS = M.counter(
    "proxmox_exporter_collector_runs_total", "Collector runs since start.", "collector"
)
COLLECTOR_ERRORS = M.counter(
    "proxmox_exporter_collector_errors_total", "Failed collector runs since start.", "collector"
)
BUILD_INFO = M.gauge(
    "proxmox_exporter_build_info",
    "Exporter version; the value is always 1.",
    "version",
    "python_version",
)
PROCESS_CPU = M.counter(
    "process_cpu_seconds_total", "Total user and system CPU time spent by the exporter."
)
PROCESS_RSS = M.gauge("process_resident_memory_bytes", "Resident memory size of the exporter.")
PROCESS_VSZ = M.gauge("process_virtual_memory_bytes", "Virtual memory size of the exporter.")
PROCESS_OPEN_FDS = M.gauge("process_open_fds", "Number of open file descriptors.")
PROCESS_MAX_FDS = M.gauge("process_max_fds", "Maximum number of open file descriptors.")
PROCESS_START = M.gauge(
    "process_start_time_seconds", "Start time of the exporter since the Unix epoch."
)


@dataclass
class _State:
    collector: Collector
    interval: float
    families: list[Family] = field(default_factory=list)
    success: bool | None = None
    duration: float = 0.0
    last_success: float = 0.0
    last_success_monotonic: float = 0.0
    runs: int = 0
    errors: int = 0
    thread: threading.Thread | None = None

    @property
    def stale_after(self) -> float:
        return self.interval * 3 + 30


class Manager:
    def __init__(
        self,
        collectors: Sequence[Collector],
        enabled: dict[str, bool],
        default_interval: float,
        intervals: dict[str, float] | None = None,
        procfs: str = "/proc",
    ) -> None:
        intervals = intervals or {}
        self._states = [
            _State(c, intervals.get(c.name, c.default_interval or default_interval))
            for c in collectors
        ]
        self._enabled = dict(enabled)
        self._procfs = procfs
        self._lock = threading.Lock()
        self._stop = threading.Event()

    @property
    def collectors(self) -> list[Collector]:
        return [s.collector for s in self._states]

    def start(self) -> None:
        for state in self._states:
            state.thread = threading.Thread(
                target=self._loop, args=(state,), name=f"collector-{state.collector.name}"
            )
            state.thread.daemon = True
            state.thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        deadline = time.monotonic() + timeout
        for state in self._states:
            if state.thread is not None:
                state.thread.join(max(0.0, deadline - time.monotonic()))

    def healthy(self) -> bool:
        return all(s.thread is None or s.thread.is_alive() for s in self._states)

    def run_once(self, timeout: float = 120.0) -> bool:
        """Run every collector once, concurrently; True if all succeeded in time."""
        threads = [threading.Thread(target=self._run, args=(s,), daemon=True) for s in self._states]
        for t in threads:
            t.start()
        deadline = time.monotonic() + timeout
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))
        hung = [s.collector.name for s, t in zip(self._states, threads) if t.is_alive()]
        if hung:
            log.warning("collectors still running after %gs: %s", timeout, ", ".join(hung))
        return not hung and all(s.success for s in self._states)

    def _loop(self, state: _State) -> None:
        next_run = time.monotonic()
        while not self._stop.is_set():
            self._run(state)
            next_run += state.interval
            now = time.monotonic()
            if next_run < now:  # overran; don't try to catch up
                next_run = now + state.interval
            self._stop.wait(next_run - now)

    def _run(self, state: _State) -> None:
        name = state.collector.name
        batch = Batch()
        start = time.monotonic()
        error: BaseException | None = None
        try:
            state.collector.collect(batch)
        except Exception as exc:  # noqa: BLE001 - a collector must never kill its thread
            error = exc
        duration = time.monotonic() - start
        with self._lock:
            previous = state.success
            state.runs += 1
            state.duration = duration
            if error is None:
                state.families = batch.families()
                state.success = True
                state.last_success = time.time()
                state.last_success_monotonic = time.monotonic()
            else:
                state.families = []
                state.success = False
                state.errors += 1
        if error is not None:
            level = logging.WARNING if previous is not False else logging.DEBUG
            log.log(
                level,
                "collector %s failed: %s",
                name,
                error,
                # Outside the except block: pass the exception, not True.
                exc_info=error if log.isEnabledFor(logging.DEBUG) else None,
            )
        elif previous is False:
            log.info("collector %s recovered", name)
        else:
            log.debug("collector %s: %d samples in %.3fs", name, len(batch), duration)

    def gather(self) -> list[Family]:
        now = time.monotonic()
        out = Batch()
        families: list[Family] = []
        with self._lock:
            for state in self._states:
                name = state.collector.name
                fresh = now - state.last_success_monotonic <= state.stale_after
                if state.success and fresh:
                    families.extend(state.families)
                out.add(COLLECTOR_SUCCESS, 1 if state.success and fresh else 0, collector=name)
                out.add(COLLECTOR_DURATION, state.duration, collector=name)
                out.add(COLLECTOR_INTERVAL, state.interval, collector=name)
                out.add(COLLECTOR_RUNS, state.runs, collector=name)
                out.add(COLLECTOR_ERRORS, state.errors, collector=name)
                if state.last_success:
                    out.add(COLLECTOR_LAST_SUCCESS, state.last_success, collector=name)
        for name, enabled in sorted(self._enabled.items()):
            out.add(COLLECTOR_ENABLED, 1 if enabled else 0, collector=name)
        out.add(BUILD_INFO, 1, version=__version__, python_version=platform.python_version())
        self._process_metrics(out)
        families.extend(out.families())
        return families

    def _process_metrics(self, out: Batch) -> None:
        stat = read_text(os.path.join(self._procfs, "self", "stat"))
        if stat:
            fields = stat[stat.rfind(")") + 2 :].split()
            ticks = os.sysconf("SC_CLK_TCK")
            page = resource.getpagesize()
            out.add(PROCESS_CPU, (int(fields[11]) + int(fields[12])) / ticks)
            out.add(PROCESS_VSZ, int(fields[20]))
            out.add(PROCESS_RSS, int(fields[21]) * page)
            boot = _boot_time(self._procfs)
            if boot is not None:
                out.add(PROCESS_START, boot + int(fields[19]) / ticks)
        out.add(PROCESS_OPEN_FDS, len(list_dir(os.path.join(self._procfs, "self", "fd"))))
        out.add(PROCESS_MAX_FDS, resource.getrlimit(resource.RLIMIT_NOFILE)[0])


def _boot_time(procfs: str) -> int | None:
    text = read_text(os.path.join(procfs, "stat")) or ""
    for line in text.splitlines():
        if line.startswith("btime "):
            return int(line.split()[1])
    return None
