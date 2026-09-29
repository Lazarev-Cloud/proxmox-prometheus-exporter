from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from conftest import collect, fixture, value, write_tree
from proxmox_node_exporter.collectors.base import Context
from proxmox_node_exporter.collectors.network import NetworkCollector

TCP_STATES = {
    "established",
    "syn_sent",
    "syn_recv",
    "fin_wait1",
    "fin_wait2",
    "time_wait",
    "close",
    "close_wait",
    "last_ack",
    "listen",
    "closing",
    "new_syn_recv",
}


def iface(
    name: str,
    operstate: str,
    flags: str,
    mtu: int,
    carrier: str | None = None,
    speed: str | None = None,
) -> dict[str, str]:
    base = f"sys/class/net/{name}"
    files = {
        f"{base}/operstate": operstate + "\n",
        f"{base}/flags": flags + "\n",
        f"{base}/mtu": f"{mtu}\n",
        f"{base}/address": "bc:24:11:00:00:01\n",
        f"{base}/tx_queue_len": "1000\n",
    }
    if carrier is not None:
        files[f"{base}/carrier"] = carrier + "\n"
    if speed is not None:
        files[f"{base}/speed"] = speed + "\n"
    return files


def sysfs() -> dict[str, str]:
    return {
        "sys/class/net/bonding_masters": "bond0 bond1\n",
        **iface("lo", "unknown", "0x9", 65536, carrier="1"),
        # bond members: eno1 has link, eno2 is up but without a cable
        **iface("eno1", "up", "0x1803", 9000, carrier="1", speed="10000"),
        "sys/class/net/eno1/bonding_slave/mii_status": "up\n",
        "sys/class/net/eno1/bonding_slave/state": "active\n",
        **iface("eno2", "down", "0x1803", 9000, carrier="0", speed="-1"),
        "sys/class/net/eno2/bonding_slave/mii_status": "down\n",
        "sys/class/net/eno2/bonding_slave/state": "backup\n",
        **iface("bond0", "up", "0x1403", 9000, carrier="1", speed="10000"),
        "sys/class/net/bond0/bonding/slaves": "eno1 eno2\n",
        "sys/class/net/bond0/bonding/mode": "802.3ad 4\n",
        # an administratively down bond without members: carrier and speed
        # cannot be read (EINVAL), so the files are missing here
        **iface("bond1", "down", "0x1402", 1500),
        "sys/class/net/bond1/bonding/slaves": "\n",
        **iface("vmbr0", "up", "0x1003", 1500, carrier="1"),
        "sys/class/net/vmbr0/bridge/stp_state": "0\n",
        # tap devices report operstate "unknown"; IFF_UP decides
        **iface("tap100i0", "unknown", "0x1103", 1500, carrier="1", speed="10"),
        **iface("fwbr100i0", "up", "0x1003", 1500, carrier="1"),
        **iface("fwpr100p0", "up", "0x1103", 1500, carrier="1", speed="10000"),
        **iface("fwln100i0", "up", "0x1103", 1500, carrier="1", speed="10000"),
        **iface("veth101i0", "up", "0x1103", 1500, carrier="1", speed="10000"),
    }


@pytest.fixture
def host(tmp_path: Path) -> Path:
    write_tree(
        tmp_path,
        {
            "proc/net/dev": fixture("network/net-dev.txt"),
            "proc/net/tcp": fixture("network/net-tcp.txt"),
            "proc/net/tcp6": fixture("network/net-tcp6.txt"),
            "proc/net/udp": fixture("network/net-udp.txt"),
            "proc/net/udp6": fixture("network/net-udp6.txt"),
            **sysfs(),
        },
    )
    return tmp_path


def devices(samples: dict[str, dict[frozenset[tuple[str, str]], float]], name: str) -> set[str]:
    return {dict(key)["device"] for key in samples.get(name, {})}


def test_detect(tmp_path: Path, make_ctx: Callable[..., Context]) -> None:
    ctx = make_ctx()
    assert not NetworkCollector(ctx).detect()
    write_tree(tmp_path, {"proc/net/dev": fixture("network/net-dev.txt")})
    assert NetworkCollector(ctx).detect()


def test_missing_net_dev_raises(make_ctx: Callable[..., Context]) -> None:
    with pytest.raises(RuntimeError, match="/proc/net/dev"):
        collect(NetworkCollector(make_ctx()))


@pytest.mark.usefixtures("host")
def test_interface_counters(make_ctx: Callable[..., Context]) -> None:
    samples = collect(NetworkCollector(make_ctx()))
    expected = {
        "node_network_receive_bytes_total": 98765432109,
        "node_network_receive_packets_total": 76543210,
        "node_network_receive_errs_total": 12,
        "node_network_receive_drop_total": 34,
        "node_network_receive_multicast_total": 56789,
        "node_network_transmit_bytes_total": 12345678901,
        "node_network_transmit_packets_total": 23456789,
        "node_network_transmit_errs_total": 1,
        "node_network_transmit_drop_total": 2,
    }
    for name, want in expected.items():
        assert value(samples, name, device="eno1") == want, name
    assert value(samples, "node_network_receive_drop_total", device="vmbr0") == 123
    assert value(samples, "node_network_receive_multicast_total", device="vmbr0") == 456789
    assert value(samples, "node_network_transmit_drop_total", device="tap100i0") == 17
    assert value(samples, "node_network_transmit_bytes_total", device="tap100i0") == 9876543210
    assert value(samples, "node_network_receive_bytes_total", device="eno2") == 0


@pytest.mark.usefixtures("host")
def test_old_format_without_space_after_colon(make_ctx: Callable[..., Context]) -> None:
    samples = collect(NetworkCollector(make_ctx()))
    assert value(samples, "node_network_receive_bytes_total", device="eth9") == 4294967296
    assert value(samples, "node_network_receive_packets_total", device="eth9") == 3000000
    assert value(samples, "node_network_receive_multicast_total", device="eth9") == 70
    assert value(samples, "node_network_transmit_packets_total", device="eth9") == 20
    # no sysfs directory for it: no link metrics
    for name in (
        "node_network_up",
        "node_network_carrier",
        "node_network_mtu_bytes",
        "node_network_speed_bytes",
    ):
        assert value(samples, name, device="eth9") is None


@pytest.mark.usefixtures("host")
def test_default_exclusions(make_ctx: Callable[..., Context]) -> None:
    samples = collect(NetworkCollector(make_ctx()))
    wanted = {"eno1", "eno2", "bond0", "bond1", "vmbr0", "tap100i0", "veth101i0", "eth9"}
    assert devices(samples, "node_network_receive_bytes_total") == wanted
    for name, family in samples.items():
        for key in family:
            labels = dict(key)
            if "device" in labels:
                assert labels["device"] in wanted, name


@pytest.mark.usefixtures("host")
def test_link_state(make_ctx: Callable[..., Context]) -> None:
    samples = collect(NetworkCollector(make_ctx()))
    up = {dict(k)["device"]: v for k, v in samples["node_network_up"].items()}
    assert up == {
        "eno1": 1,
        "eno2": 0,  # IFF_UP set, but operstate "down" (no link)
        "bond0": 1,
        "bond1": 0,
        "vmbr0": 1,
        "tap100i0": 1,  # operstate "unknown" with IFF_UP
        "veth101i0": 1,
    }
    carrier = {dict(k)["device"]: v for k, v in samples["node_network_carrier"].items()}
    assert carrier == {
        "eno1": 1,
        "eno2": 0,
        "bond0": 1,
        "vmbr0": 1,
        "tap100i0": 1,
        "veth101i0": 1,
    }
    assert value(samples, "node_network_mtu_bytes", device="eno1") == 9000
    assert value(samples, "node_network_mtu_bytes", device="bond1") == 1500


@pytest.mark.usefixtures("host")
def test_speed_in_bytes_per_second(make_ctx: Callable[..., Context]) -> None:
    samples = collect(NetworkCollector(make_ctx()))
    speed = {dict(k)["device"]: v for k, v in samples["node_network_speed_bytes"].items()}
    # Mbit/s * 125000 = bytes/s; "-1" (unknown) and unreadable speeds are skipped
    assert speed == {
        "eno1": 1_250_000_000,
        "bond0": 1_250_000_000,
        "tap100i0": 1_250_000,
        "veth101i0": 1_250_000_000,
    }


@pytest.mark.parametrize(
    ("operstate", "flags", "expected"),
    [
        ("up", "0x1003", 1),
        ("up", "0x1002", 1),
        ("down", "0x1003", 0),
        ("unknown", "0x1003", 1),
        ("unknown", "0x1091", 1),
        ("unknown", "0x1002", 0),
        ("unknown", None, 0),
        ("dormant", "0x1003", 0),
        ("lowerlayerdown", "0x1003", 0),
        ("notpresent", "0x1003", 0),
        (None, "0x1003", None),
    ],
)
def test_up_rule(
    tmp_path: Path,
    make_ctx: Callable[..., Context],
    operstate: str | None,
    flags: str | None,
    expected: int | None,
) -> None:
    files = {
        "proc/net/dev": (
            "Inter-|   Receive                                                |  Transmit\n"
            " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets "
            "errs drop fifo colls carrier compressed\n"
            "tap200i1: 1 2 0 0 0 0 0 0 3 4 0 0 0 0 0 0\n"
        ),
        "sys/class/net/tap200i1/mtu": "1500\n",
    }
    if operstate is not None:
        files["sys/class/net/tap200i1/operstate"] = operstate + "\n"
    if flags is not None:
        files["sys/class/net/tap200i1/flags"] = flags + "\n"
    write_tree(tmp_path, files)
    samples = collect(NetworkCollector(make_ctx()))
    assert value(samples, "node_network_up", device="tap200i1") == expected


@pytest.mark.usefixtures("host")
def test_bonds(make_ctx: Callable[..., Context]) -> None:
    samples = collect(NetworkCollector(make_ctx()))
    assert samples["node_bonding_slaves"] == {
        frozenset({("master", "bond0")}): 2,
        frozenset({("master", "bond1")}): 0,
    }
    assert samples["node_bonding_active"] == {
        frozenset({("master", "bond0")}): 1,
        frozenset({("master", "bond1")}): 0,
    }


@pytest.mark.usefixtures("host")
def test_socket_states(make_ctx: Callable[..., Context]) -> None:
    samples = collect(NetworkCollector(make_ctx()))
    tcp = {dict(k)["state"]: v for k, v in samples["node_network_tcp_connections"].items()}
    assert set(tcp) == TCP_STATES  # every state is always exported
    assert tcp == {
        **dict.fromkeys(TCP_STATES, 0),
        "listen": 5,  # 3 IPv4 + 2 IPv6
        "established": 3,  # 2 IPv4 + 1 IPv6
        "time_wait": 1,
        "close_wait": 1,
        "syn_recv": 1,
        "fin_wait1": 1,
    }
    assert value(samples, "node_network_udp_sockets") == 4


def test_lowercase_hex_state(tmp_path: Path, make_ctx: Callable[..., Context]) -> None:
    tcp = fixture("network/net-tcp.txt").replace(" 0A ", " 0a ")
    write_tree(tmp_path, {"proc/net/dev": fixture("network/net-dev.txt"), "proc/net/tcp": tcp})
    samples = collect(NetworkCollector(make_ctx()))
    assert value(samples, "node_network_tcp_connections", state="listen") == 3


def test_without_socket_tables(tmp_path: Path, make_ctx: Callable[..., Context]) -> None:
    write_tree(
        tmp_path,
        {
            "proc/net/dev": fixture("network/net-dev.txt"),
            # IPv6 disabled: only the header is left
            "proc/net/tcp6": fixture("network/net-tcp6.txt").splitlines()[0] + "\n",
        },
    )
    samples = collect(NetworkCollector(make_ctx()))
    tcp = samples["node_network_tcp_connections"]
    assert len(tcp) == len(TCP_STATES)
    assert set(tcp.values()) == {0}
    assert value(samples, "node_network_udp_sockets") == 0
    # no sysfs at all: counters only
    assert "node_network_up" not in samples
    assert "node_bonding_slaves" not in samples


@pytest.mark.usefixtures("host")
def test_custom_exclude(make_ctx: Callable[..., Context]) -> None:
    samples = collect(NetworkCollector(make_ctx(network_device_exclude=r"^(tap|veth|fw)")))
    assert devices(samples, "node_network_receive_bytes_total") == {
        "lo",
        "eno1",
        "eno2",
        "bond0",
        "bond1",
        "vmbr0",
        "eth9",
    }
    assert value(samples, "node_network_up", device="lo") == 1


@pytest.mark.parametrize(
    ("device", "excluded"),
    [
        ("lo", True),
        ("fwbr100i0", True),
        ("fwln100i0", True),
        ("fwpr100p0", True),
        ("fwbr1234i12", True),
        ("fwpr999p3", True),
        ("eno1", False),
        ("vmbr0", False),
        ("vmbr0.100", False),
        ("tap100i0", False),
        ("veth101i0", False),
        ("fwbr100", False),
        ("lo0", False),
        ("bond0", False),
    ],
)
def test_default_exclude_regex(
    tmp_path: Path, make_ctx: Callable[..., Context], device: str, excluded: bool
) -> None:
    write_tree(tmp_path, {"proc/net/dev": f"{device}: 1 2 0 0 0 0 0 0 3 4 0 0 0 0 0 0\n"})
    samples = collect(NetworkCollector(make_ctx()))
    got = value(samples, "node_network_receive_bytes_total", device=device)
    assert got == (None if excluded else 1)
