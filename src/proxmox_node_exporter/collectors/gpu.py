"""GPU metrics: NVIDIA via nvidia-smi, AMD and Intel via the DRM sysfs interface.

GPUs passed through to guests are bound to vfio-pci, have no DRM node and
are therefore (correctly) not reported by the host.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import re

from ..metrics import Batch, MetricGroup
from ..runner import CommandError
from .base import Collector, list_dir, read_int, read_text, to_float

log = logging.getLogger(__name__)

M = MetricGroup("gpu")
_G = ("gpu", "name", "vendor")
INFO = M.gauge(
    "node_gpu_info", "GPU identity; the value is always 1.", *_G, "uuid", "driver_version"
)
COUNT = M.gauge("node_gpu_count", "Number of GPUs.", "vendor")
TEMP = M.gauge("node_gpu_temp_celsius", "GPU temperature.", *_G)
UTIL = M.gauge("node_gpu_utilization_percent", "GPU utilisation (type: gpu, memory).", *_G, "type")
MEM_TOTAL = M.gauge("node_gpu_memory_total_bytes", "GPU memory size.", *_G)
MEM_USED = M.gauge("node_gpu_memory_used_bytes", "GPU memory used.", *_G)
MEM_FREE = M.gauge("node_gpu_memory_free_bytes", "GPU memory free.", *_G)
POWER = M.gauge("node_gpu_power_draw_watts", "GPU power draw.", *_G)
POWER_LIMIT = M.gauge("node_gpu_power_limit_watts", "GPU power limit.", *_G)
CLOCK_GRAPHICS = M.gauge("node_gpu_clock_graphics_hertz", "GPU core clock.", *_G)
CLOCK_MEMORY = M.gauge("node_gpu_clock_memory_hertz", "GPU memory clock.", *_G)
FAN = M.gauge("node_gpu_fan_speed_percent", "GPU fan speed.", *_G)
PCIE_GEN = M.gauge("node_gpu_pcie_link_gen", "Current PCIe link generation.", *_G)
PCIE_WIDTH = M.gauge("node_gpu_pcie_link_width", "Current PCIe link width.", *_G)

_NVIDIA_FIELDS = (
    "index", "name", "uuid", "driver_version", "temperature.gpu", "utilization.gpu",
    "utilization.memory", "memory.total", "memory.used", "memory.free", "power.draw",
    "power.limit", "clocks.gr", "clocks.mem", "fan.speed", "pcie.link.gen.current",
    "pcie.link.width.current",
)  # fmt: skip
_MIB = 1024 * 1024
_MHZ = 1_000_000
_VENDORS = {"0x1002": "amd", "0x8086": "intel"}
_PCIE_GEN = {"2.5": 1, "5.0": 2, "8.0": 3, "16.0": 4, "32.0": 5, "64.0": 6}


class GpuCollector(Collector):
    name = "gpu"
    description = "NVIDIA (nvidia-smi), AMD and Intel (sysfs) GPU utilisation, memory, power"

    def detect(self) -> bool:
        return self.has_command("nvidia-smi") or bool(self._drm_cards())

    def collect(self, out: Batch) -> None:
        counts: dict[str, int] = {}
        cards = self._drm_cards()
        if self.has_command("nvidia-smi"):
            try:
                counts["nvidia"] = self._nvidia(out)
            except CommandError as exc:
                # e.g. every NVIDIA GPU is bound to vfio-pci; still report the others.
                if not cards:
                    raise
                log.debug("nvidia-smi: %s", exc)
        for card, vendor in cards:
            counts[vendor] = counts.get(vendor, 0) + 1
            self._drm(out, card, vendor)
        for vendor, count in counts.items():
            out.add(COUNT, count, vendor=vendor)

    def _nvidia(self, out: Batch) -> int:
        result = self.run(
            "nvidia-smi",
            f"--query-gpu={','.join(_NVIDIA_FIELDS)}",
            "--format=csv,noheader,nounits",
            timeout=15.0,
        )
        count = 0
        for row in csv.reader(io.StringIO(result.stdout), skipinitialspace=True):
            if len(row) != len(_NVIDIA_FIELDS):
                continue
            count += 1
            v = dict(zip(_NVIDIA_FIELDS, (c.strip() for c in row)))
            labels = {"gpu": v["index"], "name": v["name"], "vendor": "nvidia"}
            out.add(INFO, 1, uuid=v["uuid"], driver_version=v["driver_version"], **labels)
            out.add(TEMP, to_float(v["temperature.gpu"]), **labels)
            out.add(UTIL, to_float(v["utilization.gpu"]), type="gpu", **labels)
            out.add(UTIL, to_float(v["utilization.memory"]), type="memory", **labels)
            for key, spec in (
                ("memory.total", MEM_TOTAL),
                ("memory.used", MEM_USED),
                ("memory.free", MEM_FREE),
            ):
                out.add(spec, _scaled(v[key], _MIB), **labels)
            out.add(POWER, to_float(v["power.draw"]), **labels)
            out.add(POWER_LIMIT, to_float(v["power.limit"]), **labels)
            out.add(CLOCK_GRAPHICS, _scaled(v["clocks.gr"], _MHZ), **labels)
            out.add(CLOCK_MEMORY, _scaled(v["clocks.mem"], _MHZ), **labels)
            out.add(FAN, to_float(v["fan.speed"]), **labels)
            out.add(PCIE_GEN, to_float(v["pcie.link.gen.current"]), **labels)
            out.add(PCIE_WIDTH, to_float(v["pcie.link.width.current"]), **labels)
        return count

    def _drm_cards(self) -> list[tuple[str, str]]:
        base = self.sys_path("class", "drm")
        cards = []
        for entry in list_dir(base):
            if not re.match(r"^card\d+$", entry):
                continue
            vendor = _VENDORS.get(read_text(os.path.join(base, entry, "device", "vendor")) or "")
            if vendor:
                cards.append((entry, vendor))
        return cards

    def _drm(self, out: Batch, card: str, vendor: str) -> None:
        card_dir = self.sys_path("class", "drm", card)
        dev = os.path.join(card_dir, "device")
        device_id = read_text(os.path.join(dev, "device")) or "unknown"
        name = read_text(os.path.join(dev, "product_name")) or f"{vendor.upper()} {device_id}"
        labels = {"gpu": card[4:], "name": name, "vendor": vendor}
        out.add(
            INFO,
            1,
            uuid=read_text(os.path.join(dev, "unique_id")) or "",
            driver_version="",
            **labels,
        )

        out.add(UTIL, read_int(os.path.join(dev, "gpu_busy_percent")), type="gpu", **labels)
        out.add(UTIL, read_int(os.path.join(dev, "mem_busy_percent")), type="memory", **labels)
        total = read_int(os.path.join(dev, "mem_info_vram_total"))
        used = read_int(os.path.join(dev, "mem_info_vram_used"))
        out.add(MEM_TOTAL, total, **labels)
        out.add(MEM_USED, used, **labels)
        if total is not None and used is not None:
            out.add(MEM_FREE, total - used, **labels)

        link_speed = read_text(os.path.join(dev, "current_link_speed")) or ""
        out.add(PCIE_GEN, _PCIE_GEN.get(link_speed.split(" ")[0]), **labels)
        out.add(PCIE_WIDTH, read_int(os.path.join(dev, "current_link_width")), **labels)

        # Intel exposes the current GT frequency on the card itself.
        mhz = read_int(os.path.join(card_dir, "gt_cur_freq_mhz"))
        if mhz is not None:
            out.add(CLOCK_GRAPHICS, mhz * _MHZ, **labels)

        for hwmon in list_dir(os.path.join(dev, "hwmon")):
            self._drm_hwmon(out, os.path.join(dev, "hwmon", hwmon), labels)

    @staticmethod
    def _drm_hwmon(out: Batch, path: str, labels: dict[str, str]) -> None:
        temp = read_int(os.path.join(path, "temp1_input"))
        if temp is not None:
            out.add(TEMP, temp / 1000, **labels)
        power = read_int(os.path.join(path, "power1_input"))
        if power is None:
            power = read_int(os.path.join(path, "power1_average"))
        if power is not None:
            out.add(POWER, power / 1_000_000, **labels)
        cap = read_int(os.path.join(path, "power1_cap"))
        if cap:
            out.add(POWER_LIMIT, cap / 1_000_000, **labels)
        for name, spec in (("freq1_input", CLOCK_GRAPHICS), ("freq2_input", CLOCK_MEMORY)):
            hz = read_int(os.path.join(path, name))
            if hz is not None:
                out.add(spec, hz, **labels)
        pwm = read_int(os.path.join(path, "pwm1"))
        pwm_max = read_int(os.path.join(path, "pwm1_max")) or 255
        if pwm is not None:
            out.add(FAN, round(pwm / pwm_max * 100, 1), **labels)


def _scaled(text: str, factor: float) -> float | None:
    value = to_float(text)
    return None if value is None else value * factor
