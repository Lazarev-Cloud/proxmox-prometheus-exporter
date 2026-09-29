"""Disk health from smartctl (ATA/SATA, NVMe and SAS).

Disks in standby are not woken up (``-n standby``); they are reported through
``node_disk_smart_standby`` until they spin up again.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..metrics import Batch, MetricGroup
from ..runner import CommandError
from .base import Collector

log = logging.getLogger(__name__)

M = MetricGroup("smart")
_D = ("device", "model")
INFO = M.gauge(
    "node_disk_smart_info",
    "Disk identity; the value is always 1.",
    "device",
    "model",
    "serial",
    "firmware",
    "protocol",
)
HEALTHY = M.gauge(
    "node_disk_smart_healthy",
    "Overall SMART health self-assessment passed.",
    "device",
    "model",
    "serial",
)
STANDBY = M.gauge("node_disk_smart_standby", "Disk was in standby and not queried.", "device")
EXIT_STATUS = M.gauge(
    "node_disk_smart_exit_status",
    "smartctl exit status bitmask (8: failing, 16: prefail attribute at threshold, "
    "32: attribute was at threshold, 64: errors logged, 128: self-test errors).",
    "device",
)
TEMP = M.gauge("node_disk_smart_temperature_celsius", "Disk temperature.", *_D)
POWER_ON_HOURS = M.counter("node_disk_smart_power_on_hours_total", "Power-on hours.", *_D)
POWER_CYCLES = M.counter("node_disk_smart_power_cycles_total", "Power cycles.", *_D)
REALLOCATED = M.gauge(
    "node_disk_smart_reallocated_sectors", "Reallocated sectors (SAS: grown defects).", *_D
)
PENDING = M.gauge("node_disk_smart_pending_sectors", "Sectors pending reallocation.", *_D)
UNCORRECTABLE = M.gauge(
    "node_disk_smart_uncorrectable_sectors", "Offline uncorrectable sectors.", *_D
)
SPIN_RETRY = M.gauge("node_disk_smart_spin_retry_count", "Spin-up retries.", *_D)
CRC_ERRORS = M.gauge(
    "node_disk_smart_udma_crc_errors", "Interface CRC errors (usually cabling).", *_D
)
WEAROUT = M.gauge(
    "node_disk_smart_ssd_wearout_percent", "Share of the SSD's rated endurance used.", *_D
)
SPARE = M.gauge("node_disk_smart_available_spare_percent", "NVMe available spare.", *_D)
CRITICAL_WARNING = M.gauge(
    "node_disk_smart_critical_warning", "NVMe critical warning bitmask.", *_D
)
MEDIA_ERRORS = M.counter("node_disk_smart_media_errors_total", "NVMe media errors.", *_D)
UNSAFE_SHUTDOWNS = M.counter(
    "node_disk_smart_unsafe_shutdowns_total", "NVMe unsafe shutdowns.", *_D
)
READ_BYTES = M.counter("node_disk_smart_read_bytes_total", "NVMe host bytes read.", *_D)
WRITTEN_BYTES = M.counter("node_disk_smart_written_bytes_total", "NVMe host bytes written.", *_D)
ERROR_LOG = M.gauge("node_disk_smart_error_log_entries", "Entries in the device error log.", *_D)

_DEVICE_RE = re.compile(r"^/dev/[A-Za-z0-9/_.:-]+$")
# e.g. "sat", "nvme", "megaraid,0", "areca,3/1", "hpt,1/1/2"
_TYPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_+-]*(,[A-Za-z0-9_+/-]+)*$")
_ATA_ATTRS = {5: REALLOCATED, 197: PENDING, 198: UNCORRECTABLE, 10: SPIN_RETRY, 199: CRC_ERRORS}
# ATA attributes whose normalised value is the remaining life in percent. The
# name is checked too: vendors reuse these IDs for other things (WD Blue's 233
# is NAND_GB_Written, old HDDs' 202 is Data_Address_Mark_Errs).
_ATA_LIFE_ATTRS = {
    231: "SSD_Life_Left",
    233: "Media_Wearout_Indicator",
    177: "Wear_Leveling_Count",
    202: "Percent_Lifetime_Remain",
}
_NVME_DATA_UNIT = 512_000


def parse_scan(text: str) -> list[tuple[str, str]]:
    devices = []
    for line in text.splitlines():
        fields = line.split("#", 1)[0].split()
        if len(fields) >= 3 and fields[1] == "-d":
            name, kind = fields[0], fields[2]
            if _DEVICE_RE.match(name) and ".." not in name.split("/") and _TYPE_RE.match(kind):
                devices.append((name, kind))
    return devices


def device_label(name: str, kind: str) -> str:
    label = name[len("/dev/") :]
    return f"{label}[{kind}]" if "," in kind else label


class SmartCollector(Collector):
    name = "smart"
    description = "SMART health, temperature, wear and error counters (smartctl, root)"
    default_interval = 120.0

    def detect(self) -> bool:
        return self.ctx.is_root and self.has_command("smartctl")

    def collect(self, out: Batch) -> None:
        devices = parse_scan(self.run("smartctl", "--scan").stdout)
        read = 0
        failures: list[str] = []
        for name, kind in devices:
            try:
                if self._device(out, device_label(name, kind), self._query(name, kind)):
                    read += 1
            except (CommandError, ValueError, TypeError, AttributeError) as exc:
                log.debug("smartctl %s: %s", name, exc)
                failures.append(f"{name}: {exc}")
        # Disks smartctl cannot open (virtual disks, unsupported USB bridges,
        # disks that vanished) are not failures of the collector; smartctl
        # itself not working (timeouts, no JSON output) is.
        if failures and not read:
            raise RuntimeError(f"smartctl failed for every device ({failures[0]})")

    def _query(self, name: str, kind: str) -> dict[str, Any]:
        result = self.run(
            "smartctl", "--json", "-a", "-n", "standby", "-d", kind, name,
            timeout=30.0, ok_codes=None,
        )  # fmt: skip
        data: dict[str, Any] = json.loads(result.stdout)
        data.setdefault("smartctl", {}).setdefault("exit_status", result.returncode)
        return data

    def _device(self, out: Batch, device: str, data: dict[str, Any]) -> bool:
        """Emits the metrics of one disk; False if smartctl could not read it."""
        status = int(data.get("smartctl", {}).get("exit_status") or 0)
        messages = " ".join(
            str(m.get("string", "")) for m in data.get("smartctl", {}).get("messages", [])
        )
        if "STANDBY" in messages.upper() or data.get("power_mode", {}).get("is_standby"):
            out.add(STANDBY, 1, device=device)
            return True
        out.add(EXIT_STATUS, status, device=device)
        if status & 0b11:  # command line error or device could not be opened
            log.debug("smartctl cannot read %s: %s", device, messages or f"exit status {status}")
            return False
        out.add(STANDBY, 0, device=device)

        model = str(
            data.get("model_name")
            or data.get("scsi_model_name")
            or " ".join(filter(None, (data.get("scsi_vendor"), data.get("scsi_product"))))
            or "unknown"
        )
        serial = str(data.get("serial_number") or "")
        protocol = str(data.get("device", {}).get("protocol") or "")
        out.add(
            INFO,
            1,
            device=device,
            model=model,
            serial=serial,
            firmware=str(data.get("firmware_version") or data.get("scsi_revision") or ""),
            protocol=protocol,
        )
        labels = {"device": device, "model": model}
        passed = data.get("smart_status", {}).get("passed")
        if passed is not None:
            out.add(HEALTHY, 1 if passed else 0, serial=serial, **labels)
        out.add(TEMP, data.get("temperature", {}).get("current"), **labels)
        out.add(POWER_ON_HOURS, data.get("power_on_time", {}).get("hours"), **labels)
        out.add(POWER_CYCLES, data.get("power_cycle_count"), **labels)
        out.add(REALLOCATED, data.get("scsi_grown_defect_list"), **labels)

        attrs = {
            a.get("id"): a for a in data.get("ata_smart_attributes", {}).get("table", []) or []
        }
        for attr_id, spec in _ATA_ATTRS.items():
            if attr_id in attrs:
                out.add(spec, _raw(attrs[attr_id]), **labels)
        out.add(
            ERROR_LOG, data.get("ata_smart_error_log", {}).get("summary", {}).get("count"), **labels
        )

        nvme = data.get("nvme_smart_health_information_log") or {}
        if nvme:
            out.add(SPARE, nvme.get("available_spare"), **labels)
            out.add(CRITICAL_WARNING, nvme.get("critical_warning"), **labels)
            out.add(MEDIA_ERRORS, nvme.get("media_errors"), **labels)
            out.add(UNSAFE_SHUTDOWNS, nvme.get("unsafe_shutdowns"), **labels)
            out.add(ERROR_LOG, nvme.get("num_err_log_entries"), **labels)
            for key, spec in (
                ("data_units_read", READ_BYTES),
                ("data_units_written", WRITTEN_BYTES),
            ):
                if nvme.get(key) is not None:
                    out.add(spec, int(nvme[key]) * _NVME_DATA_UNIT, **labels)
        out.add(WEAROUT, _wearout(data, nvme, attrs), **labels)
        return True


def _raw(attr: dict[str, Any]) -> int | None:
    raw = attr.get("raw", {})
    value = raw.get("value")
    if value is None:
        return None
    # Some vendors pack extra data into the high bytes; the count is the low 32 bits.
    return int(value) & 0xFFFFFFFF


def _wearout(data: dict[str, Any], nvme: dict[str, Any], attrs: dict[Any, Any]) -> float | None:
    used = data.get("endurance_used", {}).get("current_percent")
    if used is not None:
        return float(used)
    if nvme.get("percentage_used") is not None:
        return float(nvme["percentage_used"])
    sas = data.get("scsi_percentage_used_endurance_indicator")
    if sas is not None:
        return float(sas)
    for attr_id, name in _ATA_LIFE_ATTRS.items():
        attr = attrs.get(attr_id)
        if attr and attr.get("name") == name and attr.get("value") is not None:
            return float(max(0, 100 - int(attr["value"])))
    return None
