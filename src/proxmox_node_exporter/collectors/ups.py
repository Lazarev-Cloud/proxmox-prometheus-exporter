"""UPS status from Network UPS Tools (upsc)."""

from __future__ import annotations

import logging
import re

from ..metrics import Batch, MetricGroup
from ..runner import CommandError
from .base import Collector, read_text, to_float

log = logging.getLogger(__name__)

M = MetricGroup("ups")
INFO = M.gauge(
    "node_ups_info", "UPS identity; the value is always 1.", "ups", "manufacturer", "model"
)
_VARIABLES = {
    "battery.charge": M.gauge("node_ups_battery_charge_percent", "Battery charge.", "ups"),
    "battery.runtime": M.gauge(
        "node_ups_battery_runtime_seconds", "Estimated runtime on battery.", "ups"
    ),
    "battery.voltage": M.gauge("node_ups_battery_voltage_volts", "Battery voltage.", "ups"),
    "input.voltage": M.gauge("node_ups_input_voltage_volts", "Input voltage.", "ups"),
    "output.voltage": M.gauge("node_ups_output_voltage_volts", "Output voltage.", "ups"),
    "ups.load": M.gauge("node_ups_load_percent", "Load, % of capacity.", "ups"),
    "ups.temperature": M.gauge("node_ups_temperature_celsius", "UPS temperature.", "ups"),
    "ups.realpower": M.gauge("node_ups_power_watts", "Real power output.", "ups"),
    "ups.realpower.nominal": M.gauge("node_ups_power_nominal_watts", "Nominal real power.", "ups"),
}
ON_BATTERY = M.gauge("node_ups_on_battery", "UPS is running on battery (OB).", "ups")
LOW_BATTERY = M.gauge("node_ups_low_battery", "UPS reports low battery (LB).", "ups")
REPLACE_BATTERY = M.gauge("node_ups_replace_battery", "UPS asks for a new battery (RB).", "ups")
ONLINE = M.gauge("node_ups_online", "UPS is on line power (OL).", "ups")

# UPS names never start with "-", so they cannot be mistaken for upsc options.
_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*(@[A-Za-z0-9_.:\[\]-]+)?")


def parse_upsc(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            values[key.strip()] = value.strip()
    return values


class UpsCollector(Collector):
    name = "ups"
    description = "UPS battery, load and power status via Network UPS Tools (upsc)"

    NUT_CONFIG = "/etc/nut/ups.conf"

    def detect(self) -> bool:
        if not self.has_command("upsc"):
            return False
        if self.settings.ups_targets:
            return True
        return bool(re.search(r"^\s*\[[^\]]+\]", read_text(self.NUT_CONFIG) or "", re.M))

    def targets(self) -> list[str]:
        if self.settings.ups_targets:
            names = list(self.settings.ups_targets)
        else:
            names = self.run("upsc", "-l", "localhost").stdout.split()
        return [n for n in names if _NAME_RE.fullmatch(n)]

    def collect(self, out: Batch) -> None:
        targets = self.targets()
        failures = 0
        for target in targets:
            try:
                values = parse_upsc(self.run("upsc", target).stdout)
            except CommandError as exc:
                log.debug("upsc %s: %s", target, exc)
                failures += 1
                continue
            # Keep "@host" so equally named UPSes on different servers stay apart.
            ups = target.removesuffix("@localhost")
            out.add(
                INFO,
                1,
                ups=ups,
                manufacturer=values.get("device.mfr") or values.get("ups.mfr", ""),
                model=values.get("device.model") or values.get("ups.model", ""),
            )
            for key, spec in _VARIABLES.items():
                out.add(spec, to_float(values.get(key)), ups=ups)
            flags = values.get("ups.status", "").split()
            if flags:
                out.add(ON_BATTERY, 1 if "OB" in flags else 0, ups=ups)
                out.add(LOW_BATTERY, 1 if "LB" in flags else 0, ups=ups)
                out.add(REPLACE_BATTERY, 1 if "RB" in flags else 0, ups=ups)
                out.add(ONLINE, 1 if "OL" in flags else 0, ups=ups)
        if targets and failures == len(targets):
            raise RuntimeError("upsc failed for every UPS")
