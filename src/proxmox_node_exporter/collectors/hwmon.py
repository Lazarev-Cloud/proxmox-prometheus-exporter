"""Hardware sensors (temperatures, fans, voltages, current, power) from sysfs.

Reads /sys/class/hwmon directly, so lm-sensors is not required (running
``sensors-detect`` once still helps the kernel load the right drivers).
"""

from __future__ import annotations

import os
import re

from ..metrics import Batch, MetricGroup
from .base import Collector, list_dir, read_int, read_text

M = MetricGroup("hwmon")
_T = ("chip", "device", "sensor", "label")
_S = ("chip", "device", "sensor")
TEMP = M.gauge("node_hwmon_temp_celsius", "Temperature.", *_T)
TEMP_MAX = M.gauge("node_hwmon_temp_max_celsius", "High temperature threshold.", *_T)
TEMP_CRIT = M.gauge("node_hwmon_temp_crit_celsius", "Critical temperature threshold.", *_T)
TEMP_ALARM = M.gauge("node_hwmon_temp_alarm", "Temperature alarm raised by the chip.", *_T)
FAN = M.gauge("node_hwmon_fan_rpm", "Fan speed.", *_S)
FAN_MIN = M.gauge("node_hwmon_fan_min_rpm", "Minimum fan speed threshold.", *_S)
VOLTAGE = M.gauge("node_hwmon_voltage_volts", "Voltage.", *_S)
CURRENT = M.gauge("node_hwmon_curr_amps", "Current.", *_S)
POWER = M.gauge("node_hwmon_power_watt", "Power.", *_S)

_MAX_PLAUSIBLE_MILLI_C = 200_000
_SENSOR_RE = re.compile(r"^(temp|fan|in|curr|power)(\d+)_(input|average)$")


class HwmonCollector(Collector):
    name = "hwmon"
    description = "Temperatures, fans, voltages and power from hardware monitoring chips"

    def detect(self) -> bool:
        return bool(list_dir(self.sys_path("class", "hwmon")))

    def collect(self, out: Batch) -> None:
        base = self.sys_path("class", "hwmon")
        for hwmon in list_dir(base):
            path = os.path.join(base, hwmon)
            sensors = self._sensor_files(path)
            if not sensors and os.path.isdir(os.path.join(path, "device")):
                path = os.path.join(path, "device")  # pre-3.x kernel layout
                sensors = self._sensor_files(path)
            if not sensors:
                continue
            chip = read_text(os.path.join(path, "name")) or hwmon
            device = _device_id(os.path.join(base, hwmon)) or hwmon
            seen: set[tuple[str, str]] = set()
            for kind, index, filename in sensors:
                self._sensor(out, path, chip, device, kind, index, filename, seen)

    @staticmethod
    def _sensor_files(path: str) -> list[tuple[str, str, str]]:
        found: dict[tuple[str, str], str] = {}
        for entry in list_dir(path):
            match = _SENSOR_RE.match(entry)
            if match:
                key = (match.group(1), match.group(2))
                # prefer instantaneous power over the average
                if key not in found or match.group(3) == "input":
                    found[key] = entry
        return [(kind, index, name) for (kind, index), name in sorted(found.items())]

    def _sensor(
        self,
        out: Batch,
        path: str,
        chip: str,
        device: str,
        kind: str,
        index: str,
        filename: str,
        seen: set[tuple[str, str]],
    ) -> None:
        prefix = os.path.join(path, f"{kind}{index}")
        label = read_text(f"{prefix}_label") or f"{kind}{index}"
        raw = read_int(os.path.join(path, filename))
        if raw is None:
            return
        sensor = label.replace(" ", "_").replace(".", "_") if kind == "temp" else label
        if (kind, sensor) in seen:
            sensor = f"{kind}{index}"  # labels can repeat on one chip (dell_smm: "Other")
        seen.add((kind, sensor))
        if kind == "temp":
            labels = {"chip": chip, "device": device, "sensor": sensor, "label": label}
            out.add(TEMP, raw / 1000, **labels)
            for suffix, spec in (("max", TEMP_MAX), ("crit", TEMP_CRIT)):
                value = read_int(f"{prefix}_{suffix}")
                # Drivers report "unset" thresholds as 0 or absurd values
                # (NVMe: 65261.85 °C); only plausible ones are exported.
                if value is not None and 0 < value < _MAX_PLAUSIBLE_MILLI_C:
                    out.add(spec, value / 1000, **labels)
            # Drivers expose the plain, critical and/or emergency alarm flag.
            alarms = [
                read_int(f"{prefix}_{name}") for name in ("alarm", "crit_alarm", "emergency_alarm")
            ]
            present = [a for a in alarms if a is not None]
            out.add(TEMP_ALARM, max(present) if present else None, **labels)
            return
        labels = {"chip": chip, "device": device, "sensor": sensor}
        if kind == "fan":
            out.add(FAN, raw, **labels)
            out.add(FAN_MIN, read_int(f"{prefix}_min"), **labels)
        elif kind == "in":
            out.add(VOLTAGE, raw / 1000, **labels)
        elif kind == "curr":
            out.add(CURRENT, raw / 1000, **labels)
        elif kind == "power":
            out.add(POWER, raw / 1_000_000, **labels)


def _device_id(hwmon_path: str) -> str:
    """Stable name of the device behind a hwmon node (e.g. nvme0, coretemp.0)."""
    link = os.path.join(hwmon_path, "device")
    if not os.path.exists(link):
        return ""
    return os.path.basename(os.path.realpath(link))
