"""BMC sensors through ipmitool (server boards with IPMI)."""

from __future__ import annotations

import os

from ..metrics import Batch, MetricGroup
from .base import Collector, to_float

M = MetricGroup("ipmi")
VALUE = M.gauge("node_ipmi_sensor_value", "Reading of an IPMI sensor.", "name", "unit")
STATE = M.gauge(
    "node_ipmi_sensor_state",
    "Sensor status: 0 ok, 1 non-critical, 2 critical, 3 non-recoverable.",
    "name",
)
TEMP = M.gauge("node_ipmi_temperature_celsius", "IPMI temperature sensor.", "name")
FAN = M.gauge("node_ipmi_fan_speed_rpm", "IPMI fan sensor.", "name")
VOLTAGE = M.gauge("node_ipmi_voltage_volts", "IPMI voltage sensor.", "name")
POWER = M.gauge("node_ipmi_power_watts", "IPMI power sensor.", "name")
CURRENT = M.gauge("node_ipmi_current_amps", "IPMI current sensor.", "name")

_UNITS = {
    "degrees c": "celsius",
    "degrees f": "fahrenheit",
    "volts": "volts",
    "rpm": "rpm",
    "watts": "watts",
    "amps": "amps",
    "percent": "percent",
}
_TYPED = {"celsius": TEMP, "rpm": FAN, "volts": VOLTAGE, "watts": POWER, "amps": CURRENT}
_STATES = {"ok": 0, "nc": 1, "cr": 2, "nr": 3}


def parse_sensors(text: str) -> list[tuple[str, float | None, str, int | None]]:
    """Parse ``ipmitool sensor`` into (name, value, unit, state) with unique names."""
    rows = []
    seen: dict[str, int] = {}
    for line in text.splitlines():
        cols = [c.strip() for c in line.split("|")]
        if len(cols) < 4 or not cols[0]:
            continue
        name, value, unit, status = cols[:4]
        if unit.lower() == "discrete":
            continue
        seen[name] = seen.get(name, 0) + 1
        if seen[name] > 1:  # e.g. two "Temp" sensors on Dell boards
            name = f"{name}_{seen[name]}"
        rows.append(
            (
                name,
                to_float(value),
                _UNITS.get(unit.lower(), unit.lower().replace(" ", "_")),
                _STATES.get(status.lower()),
            )
        )
    return rows


class IpmiCollector(Collector):
    name = "ipmi"
    description = "BMC temperatures, fans, voltages, power and sensor states (ipmitool, root)"
    default_interval = 60.0

    DEVICES = ("/dev/ipmi0", "/dev/ipmi/0", "/dev/ipmidev/0")

    def detect(self) -> bool:
        return (
            self.ctx.is_root
            and self.has_command("ipmitool")
            and any(os.path.exists(d) for d in self.DEVICES)
        )

    def collect(self, out: Batch) -> None:
        result = self.run("ipmitool", "sensor", timeout=45.0)
        for name, value, unit, state in parse_sensors(result.stdout):
            out.add(STATE, state, name=name)
            if value is None:
                continue
            out.add(VALUE, value, name=name, unit=unit)
            spec = _TYPED.get(unit)
            if spec is not None:
                out.add(spec, value, name=name)
