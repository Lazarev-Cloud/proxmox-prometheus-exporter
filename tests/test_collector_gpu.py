from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from conftest import FakeRunner, collect, fixture, value, write_tree
from proxmox_node_exporter.collectors.base import Context
from proxmox_node_exporter.collectors.gpu import GpuCollector
from proxmox_node_exporter.runner import CommandError, CommandResult

NVIDIA_SMI = (
    "nvidia-smi",
    "--query-gpu=index,name,uuid,driver_version,temperature.gpu,utilization.gpu,"
    "utilization.memory,memory.total,memory.used,memory.free,power.draw,power.limit,"
    "clocks.gr,clocks.mem,fan.speed,pcie.link.gen.current,pcie.link.width.current",
    "--format=csv,noheader,nounits",
)
MIB = 1024 * 1024
RTX = {"gpu": "0", "name": "NVIDIA GeForce RTX 3090", "vendor": "nvidia"}
GT = {"gpu": "1", "name": "NVIDIA GeForce GT 1030", "vendor": "nvidia"}
AMD = {"gpu": "0", "name": "AMD 0x73bf", "vendor": "amd"}
INTEL = {"gpu": "1", "name": "INTEL 0x46a6", "vendor": "intel"}


def amd_card(card: str = "card0", **extra: str) -> dict[str, str]:
    """sysfs of an amdgpu card (Radeon RX 6800 XT), as seen through /sys/class/drm."""
    dev = f"class/drm/{card}/device"
    hwmon = f"{dev}/hwmon/hwmon3"
    files = {
        f"{dev}/vendor": "0x1002\n",
        f"{dev}/device": "0x73bf\n",
        f"{dev}/unique_id": "a1b2c3d4e5f60718\n",
        f"{dev}/gpu_busy_percent": "12\n",
        f"{dev}/mem_busy_percent": "3\n",
        f"{dev}/mem_info_vram_total": "17163091968\n",
        f"{dev}/mem_info_vram_used": "1234567168\n",
        f"{dev}/current_link_speed": "16.0 GT/s PCIe\n",
        f"{dev}/current_link_width": "16\n",
        f"{hwmon}/name": "amdgpu\n",
        f"{hwmon}/temp1_input": "45000\n",
        f"{hwmon}/temp1_label": "edge\n",
        f"{hwmon}/power1_average": "35000000\n",
        f"{hwmon}/power1_cap": "250000000\n",
        f"{hwmon}/pwm1": "76\n",
        f"{hwmon}/pwm1_max": "255\n",
        f"{hwmon}/freq1_input": "1850000000\n",
        f"{hwmon}/freq2_input": "1000000000\n",
        # connectors and render nodes live next to the cards
        f"class/drm/{card}-DP-1/status": "connected\n",
        "class/drm/renderD128/dev": "226:128\n",
        "class/drm/version": "drm 1.1.0 20060810\n",
    }
    files.update({f"{dev}/{name}": text for name, text in extra.items()})
    return files


INTEL_CARD = {
    "class/drm/card1/device/vendor": "0x8086\n",
    "class/drm/card1/device/device": "0x46a6\n",
    "class/drm/card1/gt_cur_freq_mhz": "1100\n",
    "class/drm/card1-eDP-1/status": "connected\n",
}
OTHER_CARDS = {
    "class/drm/card2/device/vendor": "0x1a03\n",  # ASPEED BMC VGA
    "class/drm/card3/device/vendor": "0x10de\n",  # NVIDIA through nvidia-drm/nouveau
}


def sysfs(tmp_path: Path, *trees: dict[str, str]) -> None:
    for tree in trees:
        write_tree(tmp_path / "sys", tree)


# -- detection -------------------------------------------------------------------------


def test_detect_nvidia(make_ctx: Callable[..., Context]) -> None:
    assert GpuCollector(make_ctx(FakeRunner({NVIDIA_SMI: ""}), is_root=False)).detect()


def test_detect_drm(make_ctx: Callable[..., Context], tmp_path: Path) -> None:
    sysfs(tmp_path, amd_card())
    assert GpuCollector(make_ctx(is_root=False)).detect()


def test_detect_nothing(make_ctx: Callable[..., Context], tmp_path: Path) -> None:
    sysfs(tmp_path, OTHER_CARDS)
    assert not GpuCollector(make_ctx()).detect()


# -- NVIDIA ----------------------------------------------------------------------------


def test_nvidia(make_ctx: Callable[..., Context]) -> None:
    runner = FakeRunner({NVIDIA_SMI: fixture("gpu/nvidia_smi.csv")})
    samples = collect(GpuCollector(make_ctx(runner)))
    assert runner.calls == [NVIDIA_SMI]
    info = dict(RTX, uuid="GPU-5a3d7c1e-8b2f-4c6a-9e1d-2f7b8c9a0d1e", driver_version="550.107.02")
    assert value(samples, "node_gpu_info", **info) == 1
    assert value(samples, "node_gpu_temp_celsius", **RTX) == 46
    assert value(samples, "node_gpu_utilization_percent", **RTX, type="gpu") == 17
    assert value(samples, "node_gpu_utilization_percent", **RTX, type="memory") == 6
    assert value(samples, "node_gpu_memory_total_bytes", **RTX) == 24576 * MIB
    assert value(samples, "node_gpu_memory_used_bytes", **RTX) == 1843 * MIB
    assert value(samples, "node_gpu_memory_free_bytes", **RTX) == 22346 * MIB
    assert value(samples, "node_gpu_power_draw_watts", **RTX) == 108.34
    assert value(samples, "node_gpu_power_limit_watts", **RTX) == 350
    assert value(samples, "node_gpu_clock_graphics_hertz", **RTX) == 1395e6
    assert value(samples, "node_gpu_clock_memory_hertz", **RTX) == 9751e6
    assert value(samples, "node_gpu_fan_speed_percent", **RTX) == 34
    assert value(samples, "node_gpu_pcie_link_gen", **RTX) == 4
    assert value(samples, "node_gpu_pcie_link_width", **RTX) == 16
    assert value(samples, "node_gpu_count", vendor="nvidia") == 2


def test_nvidia_unavailable_values_are_omitted(make_ctx: Callable[..., Context]) -> None:
    runner = FakeRunner({NVIDIA_SMI: fixture("gpu/nvidia_smi.csv")})
    samples = collect(GpuCollector(make_ctx(runner)))
    assert value(samples, "node_gpu_temp_celsius", **GT) == 41
    assert value(samples, "node_gpu_memory_total_bytes", **GT) == 2048 * MIB
    assert value(samples, "node_gpu_pcie_link_width", **GT) == 4
    # "[N/A]" and "[Not Supported]"
    assert value(samples, "node_gpu_power_draw_watts", **GT) is None
    assert value(samples, "node_gpu_power_limit_watts", **GT) is None
    assert value(samples, "node_gpu_fan_speed_percent", **GT) is None


def test_nvidia_malformed_rows_are_skipped(make_ctx: Callable[..., Context]) -> None:
    rtx = fixture("gpu/nvidia_smi.csv").splitlines()[0]
    runner = FakeRunner({NVIDIA_SMI: f"{rtx}\n1, broken row\n\n"})
    samples = collect(GpuCollector(make_ctx(runner)))
    assert value(samples, "node_gpu_count", vendor="nvidia") == 1
    assert {dict(k)["gpu"] for k in samples["node_gpu_info"]} == {"0"}


def test_nvidia_failure_without_other_gpus_fails(make_ctx: Callable[..., Context]) -> None:
    runner = FakeRunner({NVIDIA_SMI: CommandResult(6, "No devices were found\n", "")})
    with pytest.raises(CommandError):
        collect(GpuCollector(make_ctx(runner)))


def test_nvidia_failure_keeps_other_vendors(
    make_ctx: Callable[..., Context], tmp_path: Path
) -> None:
    # nvidia-smi is installed but every NVIDIA GPU is bound to vfio-pci for passthrough.
    sysfs(tmp_path, amd_card(), INTEL_CARD)
    runner = FakeRunner({NVIDIA_SMI: CommandResult(6, "No devices were found\n", "")})
    samples = collect(GpuCollector(make_ctx(runner)))
    assert value(samples, "node_gpu_count", vendor="amd") == 1
    assert value(samples, "node_gpu_count", vendor="intel") == 1
    assert value(samples, "node_gpu_count", vendor="nvidia") is None
    assert value(samples, "node_gpu_temp_celsius", **AMD) == 45


# -- AMD / Intel -----------------------------------------------------------------------


def test_amd(make_ctx: Callable[..., Context], tmp_path: Path) -> None:
    sysfs(tmp_path, amd_card())
    samples = collect(GpuCollector(make_ctx()))
    assert value(samples, "node_gpu_info", **AMD, uuid="a1b2c3d4e5f60718", driver_version="") == 1
    assert value(samples, "node_gpu_utilization_percent", **AMD, type="gpu") == 12
    assert value(samples, "node_gpu_utilization_percent", **AMD, type="memory") == 3
    assert value(samples, "node_gpu_memory_total_bytes", **AMD) == 17163091968
    assert value(samples, "node_gpu_memory_used_bytes", **AMD) == 1234567168
    assert value(samples, "node_gpu_memory_free_bytes", **AMD) == 17163091968 - 1234567168
    assert value(samples, "node_gpu_pcie_link_gen", **AMD) == 4  # "16.0 GT/s PCIe"
    assert value(samples, "node_gpu_pcie_link_width", **AMD) == 16
    assert value(samples, "node_gpu_temp_celsius", **AMD) == 45.0  # millidegrees
    assert value(samples, "node_gpu_power_draw_watts", **AMD) == 35.0  # microwatts
    assert value(samples, "node_gpu_power_limit_watts", **AMD) == 250.0
    assert value(samples, "node_gpu_fan_speed_percent", **AMD) == 29.8  # pwm 76/255
    assert value(samples, "node_gpu_clock_graphics_hertz", **AMD) == 1850000000
    assert value(samples, "node_gpu_clock_memory_hertz", **AMD) == 1000000000
    assert value(samples, "node_gpu_count", vendor="amd") == 1
    assert len(samples["node_gpu_info"]) == 1


def test_amd_product_name_and_instant_power(
    make_ctx: Callable[..., Context], tmp_path: Path
) -> None:
    # Newer kernels expose power1_input (RDNA3) and boards with a FRU EEPROM a product name.
    sysfs(
        tmp_path,
        amd_card(
            product_name="Radeon Pro W7600\n",
            **{"hwmon/hwmon3/power1_input": "42000000\n", "hwmon/hwmon3/pwm1_max": "200\n"},
        ),
    )
    samples = collect(GpuCollector(make_ctx()))
    labels = {"gpu": "0", "name": "Radeon Pro W7600", "vendor": "amd"}
    assert value(samples, "node_gpu_power_draw_watts", **labels) == 42.0
    assert value(samples, "node_gpu_fan_speed_percent", **labels) == 38.0  # 76/200


def test_amd_link_speeds(make_ctx: Callable[..., Context], tmp_path: Path) -> None:
    for n, speed in enumerate(
        ["2.5 GT/s PCIe", "5.0 GT/s PCIe", "8.0 GT/s PCIe", "32.0 GT/s PCIe"]
    ):
        sysfs(tmp_path, amd_card(f"card{n}", current_link_speed=f"{speed}\n"))
    sysfs(tmp_path, amd_card("card4", current_link_speed="Unknown\n"))
    samples = collect(GpuCollector(make_ctx()))
    gens = {dict(k)["gpu"]: v for k, v in samples["node_gpu_pcie_link_gen"].items()}
    assert gens == {"0": 1, "1": 2, "2": 3, "3": 5}
    assert value(samples, "node_gpu_count", vendor="amd") == 5


def test_intel(make_ctx: Callable[..., Context], tmp_path: Path) -> None:
    sysfs(tmp_path, INTEL_CARD, OTHER_CARDS)
    samples = collect(GpuCollector(make_ctx()))
    assert value(samples, "node_gpu_info", **INTEL, uuid="", driver_version="") == 1
    assert value(samples, "node_gpu_clock_graphics_hertz", **INTEL) == 1100e6
    assert value(samples, "node_gpu_count", vendor="intel") == 1
    assert "node_gpu_memory_total_bytes" not in samples
    # ASPEED and nvidia-drm cards are not counted
    assert {dict(k)["vendor"] for k in samples["node_gpu_count"]} == {"intel"}


def test_all_vendors(make_ctx: Callable[..., Context], tmp_path: Path) -> None:
    sysfs(tmp_path, amd_card(), INTEL_CARD, OTHER_CARDS)
    runner = FakeRunner({NVIDIA_SMI: fixture("gpu/nvidia_smi.csv")})
    samples = collect(GpuCollector(make_ctx(runner)))
    counts = {dict(k)["vendor"]: v for k, v in samples["node_gpu_count"].items()}
    assert counts == {"nvidia": 2, "amd": 1, "intel": 1}
    # NVIDIA index 0 and AMD card0 share gpu="0" but stay distinct series.
    assert value(samples, "node_gpu_temp_celsius", **RTX) == 46
    assert value(samples, "node_gpu_temp_celsius", **AMD) == 45
