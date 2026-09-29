"""Network interface counters, link state, bonds and socket states."""

from __future__ import annotations

import os
import re

from ..metrics import Batch, MetricGroup
from .base import Collector, Context, list_dir, read_int, read_text

M = MetricGroup("network")
_RX = {
    0: M.counter("node_network_receive_bytes_total", "Bytes received.", "device"),
    1: M.counter("node_network_receive_packets_total", "Packets received.", "device"),
    2: M.counter("node_network_receive_errs_total", "Receive errors.", "device"),
    3: M.counter("node_network_receive_drop_total", "Received packets dropped.", "device"),
    7: M.counter("node_network_receive_multicast_total", "Multicast packets received.", "device"),
}
_TX = {
    0: M.counter("node_network_transmit_bytes_total", "Bytes sent.", "device"),
    1: M.counter("node_network_transmit_packets_total", "Packets sent.", "device"),
    2: M.counter("node_network_transmit_errs_total", "Transmit errors.", "device"),
    3: M.counter("node_network_transmit_drop_total", "Transmitted packets dropped.", "device"),
}
UP = M.gauge("node_network_up", "Whether the interface is up.", "device")
CARRIER = M.gauge("node_network_carrier", "Whether the interface has link.", "device")
MTU = M.gauge("node_network_mtu_bytes", "Interface MTU.", "device")
SPEED = M.gauge("node_network_speed_bytes", "Negotiated link speed in bytes per second.", "device")
BOND_SLAVES = M.gauge("node_bonding_slaves", "Number of interfaces in a bond.", "master")
BOND_ACTIVE = M.gauge("node_bonding_active", "Number of bond members with link up.", "master")
TCP = M.gauge("node_network_tcp_connections", "TCP sockets by state.", "state")
UDP = M.gauge("node_network_udp_sockets", "Open UDP sockets.")

_TCP_STATES = {
    "01": "established",
    "02": "syn_sent",
    "03": "syn_recv",
    "04": "fin_wait1",
    "05": "fin_wait2",
    "06": "time_wait",
    "07": "close",
    "08": "close_wait",
    "09": "last_ack",
    "0A": "listen",
    "0B": "closing",
    "0C": "new_syn_recv",
}
_IFF_UP = 0x1


class NetworkCollector(Collector):
    name = "network"
    description = "Network traffic, errors, link state, bonds, TCP states"

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        self._exclude = re.compile(self.settings.network_device_exclude)

    def detect(self) -> bool:
        return os.path.exists(self.proc_path("net", "dev"))

    def collect(self, out: Batch) -> None:
        text = read_text(self.proc_path("net", "dev"))
        if text is None:
            raise RuntimeError("cannot read /proc/net/dev")
        for line in text.splitlines():
            name, sep, rest = line.partition(":")
            if not sep or "|" in line:
                continue
            device = name.strip()
            if self._exclude.search(device):
                continue
            fields = rest.split()
            if len(fields) < 16:
                continue
            for index, spec in _RX.items():
                out.add(spec, int(fields[index]), device=device)
            for index, spec in _TX.items():
                out.add(spec, int(fields[8 + index]), device=device)
            self._link(out, device)
        self._bonds(out)
        self._sockets(out)

    def _link(self, out: Batch, device: str) -> None:
        base = self.sys_path("class", "net", device)
        operstate = read_text(os.path.join(base, "operstate"))
        flags_text = read_text(os.path.join(base, "flags"))
        flags = int(flags_text, 16) if flags_text else 0
        if operstate is not None:
            # tap/veth devices often report "unknown"; fall back to IFF_UP.
            up = operstate == "up" or (operstate == "unknown" and bool(flags & _IFF_UP))
            out.add(UP, 1 if up else 0, device=device)
        out.add(CARRIER, read_int(os.path.join(base, "carrier")), device=device)
        out.add(MTU, read_int(os.path.join(base, "mtu")), device=device)
        speed = read_int(os.path.join(base, "speed"))
        if speed is not None and speed > 0:
            out.add(SPEED, speed * 125_000, device=device)

    def _bonds(self, out: Batch) -> None:
        net = self.sys_path("class", "net")
        for master in list_dir(net):
            slaves = read_text(os.path.join(net, master, "bonding", "slaves"))
            if slaves is None:
                continue
            members = slaves.split()
            active = sum(
                1
                for slave in members
                if read_text(os.path.join(net, slave, "bonding_slave", "mii_status")) == "up"
            )
            out.add(BOND_SLAVES, len(members), master=master)
            out.add(BOND_ACTIVE, active, master=master)

    def _sockets(self, out: Batch) -> None:
        counts: dict[str, int] = dict.fromkeys(_TCP_STATES.values(), 0)
        for name in ("tcp", "tcp6"):
            for line in (read_text(self.proc_path("net", name)) or "").splitlines()[1:]:
                fields = line.split()
                if len(fields) > 3:
                    state = _TCP_STATES.get(fields[3].upper())
                    if state is not None:
                        counts[state] += 1
        for state, count in counts.items():
            out.add(TCP, count, state=state)
        udp = 0
        for name in ("udp", "udp6"):
            text = read_text(self.proc_path("net", name))
            if text:
                udp += max(0, len(text.splitlines()) - 1)
        out.add(UDP, udp)
