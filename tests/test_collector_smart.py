from __future__ import annotations

import json
from typing import Any, Callable

import pytest

from conftest import FakeRunner, Response, Samples, collect, fixture, value
from proxmox_node_exporter.collectors.base import Context
from proxmox_node_exporter.collectors.smart import SmartCollector, device_label, parse_scan
from proxmox_node_exporter.runner import CommandError, CommandResult

SCAN = ("smartctl", "--scan")
HDD = {"device": "sda", "model": "ST4000VN008-2DR166"}
SSD = {"device": "sdb", "model": "INTEL SSDSC2KB480G8"}
SAS = {"device": "sdc", "model": "SEAGATE ST4000NM0023"}
NVME = {"device": "nvme0", "model": "Samsung SSD 980 PRO 2TB"}
MEGARAID = {"device": "bus/0[megaraid,0]", "model": "HGST HUH721010AL5200"}

DEVICES = {
    ("sat", "/dev/sda"): "sda_hdd.json",
    ("sat", "/dev/sdb"): "sdb_ssd.json",
    ("scsi", "/dev/sdc"): "sdc_sas.json",
    ("sat", "/dev/sdd"): "sdd_standby.json",
    ("nvme", "/dev/nvme0"): "nvme0.json",
    ("megaraid,0", "/dev/bus/0"): "bus0_megaraid0.json",
}


def query(kind: str, device: str) -> tuple[str, ...]:
    return ("smartctl", "--json", "-a", "-n", "standby", "-d", kind, device)


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(fixture(f"smart/{name}"))
    return data


def output(data: dict[str, Any]) -> CommandResult:
    """smartctl's stdout plus the exit status it reports in the JSON."""
    return CommandResult(data["smartctl"]["exit_status"], json.dumps(data, indent=2), "")


def smart_runner(
    scan: str | None = None, overrides: dict[tuple[str, str], Response] | None = None
) -> FakeRunner:
    responses: dict[tuple[str, ...], Response] = {
        SCAN: fixture("smart/scan.txt") if scan is None else scan
    }
    for (kind, device), name in DEVICES.items():
        responses[query(kind, device)] = output(load(name))
    for (kind, device), response in (overrides or {}).items():
        responses[query(kind, device)] = response
    return FakeRunner(responses)


@pytest.fixture
def samples(make_ctx: Callable[..., Context]) -> Samples:
    return collect(SmartCollector(make_ctx(smart_runner())))


def devices(samples: Samples, metric: str) -> set[str]:
    return {dict(k)["device"] for k in samples.get(metric, {})}


# -- scan ------------------------------------------------------------------------------


def test_parse_scan() -> None:
    assert parse_scan(fixture("smart/scan.txt")) == [
        ("/dev/sda", "sat"),
        ("/dev/sdb", "sat"),
        ("/dev/sdc", "scsi"),
        ("/dev/sdd", "sat"),
        ("/dev/nvme0", "nvme"),
        ("/dev/bus/0", "megaraid,0"),
    ]


def test_parse_scan_ignores_untrusted_lines() -> None:
    text = (
        "sda -d sat # relative path\n"
        "/tmp/evil -d sat # not below /dev\n"
        "/dev/../etc/shadow -d sat # escapes /dev\n"
        "/dev/sdb/../../root/.ssh/id_rsa -d sat # escapes /dev\n"
        "/dev/sdb;reboot -d sat # shell metacharacters in the device\n"
        "/dev/sdc -d sat;reboot # shell metacharacters in the type\n"
        "/dev/sdd -d $(reboot) # command substitution\n"
        "/dev/sde -d `id` # command substitution\n"
        "/dev/sdf -d sat|id # pipe\n"
        "/dev/sdg -d ,sat # empty type component\n"
        "/dev/sdh -d sat, # empty type component\n"
        "/dev/sdn -d -foo # type looks like an option\n"
        "/dev/sdi -T permissive # not a -d option\n"
        "/dev/sdj # no type\n"
        "\n"
        "# /dev/sdk -d sat # commented out\n"
        "/dev/sdl -d sat,12 # /dev/sdl [SAT], ATA device\n"
        "/dev/sdm -d usbjmicron,0 # /dev/sdm [USB JMicron], ATA device\n"
        "/dev/disk/by-id/ata-ST4000VN008-2DR166_ZDH1A2B3 -d sat # by-id link\n"
        "/dev/sg2 -d areca,3/1 # Areca enclosure/slot\n"
        "/dev/sda -d hpt,1/1/2 # HighPoint controller/channel/pmport\n"
    )
    assert parse_scan(text) == [
        ("/dev/sdl", "sat,12"),
        ("/dev/sdm", "usbjmicron,0"),
        ("/dev/disk/by-id/ata-ST4000VN008-2DR166_ZDH1A2B3", "sat"),
        ("/dev/sg2", "areca,3/1"),
        ("/dev/sda", "hpt,1/1/2"),
    ]


def test_untrusted_scan_lines_are_never_queried(make_ctx: Callable[..., Context]) -> None:
    runner = smart_runner(
        scan="/dev/sda;reboot -d sat #\n/dev/../etc/shadow -d sat #\n/dev/sda -d $(id) #\n"
    )
    assert collect(SmartCollector(make_ctx(runner))) == {}
    assert runner.calls == [SCAN]


@pytest.mark.parametrize(
    ("name", "kind", "label"),
    [
        ("/dev/sda", "sat", "sda"),
        ("/dev/nvme0", "nvme", "nvme0"),
        ("/dev/bus/0", "megaraid,0", "bus/0[megaraid,0]"),
        ("/dev/bus/0", "megaraid,1", "bus/0[megaraid,1]"),
        ("/dev/sdl", "sat,12", "sdl[sat,12]"),
    ],
)
def test_device_label(name: str, kind: str, label: str) -> None:
    assert device_label(name, kind) == label


# -- detection -------------------------------------------------------------------------


def test_detect(make_ctx: Callable[..., Context]) -> None:
    assert SmartCollector(make_ctx(smart_runner())).detect()
    assert not SmartCollector(make_ctx(smart_runner(), is_root=False)).detect()
    assert not SmartCollector(make_ctx(FakeRunner())).detect()


# -- collection ------------------------------------------------------------------------


def test_commands(make_ctx: Callable[..., Context]) -> None:
    runner = smart_runner()
    collect(SmartCollector(make_ctx(runner)))
    assert runner.calls == [SCAN, *(query(kind, dev) for kind, dev in DEVICES)]


def test_sata_hdd(samples: Samples) -> None:
    info = dict(HDD, serial="ZDH1A2B3", firmware="SC60", protocol="ATA")
    assert value(samples, "node_disk_smart_info", **info) == 1
    assert value(samples, "node_disk_smart_healthy", **HDD, serial="ZDH1A2B3") == 1
    assert value(samples, "node_disk_smart_standby", device="sda") == 0
    # 64: the device error log contains records
    assert value(samples, "node_disk_smart_exit_status", device="sda") == 64
    assert value(samples, "node_disk_smart_temperature_celsius", **HDD) == 34
    assert value(samples, "node_disk_smart_power_on_hours_total", **HDD) == 31245
    assert value(samples, "node_disk_smart_power_cycles_total", **HDD) == 58
    assert value(samples, "node_disk_smart_reallocated_sectors", **HDD) == 16
    assert value(samples, "node_disk_smart_pending_sectors", **HDD) == 8
    # raw value 0x0004_0000_0003: vendor data in the high bytes, the count in the low 32 bits
    assert value(samples, "node_disk_smart_uncorrectable_sectors", **HDD) == 3
    assert value(samples, "node_disk_smart_spin_retry_count", **HDD) == 1
    assert value(samples, "node_disk_smart_udma_crc_errors", **HDD) == 5
    assert value(samples, "node_disk_smart_error_log_entries", **HDD) == 2
    assert value(samples, "node_disk_smart_ssd_wearout_percent", **HDD) is None
    assert value(samples, "node_disk_smart_available_spare_percent", **HDD) is None


def test_sata_ssd_wearout_from_attribute(samples: Samples) -> None:
    info = dict(SSD, serial="PHYF912300AB480BGN", firmware="XCV10132", protocol="ATA")
    assert value(samples, "node_disk_smart_info", **info) == 1
    # 233 Media_Wearout_Indicator: normalised value 98 = 98% life left
    assert value(samples, "node_disk_smart_ssd_wearout_percent", **SSD) == 2
    assert value(samples, "node_disk_smart_temperature_celsius", **SSD) == 29
    assert value(samples, "node_disk_smart_reallocated_sectors", **SSD) == 0
    assert value(samples, "node_disk_smart_pending_sectors", **SSD) == 0
    assert value(samples, "node_disk_smart_udma_crc_errors", **SSD) == 0
    assert value(samples, "node_disk_smart_uncorrectable_sectors", **SSD) is None
    assert value(samples, "node_disk_smart_error_log_entries", **SSD) == 0
    assert value(samples, "node_disk_smart_exit_status", device="sdb") == 0


def test_sas_disk(samples: Samples) -> None:
    info = dict(SAS, serial="Z1Z3ABCD0000C4471234", firmware="GS10", protocol="SCSI")
    assert value(samples, "node_disk_smart_info", **info) == 1
    assert value(samples, "node_disk_smart_healthy", **SAS, serial="Z1Z3ABCD0000C4471234") == 1
    assert value(samples, "node_disk_smart_reallocated_sectors", **SAS) == 8  # grown defects
    assert value(samples, "node_disk_smart_temperature_celsius", **SAS) == 36
    assert value(samples, "node_disk_smart_power_on_hours_total", **SAS) == 41234
    assert value(samples, "node_disk_smart_power_cycles_total", **SAS) is None
    assert value(samples, "node_disk_smart_ssd_wearout_percent", **SAS) is None


def test_nvme(samples: Samples) -> None:
    info = dict(NVME, serial="S6B0NG0R123456X", firmware="5B2QGXA7", protocol="NVMe")
    assert value(samples, "node_disk_smart_info", **info) == 1
    assert value(samples, "node_disk_smart_healthy", **NVME, serial="S6B0NG0R123456X") == 1
    assert value(samples, "node_disk_smart_temperature_celsius", **NVME) == 41
    assert value(samples, "node_disk_smart_power_on_hours_total", **NVME) == 5678
    assert value(samples, "node_disk_smart_power_cycles_total", **NVME) == 120
    assert value(samples, "node_disk_smart_available_spare_percent", **NVME) == 100
    assert value(samples, "node_disk_smart_critical_warning", **NVME) == 0
    assert value(samples, "node_disk_smart_media_errors_total", **NVME) == 0
    assert value(samples, "node_disk_smart_unsafe_shutdowns_total", **NVME) == 45
    assert value(samples, "node_disk_smart_error_log_entries", **NVME) == 12
    # data units are thousands of 512-byte blocks
    assert value(samples, "node_disk_smart_read_bytes_total", **NVME) == 23456789 * 512_000
    assert value(samples, "node_disk_smart_written_bytes_total", **NVME) == 34567890 * 512_000
    assert value(samples, "node_disk_smart_ssd_wearout_percent", **NVME) == 3
    assert value(samples, "node_disk_smart_reallocated_sectors", **NVME) is None


def test_megaraid_disk(samples: Samples) -> None:
    info = dict(MEGARAID, serial="7JH1ABCD", firmware="A384", protocol="SCSI")
    assert value(samples, "node_disk_smart_info", **info) == 1
    assert value(samples, "node_disk_smart_temperature_celsius", **MEGARAID) == 31
    assert value(samples, "node_disk_smart_reallocated_sectors", **MEGARAID) == 0
    assert value(samples, "node_disk_smart_standby", device="bus/0[megaraid,0]") == 0


def test_standby_disk_is_not_woken_or_reported(samples: Samples) -> None:
    assert value(samples, "node_disk_smart_standby", device="sdd") == 1
    assert "sdd" not in devices(samples, "node_disk_smart_info")
    assert "sdd" not in devices(samples, "node_disk_smart_exit_status")
    assert devices(samples, "node_disk_smart_standby") == {
        "sda",
        "sdb",
        "sdc",
        "sdd",
        "nvme0",
        "bus/0[megaraid,0]",
    }


def test_failing_disk(make_ctx: Callable[..., Context]) -> None:
    data = load("sda_hdd.json")
    data["smart_status"] = {"passed": False}
    data["smartctl"]["exit_status"] = 8 | 64
    runner = smart_runner(overrides={("sat", "/dev/sda"): output(data)})
    samples = collect(SmartCollector(make_ctx(runner)))
    assert value(samples, "node_disk_smart_healthy", **HDD, serial="ZDH1A2B3") == 0
    assert value(samples, "node_disk_smart_exit_status", device="sda") == 72


def test_endurance_used_takes_precedence(make_ctx: Callable[..., Context]) -> None:
    data = load("sdb_ssd.json")
    data["endurance_used"] = {"current_percent": 7}  # from the device statistics log
    runner = smart_runner(overrides={("sat", "/dev/sdb"): output(data)})
    samples = collect(SmartCollector(make_ctx(runner)))
    assert value(samples, "node_disk_smart_ssd_wearout_percent", **SSD) == 7


@pytest.mark.parametrize(
    ("attr_id", "name", "normalised", "wearout"),
    [
        (231, "SSD_Life_Left", 91, 9),
        (177, "Wear_Leveling_Count", 95, 5),
        (202, "Percent_Lifetime_Remain", 97, 3),
    ],
)
def test_wearout_from_other_life_attributes(
    make_ctx: Callable[..., Context], attr_id: int, name: str, normalised: int, wearout: float
) -> None:
    data = load("sdb_ssd.json")
    table = [a for a in data["ata_smart_attributes"]["table"] if a["id"] != 233]
    life = dict(table[0], id=attr_id, name=name, value=normalised, worst=normalised)
    data["ata_smart_attributes"]["table"] = [*table, life]
    runner = smart_runner(overrides={("sat", "/dev/sdb"): output(data)})
    samples = collect(SmartCollector(make_ctx(runner)))
    assert value(samples, "node_disk_smart_ssd_wearout_percent", **SSD) == wearout


# -- errors ----------------------------------------------------------------------------


def test_one_failing_device_does_not_fail_the_collector(make_ctx: Callable[..., Context]) -> None:
    runner = smart_runner(
        overrides={
            ("sat", "/dev/sda"): CommandError("smartctl: timed out after 30s"),
            # device vanished between the scan and the query: exit status bit 1
            ("scsi", "/dev/sdc"): output(load("sde_open_failed.json")),
            # smartctl 6.x has no --json
            ("nvme", "/dev/nvme0"): CommandResult(1, "=======> UNRECOGNIZED OPTION: json\n", ""),
        }
    )
    samples = collect(SmartCollector(make_ctx(runner)))
    assert devices(samples, "node_disk_smart_standby") == {"sdb", "sdd", "bus/0[megaraid,0]"}
    assert devices(samples, "node_disk_smart_info") == {"sdb", "bus/0[megaraid,0]"}


def test_every_device_failing_fails_the_collector(make_ctx: Callable[..., Context]) -> None:
    overrides: dict[tuple[str, str], Response] = {
        device: CommandError("smartctl: timed out after 30s") for device in DEVICES
    }
    with pytest.raises(RuntimeError, match="every device"):
        collect(SmartCollector(make_ctx(smart_runner(overrides=overrides))))


def test_only_standby_disks_is_not_a_failure(make_ctx: Callable[..., Context]) -> None:
    runner = smart_runner(scan="/dev/sdd -d sat # /dev/sdd [SAT], ATA device\n")
    samples = collect(SmartCollector(make_ctx(runner)))
    assert samples == {"node_disk_smart_standby": {frozenset({("device", "sdd")}): 1.0}}


def test_no_disks(make_ctx: Callable[..., Context]) -> None:
    assert collect(SmartCollector(make_ctx(smart_runner(scan="")))) == {}


def test_scan_failure_fails_the_collector(make_ctx: Callable[..., Context]) -> None:
    runner = FakeRunner({SCAN: CommandError("smartctl: exit status 1")})
    with pytest.raises(CommandError):
        collect(SmartCollector(make_ctx(runner)))


def test_life_attribute_ids_reused_for_other_data_are_ignored(
    make_ctx: Callable[..., Context],
) -> None:
    data = load("sdb_ssd.json")
    for attr in data["ata_smart_attributes"]["table"]:
        if attr["id"] == 233:
            attr["name"] = "NAND_GB_Written"  # WD Blue reuses the ID
            attr["value"] = 100
    runner = smart_runner(overrides={("sat", "/dev/sdb"): output(data)})
    samples = collect(SmartCollector(make_ctx(runner)))
    assert value(samples, "node_disk_smart_ssd_wearout_percent", **SSD) is None


def test_sas_ssd_endurance(make_ctx: Callable[..., Context]) -> None:
    data = load("sdc_sas.json")
    data["scsi_percentage_used_endurance_indicator"] = 4
    runner = smart_runner(overrides={("scsi", "/dev/sdc"): output(data)})
    samples = collect(SmartCollector(make_ctx(runner)))
    assert value(samples, "node_disk_smart_ssd_wearout_percent", **SAS) == 4
