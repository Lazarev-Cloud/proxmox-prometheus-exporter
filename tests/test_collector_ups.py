from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from conftest import FakeRunner, Response, Samples, collect, fixture, value
from proxmox_node_exporter.collectors.base import Context
from proxmox_node_exporter.collectors.ups import UpsCollector, parse_upsc
from proxmox_node_exporter.runner import CommandError, CommandResult

LIST = ("upsc", "-l", "localhost")
NUT_CONF = """\
# Network UPS Tools: example ups.conf
maxretry = 3

[apc]
\tdriver = usbhid-ups
\tport = auto
\tdesc = "Back-UPS RS 1500G"

  [eaton]
\tdriver = usbhid-ups
\tport = auto
"""
DRIVER_NOT_CONNECTED = CommandResult(1, "", "Error: Driver not connected\n")


def ups_runner(**responses: Response) -> FakeRunner:
    base: dict[tuple[str, ...], Response] = {
        LIST: fixture("ups/upsc_list.txt"),
        ("upsc", "apc"): fixture("ups/upsc_apc.txt"),
        ("upsc", "eaton"): fixture("ups/upsc_eaton.txt"),
    }
    base.update({("upsc", name): response for name, response in responses.items()})
    return FakeRunner(base)


def flags(samples: Samples, ups: str) -> dict[str, float | None]:
    return {
        flag: value(samples, f"node_ups_{flag}", ups=ups)
        for flag in ("online", "on_battery", "low_battery", "replace_battery")
    }


# -- detection -------------------------------------------------------------------------


@pytest.fixture
def nut_conf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "ups.conf"
    monkeypatch.setattr(UpsCollector, "NUT_CONFIG", str(path))
    return path


def test_detect_without_upsc(make_ctx: Callable[..., Context], nut_conf: Path) -> None:
    nut_conf.write_text(NUT_CONF)
    assert not UpsCollector(make_ctx(FakeRunner(), ups_targets=["apc"])).detect()


def test_detect_with_targets(make_ctx: Callable[..., Context], nut_conf: Path) -> None:
    assert UpsCollector(make_ctx(ups_runner(), is_root=False, ups_targets=["apc"])).detect()


def test_detect_from_nut_config(make_ctx: Callable[..., Context], nut_conf: Path) -> None:
    collector = UpsCollector(make_ctx(ups_runner()))
    assert not collector.detect()  # no ups.conf
    nut_conf.write_text("# [myups]\n#\tdriver = usbhid-ups\nmaxretry = 3\n")
    assert not collector.detect()  # only commented-out sections
    nut_conf.write_text(NUT_CONF)
    assert collector.detect()


# -- collection ------------------------------------------------------------------------


def test_upsc_output(make_ctx: Callable[..., Context]) -> None:
    runner = ups_runner()
    samples = collect(UpsCollector(make_ctx(runner)))
    assert runner.calls == [LIST, ("upsc", "apc"), ("upsc", "eaton")]

    info = "node_ups_info"
    apc = {"ups": "apc", "manufacturer": "American Power Conversion", "model": "Back-UPS RS 1500G"}
    assert value(samples, info, **apc) == 1
    assert value(samples, "node_ups_battery_charge_percent", ups="apc") == 100
    assert value(samples, "node_ups_battery_runtime_seconds", ups="apc") == 1380
    assert value(samples, "node_ups_battery_voltage_volts", ups="apc") == 27.3
    assert value(samples, "node_ups_input_voltage_volts", ups="apc") == 121
    assert value(samples, "node_ups_output_voltage_volts", ups="apc") is None
    assert value(samples, "node_ups_load_percent", ups="apc") == 22
    assert value(samples, "node_ups_power_nominal_watts", ups="apc") == 865
    assert value(samples, "node_ups_power_watts", ups="apc") is None
    assert value(samples, "node_ups_temperature_celsius", ups="apc") is None
    assert flags(samples, "apc") == {
        "online": 1,
        "on_battery": 0,
        "low_battery": 0,
        "replace_battery": 0,
    }

    assert value(samples, info, ups="eaton", manufacturer="EATON", model="Eaton 5E 1100i") == 1
    assert value(samples, "node_ups_battery_charge_percent", ups="eaton") == 18
    assert value(samples, "node_ups_battery_runtime_seconds", ups="eaton") == 142
    assert value(samples, "node_ups_input_voltage_volts", ups="eaton") == 0
    assert value(samples, "node_ups_output_voltage_volts", ups="eaton") == 230
    assert value(samples, "node_ups_load_percent", ups="eaton") == 41
    assert value(samples, "node_ups_power_watts", ups="eaton") == 283
    assert value(samples, "node_ups_temperature_celsius", ups="eaton") == 31.5
    # "OB LB": on battery and about to shut down
    assert flags(samples, "eaton") == {
        "online": 0,
        "on_battery": 1,
        "low_battery": 1,
        "replace_battery": 0,
    }


def test_older_driver_variables_and_replace_battery(make_ctx: Callable[..., Context]) -> None:
    runner = ups_runner(
        apc="battery.charge: 97\nups.mfr: CPS\nups.model: CP1500EPFCLCD\nups.status: OL CHRG RB\n",
        eaton="battery.charge: 100\ndevice.model: 5E 1100i\n",
    )
    samples = collect(UpsCollector(make_ctx(runner)))
    cps = {"ups": "apc", "manufacturer": "CPS", "model": "CP1500EPFCLCD"}
    assert value(samples, "node_ups_info", **cps) == 1
    assert flags(samples, "apc") == {
        "online": 1,
        "on_battery": 0,
        "low_battery": 0,
        "replace_battery": 1,
    }
    # No ups.status at all: the status flags are unknown, not "off".
    assert value(samples, "node_ups_info", ups="eaton", manufacturer="", model="5E 1100i") == 1
    assert flags(samples, "eaton") == dict.fromkeys(flags(samples, "eaton"))


def test_configured_targets(make_ctx: Callable[..., Context]) -> None:
    runner = ups_runner(**{"rack@nas.example.lan:3493": fixture("ups/upsc_eaton.txt")})
    ctx = make_ctx(runner, ups_targets=["apc", "rack@nas.example.lan:3493"])
    samples = collect(UpsCollector(ctx))
    assert runner.calls == [("upsc", "apc"), ("upsc", "rack@nas.example.lan:3493")]
    # The label keeps the host, so equally named UPSes on different servers stay apart.
    assert value(samples, "node_ups_load_percent", ups="rack@nas.example.lan:3493") == 41
    assert value(samples, "node_ups_load_percent", ups="apc") == 22


@pytest.mark.parametrize(
    "hostile",
    [
        "$(reboot)",
        "ups;id",
        "ups|id",
        "ups`id`",
        "ups&&id",
        "../../etc/passwd",
        "ups@host;id",
        "ups@$(id)",
        "ups@host/../x",
        "-h",  # would be read as an upsc option
        "--help",
    ],
)
def test_names_with_shell_metacharacters_are_ignored(
    make_ctx: Callable[..., Context], hostile: str
) -> None:
    runner = ups_runner()
    runner.responses[LIST] = f"apc\n{hostile}\n"
    collector = UpsCollector(make_ctx(runner))
    assert collector.targets() == ["apc"]
    collect(collector)
    assert ("upsc", hostile) not in runner.calls
    configured = UpsCollector(make_ctx(runner, ups_targets=[hostile, "eaton"]))
    assert configured.targets() == ["eaton"]


def test_one_failing_ups_does_not_fail_the_collector(make_ctx: Callable[..., Context]) -> None:
    runner = ups_runner(apc=DRIVER_NOT_CONNECTED)
    samples = collect(UpsCollector(make_ctx(runner)))
    assert {dict(k)["ups"] for k in samples["node_ups_info"]} == {"eaton"}


def test_every_ups_failing_fails_the_collector(make_ctx: Callable[..., Context]) -> None:
    runner = ups_runner(apc=DRIVER_NOT_CONNECTED, eaton=CommandResult(1, "", "Error: Data stale\n"))
    with pytest.raises(RuntimeError, match="every UPS"):
        collect(UpsCollector(make_ctx(runner)))


def test_no_ups(make_ctx: Callable[..., Context]) -> None:
    runner = FakeRunner({LIST: ""})
    assert collect(UpsCollector(make_ctx(runner))) == {}


def test_listing_failure_fails_the_collector(make_ctx: Callable[..., Context]) -> None:
    runner = FakeRunner(
        {LIST: CommandResult(1, "", "Error: Connection failure: Connection refused\n")}
    )
    with pytest.raises(CommandError):
        collect(UpsCollector(make_ctx(runner)))


def test_parse_upsc() -> None:
    text = fixture("ups/upsc_apc.txt")
    values = parse_upsc(text)
    assert values["device.model"] == "Back-UPS RS 1500G"
    assert values["driver.version.usb"] == "libusb-1.0.26 (API: 0x1000109)"
    assert values["ups.status"] == "OL CHRG"
    assert len(values) == len(text.splitlines()) == 51


def test_localhost_suffix_is_dropped_from_the_label(make_ctx: Callable[..., Context]) -> None:
    runner = ups_runner(**{"apc@localhost": fixture("ups/upsc_apc.txt")})
    samples = collect(UpsCollector(make_ctx(runner, ups_targets=["apc@localhost"])))
    assert value(samples, "node_ups_load_percent", ups="apc") == 22
