from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

import pytest

from conftest import collect, value, write_tree
from proxmox_node_exporter.collectors.base import Context
from proxmox_node_exporter.collectors.hwmon import HwmonCollector

Samples = dict[str, dict[frozenset[tuple[str, str]], float]]


def chip(
    root: Path,
    hwmon: str,
    device_dir: str,
    files: dict[str, str],
    *,
    link_device: bool = True,
    subdir: str = "hwmon",
) -> None:
    """Create a hwmon node below ``sys/devices/<device_dir>`` like the kernel does.

    ``/sys/class/hwmon/<hwmon>`` is a relative symlink to the node and the
    node's ``device`` entry a relative symlink back to the parent device.
    """
    sys = root / "sys"
    node = sys / "devices" / device_dir / subdir / hwmon if subdir else sys / "devices" / device_dir
    write_tree(node, {"uevent": "", **files})
    if link_device:
        os.symlink(os.path.relpath(sys / "devices" / device_dir, node), node / "device")
    (sys / "class" / "hwmon").mkdir(parents=True, exist_ok=True)
    os.symlink(os.path.relpath(node, sys / "class" / "hwmon"), sys / "class" / "hwmon" / hwmon)


def milli(**values: int) -> dict[str, str]:
    return {name: f"{v}\n" for name, v in values.items()}


def build_host(root: Path) -> None:
    # acpitz on an old kernel: no parent device, no labels
    chip(
        root,
        "hwmon0",
        "virtual/thermal/thermal_zone0",
        {"name": "acpitz\n", **milli(temp1_input=27800, temp1_crit=105000, temp2_input=29800)},
        link_device=False,
    )
    chip(
        root,
        "hwmon1",
        "platform/coretemp.0",
        {
            "name": "coretemp\n",
            "temp1_label": "Package id 0\n",
            "temp2_label": "Core 0\n",
            "temp6_label": "Core 4\n",
            **milli(
                temp1_input=45000,
                temp1_max=80000,
                temp1_crit=100000,
                temp1_crit_alarm=0,
                temp2_input=43000,
                temp2_max=80000,
                temp2_crit=100000,
                temp2_crit_alarm=0,
                temp6_input=44000,
                temp6_max=80000,
                temp6_crit=100000,
                temp6_crit_alarm=0,
            ),
        },
    )
    # two NVMe drives: same chip name, told apart by the device label
    for hwmon, pci, ctrl, composite, alarm in (
        ("hwmon2", "pci0000:00/0000:00:01.1/0000:01:00.0", "nvme0", 38850, 0),
        ("hwmon3", "pci0000:00/0000:00:01.2/0000:02:00.0", "nvme1", 41850, 1),
    ):
        chip(
            root,
            hwmon,
            f"{pci}/nvme/{ctrl}",
            {
                "name": "nvme\n",
                "temp1_label": "Composite\n",
                "temp2_label": "Sensor 1\n",
                **milli(
                    temp1_input=composite,
                    temp1_max=84850,
                    temp1_min=-273150,
                    temp1_crit=84850,
                    temp1_alarm=alarm,
                    temp2_input=composite,
                    temp2_max=65261850,
                    temp2_min=-273150,
                ),
            },
            subdir="",
        )
    chip(
        root,
        "hwmon4",
        "platform/nct6775.656",
        {
            "name": "nct6798\n",
            "temp1_label": "SYSTIN\n",
            "temp2_label": "CPUTIN\n",
            "temp7_label": "PECI Agent 0 Calibration\n",
            "temp8_label": "PCH_CHIP_TEMP\n",
            "temp8_input": "",  # read fails
            "intrusion0_alarm": "1\n",
            **milli(
                in0_input=1016,
                in1_input=1832,
                fan1_input=1245,
                fan1_min=300,
                fan1_alarm=0,
                fan2_input=0,
                fan2_min=0,
                pwm1=128,
                pwm1_enable=5,
                temp1_input=35000,
                temp1_max=80000,
                temp1_max_hyst=75000,
                temp1_type=4,
                temp1_alarm=0,
                temp2_input=40500,
                temp2_max=0,
                temp7_input=51000,
            ),
        },
    )
    chip(
        root,
        "hwmon6",
        "pci0000:00/0000:00:03.1/0000:09:00.0",
        {
            "name": "amdgpu\n",
            "in0_label": "vddgfx\n",
            "power1_label": "PPT\n",
            "temp1_label": "edge\n",
            "temp2_label": "junction\n",
            "temp3_label": "mem\n",
            **milli(
                in0_input=800,
                power1_average=23000000,
                power1_input=25000000,
                power1_cap=180000000,
                temp1_input=45000,
                temp1_crit=100000,
                temp1_crit_hyst=-273150,
                temp2_input=47000,
                temp2_crit=110000,
                temp3_input=52000,
                temp3_crit=105000,
            ),
        },
    )
    chip(
        root,
        "hwmon7",
        "pci0000:00/0000:00:1f.4/i2c-0/0-0058",
        {
            "name": "pmbus\n",
            "in1_label": "vin\n",
            "in2_label": "vout1\n",
            "curr1_label": "iin\n",
            "curr2_label": "iout1\n",
            "power1_label": "pin\n",
            "power2_label": "pout1\n",
            **milli(
                in1_input=230000,
                in2_input=12050,
                curr1_input=1250,
                curr2_input=20500,
                power1_input=287000000,
                power2_input=247000000,
                fan1_input=5400,
            ),
        },
    )
    # a wireless card with a hwmon node but no readable sensor
    chip(root, "hwmon9", "pci0000:00/0000:00:1c.0/0000:03:00.0", {"name": "iwlwifi_1\n"})


@pytest.fixture
def samples(tmp_path: Path, make_ctx: Callable[..., Context]) -> Samples:
    build_host(tmp_path)
    return collect(HwmonCollector(make_ctx()))


def temp(chip: str, device: str, label: str) -> dict[str, str]:
    return {
        "chip": chip,
        "device": device,
        "sensor": label.replace(" ", "_").replace(".", "_"),
        "label": label,
    }


def other(chip: str, device: str, sensor: str) -> dict[str, str]:
    return {"chip": chip, "device": device, "sensor": sensor}


def test_detect(tmp_path: Path, make_ctx: Callable[..., Context]) -> None:
    ctx = make_ctx()
    assert not HwmonCollector(ctx).detect()
    (tmp_path / "sys" / "class" / "hwmon").mkdir(parents=True)
    assert not HwmonCollector(ctx).detect()  # class present, no chips
    build_host(tmp_path)
    assert HwmonCollector(ctx).detect()


def test_nothing_to_collect(tmp_path: Path, make_ctx: Callable[..., Context]) -> None:
    assert collect(HwmonCollector(make_ctx())) == {}


def test_temperatures_with_labels(samples: Samples) -> None:
    package = temp("coretemp", "coretemp.0", "Package id 0")
    assert package["sensor"] == "Package_id_0"
    assert value(samples, "node_hwmon_temp_celsius", **package) == 45.0
    assert value(samples, "node_hwmon_temp_max_celsius", **package) == 80.0
    assert value(samples, "node_hwmon_temp_crit_celsius", **package) == 100.0
    # coretemp only has tempN_crit_alarm, which is exported as the alarm
    assert value(samples, "node_hwmon_temp_alarm", **package) == 0
    assert (
        value(samples, "node_hwmon_temp_celsius", **temp("coretemp", "coretemp.0", "Core 4"))
        == 44.0
    )

    assert (
        value(samples, "node_hwmon_temp_celsius", **temp("amdgpu", "0000:09:00.0", "junction"))
        == 47.0
    )
    assert (
        value(samples, "node_hwmon_temp_crit_celsius", **temp("amdgpu", "0000:09:00.0", "mem"))
        == 105.0
    )


def test_label_fallback_and_missing_device_link(samples: Samples) -> None:
    # no tempN_label: the sensor is named after the file; no "device" link:
    # the hwmon node name is used
    t1 = {"chip": "acpitz", "device": "hwmon0", "sensor": "temp1", "label": "temp1"}
    t2 = {"chip": "acpitz", "device": "hwmon0", "sensor": "temp2", "label": "temp2"}
    assert value(samples, "node_hwmon_temp_celsius", **t1) == 27.8
    assert value(samples, "node_hwmon_temp_crit_celsius", **t1) == 105.0
    assert value(samples, "node_hwmon_temp_celsius", **t2) == 29.8
    assert value(samples, "node_hwmon_temp_crit_celsius", **t2) is None
    # voltages without inN_label
    assert (
        value(samples, "node_hwmon_voltage_volts", **other("nct6798", "nct6775.656", "in0"))
        == 1.016
    )
    assert (
        value(samples, "node_hwmon_voltage_volts", **other("nct6798", "nct6775.656", "in1"))
        == 1.832
    )
    assert value(samples, "node_hwmon_fan_rpm", **other("pmbus", "0-0058", "fan1")) == 5400


def test_same_chip_name_on_two_devices(samples: Samples) -> None:
    nvme0 = temp("nvme", "nvme0", "Composite")
    nvme1 = temp("nvme", "nvme1", "Composite")
    assert value(samples, "node_hwmon_temp_celsius", **nvme0) == 38.85
    assert value(samples, "node_hwmon_temp_celsius", **nvme1) == 41.85
    assert value(samples, "node_hwmon_temp_alarm", **nvme0) == 0
    assert value(samples, "node_hwmon_temp_alarm", **nvme1) == 1
    assert value(samples, "node_hwmon_temp_max_celsius", **nvme0) == 84.85
    assert value(samples, "node_hwmon_temp_crit_celsius", **nvme1) == 84.85
    sensor1 = temp("nvme", "nvme0", "Sensor 1")
    assert sensor1["sensor"] == "Sensor_1"
    assert value(samples, "node_hwmon_temp_celsius", **sensor1) == 38.85
    # NVMe reports an unset threshold as 65261.85 °C; it is not exported
    assert value(samples, "node_hwmon_temp_max_celsius", **sensor1) is None
    nvme_series = [k for k in samples["node_hwmon_temp_celsius"] if ("chip", "nvme") in k]
    assert len(nvme_series) == 4


def test_thresholds_only_when_positive(samples: Samples) -> None:
    systin = temp("nct6798", "nct6775.656", "SYSTIN")
    cputin = temp("nct6798", "nct6775.656", "CPUTIN")
    assert value(samples, "node_hwmon_temp_celsius", **systin) == 35.0
    assert value(samples, "node_hwmon_temp_max_celsius", **systin) == 80.0
    assert value(samples, "node_hwmon_temp_alarm", **systin) == 0
    assert value(samples, "node_hwmon_temp_celsius", **cputin) == 40.5
    assert value(samples, "node_hwmon_temp_max_celsius", **cputin) is None  # temp2_max is 0
    peci = temp("nct6798", "nct6775.656", "PECI Agent 0 Calibration")
    assert peci["sensor"] == "PECI_Agent_0_Calibration"
    assert value(samples, "node_hwmon_temp_celsius", **peci) == 51.0


def test_unreadable_sensor_is_skipped(samples: Samples) -> None:
    labels = {dict(k)["label"] for k in samples["node_hwmon_temp_celsius"]}
    assert "PCH_CHIP_TEMP" not in labels
    assert not [k for k in samples["node_hwmon_temp_celsius"] if ("chip", "iwlwifi_1") in k]


def test_fans(samples: Samples) -> None:
    fan1 = other("nct6798", "nct6775.656", "fan1")
    fan2 = other("nct6798", "nct6775.656", "fan2")
    assert value(samples, "node_hwmon_fan_rpm", **fan1) == 1245
    assert value(samples, "node_hwmon_fan_min_rpm", **fan1) == 300
    assert value(samples, "node_hwmon_fan_rpm", **fan2) == 0
    assert value(samples, "node_hwmon_fan_min_rpm", **fan2) == 0
    assert value(samples, "node_hwmon_fan_min_rpm", **other("pmbus", "0-0058", "fan1")) is None


def test_voltage_current_power_units(samples: Samples) -> None:
    psu = "0-0058"
    # mV -> V, mA -> A, uW -> W
    assert value(samples, "node_hwmon_voltage_volts", **other("pmbus", psu, "vin")) == 230.0
    assert value(samples, "node_hwmon_voltage_volts", **other("pmbus", psu, "vout1")) == 12.05
    assert value(samples, "node_hwmon_curr_amps", **other("pmbus", psu, "iin")) == 1.25
    assert value(samples, "node_hwmon_curr_amps", **other("pmbus", psu, "iout1")) == 20.5
    assert value(samples, "node_hwmon_power_watt", **other("pmbus", psu, "pin")) == 287.0
    assert value(samples, "node_hwmon_power_watt", **other("pmbus", psu, "pout1")) == 247.0
    gpu = "0000:09:00.0"
    assert value(samples, "node_hwmon_voltage_volts", **other("amdgpu", gpu, "vddgfx")) == 0.8
    # power1_input is preferred over power1_average
    assert samples["node_hwmon_power_watt"][frozenset(other("amdgpu", gpu, "PPT").items())] == 25.0
    assert len([k for k in samples["node_hwmon_power_watt"] if ("chip", "amdgpu") in k]) == 1


def test_power_average_only(tmp_path: Path, make_ctx: Callable[..., Context]) -> None:
    chip(
        tmp_path,
        "hwmon5",
        "pci0000:40/0000:40:01.1/0000:41:00.0",
        {"name": "amdgpu\n", "power1_label": "PPT\n", **milli(power1_average=23000000)},
    )
    samples = collect(HwmonCollector(make_ctx()))
    assert samples == {
        "node_hwmon_power_watt": {
            frozenset(other("amdgpu", "0000:41:00.0", "PPT").items()): 23.0,
        }
    }


def test_label_with_dots_and_spaces(tmp_path: Path, make_ctx: Callable[..., Context]) -> None:
    chip(
        tmp_path,
        "hwmon3",
        "platform/asus-ec-sensors",
        {"name": "asusec\n", "temp1_label": "Ambient 1.0\n", **milli(temp1_input=31500)},
    )
    samples = collect(HwmonCollector(make_ctx()))
    labels = {
        "chip": "asusec",
        "device": "asus-ec-sensors",
        "sensor": "Ambient_1_0",
        "label": "Ambient 1.0",
    }
    assert value(samples, "node_hwmon_temp_celsius", **labels) == 31.5


def test_old_kernel_layout(tmp_path: Path, make_ctx: Callable[..., Context]) -> None:
    # Before 3.x the sensors and "name" lived in the parent device directory.
    write_tree(
        tmp_path / "sys" / "devices" / "platform" / "it87.656",
        {"name": "it87\n", **milli(temp1_input=38000, fan1_input=2100, in0_input=1104)},
    )
    chip(tmp_path, "hwmon0", "platform/it87.656", {})
    samples = collect(HwmonCollector(make_ctx()))
    t1 = {"chip": "it87", "device": "it87.656", "sensor": "temp1", "label": "temp1"}
    assert value(samples, "node_hwmon_temp_celsius", **t1) == 38.0
    assert value(samples, "node_hwmon_fan_rpm", **other("it87", "it87.656", "fan1")) == 2100
    assert value(samples, "node_hwmon_voltage_volts", **other("it87", "it87.656", "in0")) == 1.104


def test_duplicate_labels_on_one_chip(tmp_path: Path, make_ctx: Callable[..., Context]) -> None:
    # dell_smm names temperatures after the sensor type, so labels repeat;
    # every sensor must still get its own series.
    chip(
        tmp_path,
        "hwmon5",
        "platform/dell_smm_hwmon",
        {
            "name": "dell_smm\n",
            "temp1_label": "CPU\n",
            "temp2_label": "Ambient\n",
            "temp3_label": "Other\n",
            "temp4_label": "Other\n",
            "fan1_label": "Processor Fan\n",
            "fan2_label": "Processor Fan\n",
            **milli(
                temp1_input=53000,
                temp2_input=42000,
                temp3_input=38000,
                temp4_input=41000,
                fan1_input=2515,
                fan2_input=2680,
            ),
        },
    )
    samples = collect(HwmonCollector(make_ctx()))
    temps = samples["node_hwmon_temp_celsius"]
    assert sorted(temps.values()) == [38.0, 41.0, 42.0, 53.0]
    assert sorted(dict(k)["label"] for k in temps) == ["Ambient", "CPU", "Other", "Other"]
    dev = "dell_smm_hwmon"
    # the first sensor keeps the label, later ones fall back to their file name
    other3 = {"chip": "dell_smm", "device": dev, "sensor": "Other", "label": "Other"}
    other4 = {"chip": "dell_smm", "device": dev, "sensor": "temp4", "label": "Other"}
    assert value(samples, "node_hwmon_temp_celsius", **other3) == 38.0
    assert value(samples, "node_hwmon_temp_celsius", **other4) == 41.0
    assert samples["node_hwmon_fan_rpm"] == {
        frozenset(other("dell_smm", dev, "Processor Fan").items()): 2515,
        frozenset(other("dell_smm", dev, "fan2").items()): 2680,
    }
