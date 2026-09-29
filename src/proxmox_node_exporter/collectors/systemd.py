"""systemd system state and service states."""

from __future__ import annotations

import os
import re

from ..metrics import Batch, MetricGroup
from .base import Collector, Context

M = MetricGroup("systemd")
SYSTEM_RUNNING = M.gauge(
    "node_systemd_system_running", "Whether the system state is 'running' (not degraded)."
)
SYSTEM_STATE = M.gauge(
    "node_systemd_system_state", "systemd system state (one series per state).", "state"
)
UNITS = M.gauge("node_systemd_units", "Loaded units by active state.", "state")
UNIT_STATE = M.gauge(
    "node_systemd_unit_state",
    "Unit active state (one series per state; see --systemd.unit-include).",
    "name",
    "state",
    "type",
)

SYSTEM_STATES = (
    "initializing", "starting", "running", "degraded", "maintenance", "stopping", "offline",
    "unknown",
)  # fmt: skip
UNIT_STATES = ("active", "activating", "deactivating", "inactive", "failed")


class SystemdCollector(Collector):
    name = "systemd"
    description = "systemd system state, failed units and per-service state"
    default_interval = 30.0

    RUN_DIR = "/run/systemd/system"

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        self._include = re.compile(self.settings.systemd_unit_include)
        self._exclude = re.compile(self.settings.systemd_unit_exclude)

    def detect(self) -> bool:
        return os.path.isdir(self.RUN_DIR) and self.has_command("systemctl")

    def collect(self, out: Batch) -> None:
        state = self.run("systemctl", "is-system-running", ok_codes=None).stdout.strip()
        state = state if state in SYSTEM_STATES else "unknown"
        out.add(SYSTEM_RUNNING, 1 if state == "running" else 0)
        for candidate in SYSTEM_STATES:
            out.add(SYSTEM_STATE, 1 if candidate == state else 0, state=candidate)

        listing = self.run(
            "systemctl", "list-units", "--all", "--no-legend", "--no-pager", "--plain",
            timeout=20.0,
        )  # fmt: skip
        counts: dict[str, int] = dict.fromkeys(UNIT_STATES, 0)
        for line in listing.stdout.splitlines():
            fields = line.split()
            if fields and fields[0] in ("●", "*"):
                fields = fields[1:]
            if len(fields) < 4:
                continue
            unit, load, active = fields[0], fields[1], fields[2]
            if load != "loaded":
                continue
            counts[active] = counts.get(active, 0) + 1
            if not self._include.search(unit) or self._exclude.search(unit):
                continue
            kind = unit.rsplit(".", 1)[-1]
            for candidate in UNIT_STATES:
                out.add(
                    UNIT_STATE,
                    1 if candidate == active else 0,
                    name=unit,
                    state=candidate,
                    type=kind,
                )
        for active, count in counts.items():
            out.add(UNITS, count, state=active)
