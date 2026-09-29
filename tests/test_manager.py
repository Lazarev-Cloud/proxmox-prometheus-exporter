"""Tests for the collector manager, using small fake collectors."""

from __future__ import annotations

import logging
import os
import platform
import resource
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Callable

import pytest
from prometheus_client.parser import text_string_to_metric_families

from proxmox_node_exporter import __version__
from proxmox_node_exporter.collectors.base import Collector, Context
from proxmox_node_exporter.config import Settings
from proxmox_node_exporter.manager import Manager
from proxmox_node_exporter.metrics import Batch, Family, MetricGroup, render
from proxmox_node_exporter.runner import Runner

M = MetricGroup("test_manager")
VALUE = M.gauge("test_manager_value", "A value reported by a fake collector.", "source")
LOGGER = "proxmox_node_exporter.manager"

Samples = dict[str, dict[frozenset[tuple[str, str]], float]]


def samples(families: Iterable[Family]) -> Samples:
    out: Samples = {}
    for family in families:
        labels = family.spec.labels
        out.setdefault(family.spec.name, {}).update(
            {frozenset(zip(labels, key)): value for key, value in family.samples.items()}
        )
    return out


def lbl(**labels: str) -> frozenset[tuple[str, str]]:
    return frozenset(labels.items())


def ctx() -> Context:
    return Context(Settings(), Runner(), is_root=False)


def wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class StaticCollector(Collector):
    name = "static"
    description = "Always succeeds."

    def __init__(self, value: float = 1.0) -> None:
        super().__init__(ctx())
        self.value = value
        self.calls = 0

    def collect(self, out: Batch) -> None:
        self.calls += 1
        out.add(VALUE, self.value, source=self.name)


class OtherCollector(StaticCollector):
    name = "other"
    description = "Also succeeds."
    default_interval = 60.0


class FailingCollector(Collector):
    name = "failing"
    description = "Always fails after producing a partial result."

    def __init__(self) -> None:
        super().__init__(ctx())
        self.calls = 0

    def collect(self, out: Batch) -> None:
        self.calls += 1
        out.add(VALUE, 99, source=self.name)
        raise RuntimeError(f"boom {self.calls}")


class ScriptedCollector(Collector):
    """Fails or succeeds according to a script of booleans."""

    name = "scripted"
    description = "Follows a script."

    def __init__(self, script: list[bool]) -> None:
        super().__init__(ctx())
        self.script = list(script)
        self.calls = 0

    def collect(self, out: Batch) -> None:
        self.calls += 1
        if not self.script.pop(0):
            raise RuntimeError(f"failure {self.calls}")
        out.add(VALUE, self.calls, source=self.name)


class BlockingCollector(Collector):
    name = "blocking"
    description = "Blocks until released."

    def __init__(self) -> None:
        super().__init__(ctx())
        self.release = threading.Event()
        self.started = threading.Event()

    def collect(self, out: Batch) -> None:
        self.started.set()
        self.release.wait(5)
        out.add(VALUE, 1, source=self.name)


# -- run_once and gather -------------------------------------------------------------


def test_run_once_success_and_self_metrics() -> None:
    before = time.time()
    manager = Manager([StaticCollector(7)], {"static": True, "zfs": False}, 15.0)
    assert manager.run_once() is True
    got = samples(manager.gather())
    name = lbl(collector="static")
    assert got["test_manager_value"] == {lbl(source="static"): 7.0}
    assert got["proxmox_exporter_collector_success"] == {name: 1.0}
    assert got["proxmox_exporter_collector_runs_total"] == {name: 1.0}
    assert got["proxmox_exporter_collector_errors_total"] == {name: 0.0}
    assert got["proxmox_exporter_collector_interval_seconds"] == {name: 15.0}
    assert 0 <= got["proxmox_exporter_collector_duration_seconds"][name] < 5
    assert before <= got["proxmox_exporter_collector_last_success_timestamp_seconds"][name]
    assert got["proxmox_exporter_collector_last_success_timestamp_seconds"][name] <= time.time()
    assert got["proxmox_exporter_build_info"] == {
        lbl(version=__version__, python_version=platform.python_version()): 1.0
    }


def test_run_once_reports_failure() -> None:
    failing = FailingCollector()
    manager = Manager([StaticCollector(), failing], {}, 15.0)
    assert manager.run_once() is False
    assert failing.calls == 1
    got = samples(manager.gather())
    # The partial batch of the failed run is discarded.
    assert got["test_manager_value"] == {lbl(source="static"): 1.0}
    assert got["proxmox_exporter_collector_success"] == {
        lbl(collector="static"): 1.0,
        lbl(collector="failing"): 0.0,
    }
    assert got["proxmox_exporter_collector_errors_total"][lbl(collector="failing")] == 1
    assert got["proxmox_exporter_collector_runs_total"][lbl(collector="failing")] == 1
    last_success = got["proxmox_exporter_collector_last_success_timestamp_seconds"]
    assert lbl(collector="failing") not in last_success


def test_run_once_runs_collectors_concurrently() -> None:
    blocking = BlockingCollector()
    static = StaticCollector()
    manager = Manager([blocking, static], {}, 15.0)
    result: list[bool] = []
    thread = threading.Thread(target=lambda: result.append(manager.run_once()))
    thread.start()
    try:
        assert blocking.started.wait(3)
        assert wait_until(lambda: static.calls == 1)  # not serialised behind "blocking"
        assert result == []
    finally:
        blocking.release.set()
        thread.join(5)
    assert result == [True]


def test_run_once_without_collectors() -> None:
    manager = Manager([], {"system": False}, 15.0)
    assert manager.run_once() is True
    got = samples(manager.gather())
    assert "proxmox_exporter_collector_success" not in got
    assert got["proxmox_exporter_collector_enabled"] == {lbl(collector="system"): 0.0}


def test_gather_before_first_run() -> None:
    manager = Manager([StaticCollector()], {"static": True}, 15.0)
    got = samples(manager.gather())
    assert "test_manager_value" not in got
    assert got["proxmox_exporter_collector_success"] == {lbl(collector="static"): 0.0}
    assert got["proxmox_exporter_collector_runs_total"] == {lbl(collector="static"): 0.0}
    assert "proxmox_exporter_collector_last_success_timestamp_seconds" not in got


def test_failure_after_success_drops_old_data() -> None:
    manager = Manager([ScriptedCollector([True, False, True])], {}, 15.0)
    assert manager.run_once() is True
    assert samples(manager.gather())["test_manager_value"] == {lbl(source="scripted"): 1.0}
    assert manager.run_once() is False
    got = samples(manager.gather())
    assert "test_manager_value" not in got
    assert got["proxmox_exporter_collector_success"] == {lbl(collector="scripted"): 0.0}
    # The last successful run is still reported.
    assert (
        lbl(collector="scripted")
        in (got["proxmox_exporter_collector_last_success_timestamp_seconds"])
    )
    assert manager.run_once() is True
    got = samples(manager.gather())
    assert got["test_manager_value"] == {lbl(source="scripted"): 3.0}
    assert got["proxmox_exporter_collector_runs_total"] == {lbl(collector="scripted"): 3.0}
    assert got["proxmox_exporter_collector_errors_total"] == {lbl(collector="scripted"): 1.0}


def test_stale_results_are_withheld() -> None:
    manager = Manager([StaticCollector(), OtherCollector()], {}, 10.0)
    assert manager.run_once() is True
    state = next(s for s in manager._states if s.collector.name == "static")
    assert state.stale_after == 10.0 * 3 + 30

    state.last_success_monotonic = time.monotonic() - state.stale_after + 5  # still fresh
    got = samples(manager.gather())
    assert lbl(source="static") in got["test_manager_value"]
    assert got["proxmox_exporter_collector_success"][lbl(collector="static")] == 1

    state.last_success_monotonic = time.monotonic() - state.stale_after - 1  # stuck collector
    got = samples(manager.gather())
    assert got["test_manager_value"] == {lbl(source="other"): 1.0}
    assert got["proxmox_exporter_collector_success"] == {
        lbl(collector="static"): 0.0,
        lbl(collector="other"): 1.0,
    }
    assert (
        lbl(collector="static")
        in (got["proxmox_exporter_collector_last_success_timestamp_seconds"])
    )


def test_intervals() -> None:
    manager = Manager(
        [StaticCollector(), OtherCollector(), FailingCollector()],
        {},
        20.0,
        intervals={"static": 5.0, "other": 7.0},
    )
    got = samples(manager.gather())["proxmox_exporter_collector_interval_seconds"]
    assert got == {
        lbl(collector="static"): 5.0,  # explicit override
        lbl(collector="other"): 7.0,  # override beats the collector's default
        lbl(collector="failing"): 20.0,  # global default
    }
    default = Manager([OtherCollector()], {}, 20.0)
    assert samples(default.gather())["proxmox_exporter_collector_interval_seconds"] == {
        lbl(collector="other"): 60.0  # collector's own default beats the global one
    }


def test_enabled_map_covers_all_collectors() -> None:
    enabled = {"static": True, "zfs": False, "smart": False, "gpu": True}
    manager = Manager([StaticCollector()], enabled, 15.0)
    got = samples(manager.gather())["proxmox_exporter_collector_enabled"]
    assert got == {lbl(collector=k): (1.0 if v else 0.0) for k, v in enabled.items()}


def test_gather_renders_valid_exposition() -> None:
    manager = Manager([StaticCollector(), FailingCollector()], {"static": True}, 15.0)
    manager.run_once()
    text = render(manager.gather()).decode()
    names = {f.name for f in text_string_to_metric_families(text)}
    assert {"test_manager_value", "proxmox_exporter_collector_success", "process_open_fds"} <= names
    assert "process_cpu_seconds" in names  # counters lose _total in the parser


# -- process metrics -----------------------------------------------------------------


def test_process_metrics_from_real_proc() -> None:
    got = samples(Manager([], {}, 15.0).gather())
    for name in (
        "process_cpu_seconds_total",
        "process_resident_memory_bytes",
        "process_virtual_memory_bytes",
        "process_open_fds",
        "process_max_fds",
        "process_start_time_seconds",
    ):
        assert name in got, name
    one = lbl()
    assert got["process_resident_memory_bytes"][one] > 1_000_000
    assert got["process_virtual_memory_bytes"][one] >= got["process_resident_memory_bytes"][one]
    assert got["process_open_fds"][one] >= 3
    assert got["process_max_fds"][one] == resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    assert got["process_start_time_seconds"][one] <= time.time() + 1
    assert got["process_start_time_seconds"][one] > time.time() - 7 * 86400


def test_process_metrics_parsing(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    (proc / "self" / "fd").mkdir(parents=True)
    for fd in ("0", "1", "2", "5"):
        (proc / "self" / "fd" / fd).write_text("")
    fields = ["S"] + ["0"] * 40
    fields[11], fields[12] = "250", "50"  # utime, stime (ticks)
    fields[19], fields[20], fields[21] = "500", "123456789", "2000"  # start, vsize, rss
    (proc / "self" / "stat").write_text("4242 (odd (name) x) " + " ".join(fields) + "\n")
    (proc / "stat").write_text("cpu  1 2 3 4\nbtime 1700000000\nprocesses 10\n")

    got = samples(Manager([], {}, 15.0, procfs=str(proc)).gather())
    ticks = os.sysconf("SC_CLK_TCK")
    one = lbl()
    assert got["process_cpu_seconds_total"] == {one: 300 / ticks}
    assert got["process_virtual_memory_bytes"] == {one: 123456789.0}
    assert got["process_resident_memory_bytes"] == {one: 2000.0 * resource.getpagesize()}
    assert got["process_start_time_seconds"] == {one: 1700000000 + 500 / ticks}
    assert got["process_open_fds"] == {one: 4.0}


def test_process_metrics_with_missing_procfs(tmp_path: Path) -> None:
    got = samples(Manager([], {}, 15.0, procfs=str(tmp_path / "missing")).gather())
    assert "process_cpu_seconds_total" not in got
    assert "process_start_time_seconds" not in got
    assert got["process_open_fds"] == {lbl(): 0.0}
    assert lbl() in got["process_max_fds"]


# -- background threads --------------------------------------------------------------


def test_start_stop_runs_repeatedly_and_stops_promptly() -> None:
    static = StaticCollector()
    manager = Manager([static], {}, 0.01)
    assert manager.healthy()  # not started yet
    manager.start()
    try:
        assert wait_until(lambda: static.calls >= 5)
        assert manager.healthy()
    finally:
        start = time.monotonic()
        manager.stop(timeout=3)
    assert time.monotonic() - start < 1.0
    thread = manager._states[0].thread
    assert thread is not None
    assert not thread.is_alive()
    assert thread.daemon
    assert thread.name == "collector-static"
    calls = static.calls
    time.sleep(0.05)
    assert static.calls == calls
    got = samples(manager.gather())
    assert got["proxmox_exporter_collector_runs_total"][lbl(collector="static")] == calls


def test_stop_interrupts_a_long_interval() -> None:
    static = StaticCollector()
    manager = Manager([static], {}, 3600.0)
    manager.start()
    assert wait_until(lambda: static.calls == 1)
    start = time.monotonic()
    manager.stop(timeout=3)
    assert time.monotonic() - start < 1.0
    assert static.calls == 1


def test_raising_collector_does_not_kill_its_thread() -> None:
    failing = FailingCollector()
    manager = Manager([failing], {"failing": True}, 0.01)
    manager.start()
    try:
        assert wait_until(lambda: failing.calls >= 5)
        assert manager.healthy()
        got = samples(manager.gather())
        runs = got["proxmox_exporter_collector_runs_total"][lbl(collector="failing")]
        errors = got["proxmox_exporter_collector_errors_total"][lbl(collector="failing")]
        assert runs >= 5
        assert errors == runs
        assert got["proxmox_exporter_collector_success"] == {lbl(collector="failing"): 0.0}
    finally:
        manager.stop(timeout=3)


def test_healthy_is_false_when_a_thread_died() -> None:
    manager = Manager([StaticCollector(), OtherCollector()], {}, 15.0)
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    manager._states[1].thread = dead
    assert not manager.healthy()


# -- logging -------------------------------------------------------------------------


def test_failure_and_recovery_logging(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    manager = Manager([ScriptedCollector([False, False, True, True, False])], {}, 15.0)

    def run() -> list[tuple[int, str]]:
        caplog.clear()
        manager.run_once()
        return [(r.levelno, r.getMessage()) for r in caplog.records if r.name == LOGGER]

    first = run()
    assert (logging.WARNING, "collector scripted failed: failure 1") in first
    second = run()
    assert [level for level, _ in second] == [logging.DEBUG]  # repeated failure: quiet
    assert second[0][1] == "collector scripted failed: failure 2"
    third = run()
    assert (logging.INFO, "collector scripted recovered") in third
    fourth = run()
    assert all(level == logging.DEBUG for level, _ in fourth)
    assert not any("recovered" in message for _, message in fourth)
    fifth = run()
    assert (logging.WARNING, "collector scripted failed: failure 5") in fifth


def test_failure_traceback_only_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER)
    Manager([FailingCollector()], {}, 15.0).run_once()
    (record,) = [r for r in caplog.records if r.name == LOGGER]
    assert not record.exc_info
    assert "Traceback" not in caplog.text

    caplog.clear()
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    Manager([FailingCollector()], {}, 15.0).run_once()
    (record,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert record.exc_info
    assert isinstance(record.exc_info[1], RuntimeError)
    assert "Traceback (most recent call last)" in caplog.text
    assert 'raise RuntimeError(f"boom {self.calls}")' in caplog.text


def test_run_once_gives_up_on_hung_collectors(caplog: pytest.LogCaptureFixture) -> None:
    blocking = BlockingCollector()
    manager = Manager([blocking, StaticCollector(1)], {}, 15.0)
    start = time.monotonic()
    try:
        assert manager.run_once(timeout=0.2) is False
    finally:
        blocking.release.set()
    assert time.monotonic() - start < 2.0
    assert "collectors still running after 0.2s: blocking" in caplog.text
