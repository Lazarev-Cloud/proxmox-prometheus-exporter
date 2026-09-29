from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from conftest import FakeRunner, Samples, collect, fixture, value
from proxmox_node_exporter.collectors.base import Context
from proxmox_node_exporter.collectors.systemd import SYSTEM_STATES, UNIT_STATES, SystemdCollector
from proxmox_node_exporter.runner import CommandError, CommandResult

IS_RUNNING = ("systemctl", "is-system-running")
LIST_UNITS = ("systemctl", "list-units", "--all", "--no-legend", "--no-pager", "--plain")
# Services selected by the default include/exclude patterns: getty@, systemd-fsck@ and
# user@ instances are excluded, masked and not-found units are skipped.
SERVICES = {
    "chrony.service": "active",
    "corosync.service": "active",
    "postfix@-.service": "active",
    "pve-cluster.service": "active",
    "pve-guests.service": "active",
    "pvescheduler.service": "activating",
    "pvestatd.service": "active",
    "zfs-import@tank.service": "failed",
    "zfs-zed.service": "inactive",
}


def systemd_runner(
    state: str = "degraded\n", code: int = 1, listing: str = "systemd/list_units.txt"
) -> FakeRunner:
    return FakeRunner({IS_RUNNING: CommandResult(code, state, ""), LIST_UNITS: fixture(listing)})


def unit_states(samples: Samples) -> dict[tuple[str, str], str]:
    """{(unit, type): active state}, checking the one-hot encoding on the way."""
    seen: dict[tuple[str, str], dict[str, float]] = {}
    for key, val in samples.get("node_systemd_unit_state", {}).items():
        labels = dict(key)
        seen.setdefault((labels["name"], labels["type"]), {})[labels["state"]] = val
    result = {}
    for unit, states in seen.items():
        assert set(states) == set(UNIT_STATES), unit
        active = [s for s, v in states.items() if v == 1]
        assert len(active) == 1, unit
        assert sum(states.values()) == 1, unit
        result[unit] = active[0]
    return result


# -- detection -------------------------------------------------------------------------


def test_detect(
    make_ctx: Callable[..., Context], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run" / "systemd" / "system"
    monkeypatch.setattr(SystemdCollector, "RUN_DIR", str(run_dir))
    assert not SystemdCollector(make_ctx(systemd_runner())).detect()  # not booted with systemd
    run_dir.mkdir(parents=True)
    assert SystemdCollector(make_ctx(systemd_runner(), is_root=False)).detect()
    assert not SystemdCollector(make_ctx(FakeRunner())).detect()


# -- system state ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stdout", "code", "state"),
    [
        ("degraded\n", 1, "degraded"),
        ("running\n", 0, "running"),
        ("starting\n", 1, "starting"),
        ("maintenance\n", 1, "maintenance"),
        ("offline\n", 1, "offline"),
        ("", 1, "unknown"),  # "Failed to connect to bus" goes to stderr
        ("some-future-state\n", 1, "unknown"),
    ],
)
def test_system_state(make_ctx: Callable[..., Context], stdout: str, code: int, state: str) -> None:
    samples = collect(SystemdCollector(make_ctx(systemd_runner(stdout, code))))
    assert value(samples, "node_systemd_system_running") == (1 if state == "running" else 0)
    states = {dict(k)["state"]: v for k, v in samples["node_systemd_system_state"].items()}
    assert states == {s: (1 if s == state else 0) for s in SYSTEM_STATES}


# -- units -----------------------------------------------------------------------------


def test_commands(make_ctx: Callable[..., Context]) -> None:
    runner = systemd_runner()
    collect(SystemdCollector(make_ctx(runner)))
    assert runner.calls == [IS_RUNNING, LIST_UNITS]


@pytest.mark.parametrize("listing", ["list_units.txt", "list_units_bullets.txt"])
def test_units(make_ctx: Callable[..., Context], listing: str) -> None:
    samples = collect(SystemdCollector(make_ctx(systemd_runner(listing=f"systemd/{listing}"))))
    # Loaded units of every type; masked and not-found units are not counted.
    counts = {dict(k)["state"]: v for k, v in samples["node_systemd_units"].items()}
    assert counts == {"active": 16, "activating": 1, "deactivating": 0, "inactive": 3, "failed": 2}
    assert unit_states(samples) == {(unit, "service"): state for unit, state in SERVICES.items()}


def test_unit_include_and_exclude(make_ctx: Callable[..., Context]) -> None:
    ctx = make_ctx(
        systemd_runner(),
        systemd_unit_include=r".+\.(service|timer)$",
        systemd_unit_exclude=r"^pve",
    )
    states = unit_states(collect(SystemdCollector(ctx)))
    assert states[("systemd-tmpfiles-clean.timer", "timer")] == "failed"
    assert states[("zfs-import@tank.service", "service")] == "failed"
    assert states[("getty@tty1.service", "service")] == "active"
    assert not any(name.startswith("pve") for name, _ in states)
    assert ("dm-event.socket", "socket") not in states


def test_list_units_failure_fails_the_collector(make_ctx: Callable[..., Context]) -> None:
    runner = FakeRunner(
        {
            IS_RUNNING: CommandResult(1, "degraded\n", ""),
            LIST_UNITS: CommandError("systemctl: timed out after 20s"),
        }
    )
    with pytest.raises(CommandError):
        collect(SystemdCollector(make_ctx(runner)))
