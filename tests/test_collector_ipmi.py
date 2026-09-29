from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from conftest import FakeRunner, Samples, collect, fixture, value
from proxmox_node_exporter.collectors.base import Context
from proxmox_node_exporter.collectors.ipmi import IpmiCollector, parse_sensors
from proxmox_node_exporter.runner import CommandError, CommandResult

SENSOR = ("ipmitool", "sensor")


def run(make_ctx: Callable[..., Context], name: str) -> Samples:
    runner = FakeRunner({SENSOR: fixture(f"ipmi/{name}")})
    samples = collect(IpmiCollector(make_ctx(runner)))
    assert runner.calls == [SENSOR]
    return samples


def names(samples: Samples, metric: str) -> set[str]:
    return {dict(k)["name"] for k in samples.get(metric, {})}


# -- detection -------------------------------------------------------------------------


@pytest.fixture
def bmc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    device = tmp_path / "dev" / "ipmi0"
    monkeypatch.setattr(
        IpmiCollector, "DEVICES", (str(tmp_path / "dev" / "ipmi" / "0"), str(device))
    )
    return device


def test_detect(make_ctx: Callable[..., Context], bmc: Path) -> None:
    runner = FakeRunner({SENSOR: ""})
    assert not IpmiCollector(make_ctx(runner)).detect()  # no BMC device
    bmc.parent.mkdir()
    bmc.touch()
    assert IpmiCollector(make_ctx(runner)).detect()
    assert not IpmiCollector(make_ctx(runner, is_root=False)).detect()
    assert not IpmiCollector(make_ctx(FakeRunner())).detect()  # no ipmitool


# -- parsing ---------------------------------------------------------------------------


def test_parse_sensors_skips_discrete_and_renames_duplicates() -> None:
    rows = parse_sensors(fixture("ipmi/sensor_dell.txt"))
    assert [row[0] for row in rows] == [
        "Fan1",
        "Fan2",
        "Fan3",
        "Fan4",
        "Inlet Temp",
        "Exhaust Temp",
        "Temp",
        "Temp_2",
        "Current 1",
        "Current 2",
        "Voltage 1",
        "Voltage 2",
        "Pwr Consumption",
        "CPU Usage",
    ]
    assert rows[7] == ("Temp_2", 91.0, "celsius", 2)
    assert rows[12] == ("Pwr Consumption", 154.0, "watts", 0)


def test_parse_sensors_units() -> None:
    text = (
        "Ambient Temp     | 71.600     | degrees F  | ok    | na        | na        | na        "
        "| na        | na        | na        \n"
        "Airflow          | 38.000     | CFM        | ok    | na        | na        | na        "
        "| na        | na        | na        \n"
        "Watchdog         | 0x0        | unspecified | ok    | na        | na        | na        "
        "| na        | na        | na        \n"
        "garbage without separators\n"
        "| 1.000 | Volts | ok\n"
    )
    assert parse_sensors(text) == [
        ("Ambient Temp", 71.6, "fahrenheit", 0),
        ("Airflow", 38.0, "cfm", 0),
        ("Watchdog", None, "unspecified", 0),
    ]


# -- collection ------------------------------------------------------------------------


def test_dell(make_ctx: Callable[..., Context]) -> None:
    samples = run(make_ctx, "sensor_dell.txt")
    assert value(samples, "node_ipmi_sensor_value", name="Fan1", unit="rpm") == 3720
    assert value(samples, "node_ipmi_fan_speed_rpm", name="Fan1") == 3720
    assert value(samples, "node_ipmi_fan_speed_rpm", name="Fan3") == 480
    assert value(samples, "node_ipmi_temperature_celsius", name="Inlet Temp") == 21
    assert value(samples, "node_ipmi_temperature_celsius", name="Exhaust Temp") == 33
    # Dell names both CPU temperatures "Temp"; the second one gets a suffix.
    assert value(samples, "node_ipmi_temperature_celsius", name="Temp") == 45
    assert value(samples, "node_ipmi_temperature_celsius", name="Temp_2") == 91
    assert value(samples, "node_ipmi_sensor_value", name="Temp_2", unit="celsius") == 91
    assert value(samples, "node_ipmi_current_amps", name="Current 1") == 0.6
    assert value(samples, "node_ipmi_voltage_volts", name="Voltage 2") == 232
    assert value(samples, "node_ipmi_power_watts", name="Pwr Consumption") == 154
    assert value(samples, "node_ipmi_sensor_value", name="CPU Usage", unit="percent") == 12
    assert names(samples, "node_ipmi_temperature_celsius") == {
        "Inlet Temp",
        "Exhaust Temp",
        "Temp",
        "Temp_2",
    }
    assert value(samples, "node_ipmi_sensor_state", name="Fan1") == 0
    assert value(samples, "node_ipmi_sensor_state", name="Fan3") == 1  # nc
    assert value(samples, "node_ipmi_sensor_state", name="Temp") == 0
    assert value(samples, "node_ipmi_sensor_state", name="Temp_2") == 2  # cr
    # Discrete sensors (redundancy, intrusion, presence) are not reported.
    assert names(samples, "node_ipmi_sensor_state") == names(samples, "node_ipmi_sensor_value")
    assert len(samples["node_ipmi_sensor_value"]) == 14
    units = {dict(k)["unit"] for k in samples["node_ipmi_sensor_value"]}
    assert units == {"rpm", "celsius", "amps", "volts", "watts", "percent"}


def test_supermicro(make_ctx: Callable[..., Context]) -> None:
    samples = run(make_ctx, "sensor_supermicro.txt")
    assert value(samples, "node_ipmi_temperature_celsius", name="CPU1 Temp") == 45
    assert value(samples, "node_ipmi_temperature_celsius", name="System Temp") == 31
    assert value(samples, "node_ipmi_voltage_volts", name="12V") == 12.192
    assert value(samples, "node_ipmi_voltage_volts", name="VBAT") == 2.1
    assert value(samples, "node_ipmi_sensor_state", name="VBAT") == 3  # nr
    assert value(samples, "node_ipmi_fan_speed_rpm", name="FAN1") == 1400
    # Absent CPU/fan: reading "na", status "na" -> no series at all.
    for absent in ("CPU2 Temp", "FAN3"):
        assert absent not in names(samples, "node_ipmi_sensor_value")
        assert absent not in names(samples, "node_ipmi_sensor_state")
        assert absent not in names(samples, "node_ipmi_temperature_celsius")
        assert absent not in names(samples, "node_ipmi_fan_speed_rpm")
    assert "PS1 Status" not in names(samples, "node_ipmi_sensor_state")


def test_ipmitool_failure_fails_the_collector(make_ctx: Callable[..., Context]) -> None:
    error = (
        "Could not open device at /dev/ipmi0 or /dev/ipmi/0 or /dev/ipmidev/0: "
        "No such file or directory\n"
    )
    runner = FakeRunner({SENSOR: CommandResult(1, "", error)})
    with pytest.raises(CommandError):
        collect(IpmiCollector(make_ctx(runner)))
