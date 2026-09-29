#!/usr/bin/env python3
"""Generate grafana/proxmox-health.json (guests, storage, ZFS, SMART, hardware).

The dashboard is generated so that it stays consistent with the metric
catalog; tests check that every query only uses metrics the exporter emits.

    python3 tools/gen_health_dashboard.py [--check]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "grafana" / "proxmox-health.json"
DS = {"type": "prometheus", "uid": "${datasource}"}
SEL = 'instance=~"$node"'

Panel = dict[str, Any]


class Layout:
    def __init__(self) -> None:
        self.panels: list[Panel] = []
        self.x = 0
        self.y = 0
        self.row_height = 0
        self.next_id = 1

    def row(self, title: str) -> None:
        self._newline()
        self.panels.append(
            {
                "type": "row",
                "title": title,
                "id": self._id(),
                "collapsed": False,
                "gridPos": {"h": 1, "w": 24, "x": 0, "y": self.y},
                "panels": [],
            }
        )
        self.y += 1

    def add(self, panel: Panel, w: int, h: int) -> None:
        if self.x + w > 24:
            self._newline()
        panel["id"] = self._id()
        panel["gridPos"] = {"h": h, "w": w, "x": self.x, "y": self.y}
        panel.setdefault("datasource", dict(DS))
        self.panels.append(panel)
        self.x += w
        self.row_height = max(self.row_height, h)

    def _newline(self) -> None:
        if self.x:
            self.y += self.row_height
        self.x = 0
        self.row_height = 0

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id - 1


def target(expr: str, legend: str = "", ref: str = "A", *, table: bool = False) -> dict[str, Any]:
    t: dict[str, Any] = {"datasource": dict(DS), "expr": expr, "refId": ref, "legendFormat": legend}
    if table:
        t.update({"format": "table", "instant": True, "range": False})
    return t


def thresholds(*steps: tuple[float | None, str]) -> dict[str, Any]:
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for v, c in steps]}


def stat(title: str, expr: str, unit: str = "none", steps: Any = None, mappings: Any = None,
         description: str = "") -> Panel:  # fmt: skip
    return {
        "type": "stat",
        "title": title,
        "description": description,
        "targets": [target(expr)],
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "background",
            "graphMode": "none",
            "textMode": "auto",
        },
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "thresholds": steps or thresholds((None, "green")),
                "mappings": mappings or [],
                "color": {"mode": "thresholds"},
            },
            "overrides": [],
        },
    }


def timeseries(
    title: str, targets: list[dict[str, Any]], unit: str, description: str = ""
) -> Panel:
    return {
        "type": "timeseries",
        "title": title,
        "description": description,
        "targets": targets,
        "options": {
            "legend": {
                "displayMode": "table",
                "placement": "right",
                "calcs": ["lastNotNull", "max"],
            },
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
        "fieldConfig": {
            "defaults": {"unit": unit, "custom": {"lineWidth": 1, "fillOpacity": 10}},
            "overrides": [],
        },
    }


def bargauge(title: str, expr: str, legend: str, unit: str, steps: Any) -> Panel:
    return {
        "type": "bargauge",
        "title": title,
        "targets": [target(expr, legend)],
        "options": {
            "displayMode": "gradient",
            "orientation": "horizontal",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "showUnfilled": True,
        },
        "fieldConfig": {
            "defaults": {"unit": unit, "min": 0, "max": 100, "thresholds": steps},
            "overrides": [],
        },
    }


def table(title: str, columns: list[tuple[str, str, str]], hide: list[str],
          rename: dict[str, str], description: str = "") -> Panel:  # fmt: skip
    """``columns`` is a list of (expr, column title, unit)."""
    targets = []
    overrides = []
    rename = dict(rename)
    for i, (expr, column, unit) in enumerate(columns):
        ref = chr(ord("A") + i)
        targets.append(target(expr, ref=ref, table=True))
        rename[f"Value #{ref}"] = column
        overrides.append(
            {
                "matcher": {"id": "byName", "options": column},
                "properties": [{"id": "unit", "value": unit}],
            }
        )
    return {
        "type": "table",
        "title": title,
        "description": description,
        "targets": targets,
        "transformations": [
            {"id": "merge", "options": {}},
            {
                "id": "organize",
                "options": {
                    "excludeByName": dict.fromkeys(["Time", "job", "__name__", *hide], True),
                    "renameByName": rename,
                },
            },
        ],
        "options": {"showHeader": True, "cellHeight": "sm"},
        "fieldConfig": {"defaults": {}, "overrides": overrides},
    }


RED_ABOVE_0 = thresholds((None, "green"), (1, "red"))
USAGE = thresholds((None, "green"), (80, "orange"), (90, "red"))


def build() -> dict[str, Any]:
    lay = Layout()

    lay.row("Cluster and exporter")
    lay.add(
        stat(
            "Quorum",
            f"min(pve_cluster_quorate{{{SEL}}})",
            steps=thresholds((None, "red"), (1, "green")),
            mappings=[
                {
                    "type": "value",
                    "options": {"0": {"text": "NO QUORUM"}, "1": {"text": "Quorate"}},
                }
            ],
            description="Empty on standalone nodes.",
        ),
        4,
        4,
    )
    lay.add(stat("Cluster nodes online", f"max(pve_cluster_nodes_online{{{SEL}}})"), 4, 4)
    lay.add(
        stat("Guests running", f'sum(pve_vm_count{{{SEL}, status="running"}})'),
        4,
        4,
    )
    lay.add(
        stat(
            "Failed systemd units",
            f'sum(node_systemd_units{{{SEL}, state="failed"}})',
            steps=RED_ABOVE_0,
        ),
        4,
        4,
    )
    lay.add(
        stat(
            "Failing collectors",
            f"count(proxmox_exporter_collector_success{{{SEL}}} == 0) or vector(0)",
            steps=RED_ABOVE_0,
            description="Collectors whose last run failed; see the exporter's journal.",
        ),
        4,
        4,
    )
    lay.add(
        stat(
            "First certificate expiry",
            f"min(pve_certificate_expiry_timestamp_seconds{{{SEL}}}) - time()",
            unit="dtdurations",
            steps=thresholds((None, "red"), (14 * 86400, "orange"), (30 * 86400, "green")),
        ),
        4,
        4,
    )

    lay.row("Guests")
    guest_labels = {"instance": "Node", "vmid": "VMID", "name": "Name", "type": "Type"}
    lay.add(
        table(
            "Guests",
            [
                (f"pve_vm_status{{{SEL}}}", "Running", "bool_yes_no"),
                (f"pve_vm_cpu_usage_percent{{{SEL}}}", "CPU", "percent"),
                (f"pve_vm_cpus{{{SEL}}}", "vCPUs", "none"),
                (f"pve_vm_memory_used_bytes{{{SEL}}}", "Memory used", "bytes"),
                (f"pve_vm_memory_total_bytes{{{SEL}}}", "Memory", "bytes"),
                (f"pve_vm_disk_total_bytes{{{SEL}}}", "Disk", "bytes"),
                (f"pve_vm_uptime_seconds{{{SEL}}}", "Uptime", "dtdurations"),
            ],
            hide=[],
            rename=guest_labels,
        ),
        24,
        10,
    )
    lay.add(
        timeseries(
            "Guest CPU (top 10)",
            [target(f"topk(10, pve_vm_cpu_usage_percent{{{SEL}}})", "{{name}} ({{vmid}})")],
            "percent",
        ),
        12,
        8,
    )
    lay.add(
        timeseries(
            "Guest memory used (top 10)",
            [target(f"topk(10, pve_vm_memory_used_bytes{{{SEL}}})", "{{name}} ({{vmid}})")],
            "bytes",
        ),
        12,
        8,
    )
    lay.add(
        timeseries(
            "Guest disk I/O (top 10)",
            [
                target(
                    f"topk(10, rate(pve_vm_disk_read_bytes_total{{{SEL}}}[$__rate_interval])"
                    f" + rate(pve_vm_disk_write_bytes_total{{{SEL}}}[$__rate_interval]))",
                    "{{name}} ({{vmid}})",
                )
            ],
            "Bps",
        ),
        12,
        8,
    )
    lay.add(
        timeseries(
            "Guest network (top 10)",
            [
                target(
                    f"topk(10, rate(pve_vm_network_receive_bytes_total{{{SEL}}}[$__rate_interval])"
                    f" + rate(pve_vm_network_transmit_bytes_total{{{SEL}}}[$__rate_interval]))",
                    "{{name}} ({{vmid}})",
                )
            ],
            "Bps",
        ),
        12,
        8,
    )

    lay.row("Storage")
    lay.add(
        bargauge(
            "Proxmox storage used",
            f"100 * pve_storage_used_bytes{{{SEL}}} / pve_storage_total_bytes{{{SEL}}}",
            "{{instance}} {{storage}}",
            "percent",
            USAGE,
        ),
        12,
        8,
    )
    lay.add(
        bargauge(
            "ZFS pool allocated",
            f"100 * node_zfs_zpool_allocated_bytes{{{SEL}}} / node_zfs_zpool_size_bytes{{{SEL}}}",
            "{{instance}} {{pool}}",
            "percent",
            USAGE,
        ),
        12,
        8,
    )
    lay.add(
        table(
            "ZFS pools",
            [
                (f"node_zfs_zpool_health{{{SEL}}}", "Health", "none"),
                (
                    f"sum by (instance, pool) (node_zfs_zpool_errors_total{{{SEL}}})",
                    "Errors",
                    "none",
                ),
                (f"node_zfs_zpool_data_errors{{{SEL}}}", "Data errors", "none"),
                (f"node_zfs_zpool_fragmentation_percent{{{SEL}}}", "Fragmentation", "percent"),
                (
                    f"time() - node_zfs_zpool_last_scrub_timestamp_seconds{{{SEL}}}",
                    "Since last scrub",
                    "dtdurations",
                ),
            ],
            hide=[],
            rename={"instance": "Node", "pool": "Pool"},
            description="Health: 0 online, 1 degraded, 2 faulted, 3 offline, 4 unavail, "
            "5 removed, 6 suspended.",
        ),
        16,
        8,
    )
    lay.add(
        stat(
            "Degraded md arrays",
            f"count(node_md_degraded{{{SEL}}} > 0) or vector(0)",
            steps=RED_ABOVE_0,
        ),
        8,
        4,
    )
    lay.add(
        stat(
            "Replication jobs failing",
            f"count(pve_replication_failures{{{SEL}}} > 0) or vector(0)",
            steps=RED_ABOVE_0,
        ),
        8,
        4,
    )

    lay.row("Disks (SMART)")
    lay.add(
        table(
            "Disks",
            [
                (f"node_disk_smart_healthy{{{SEL}}}", "Healthy", "bool_yes_no"),
                (f"node_disk_smart_temperature_celsius{{{SEL}}}", "Temperature", "celsius"),
                (f"node_disk_smart_ssd_wearout_percent{{{SEL}}}", "Wear", "percent"),
                (f"node_disk_smart_reallocated_sectors{{{SEL}}}", "Reallocated", "none"),
                (f"node_disk_smart_pending_sectors{{{SEL}}}", "Pending", "none"),
                (f"node_disk_smart_power_on_hours_total{{{SEL}}}", "Power-on", "h"),
            ],
            hide=["serial"],
            rename={"instance": "Node", "device": "Device", "model": "Model"},
        ),
        24,
        9,
    )
    lay.add(
        timeseries(
            "Disk temperature",
            [target(f"node_disk_smart_temperature_celsius{{{SEL}}}", "{{instance}} {{device}}")],
            "celsius",
        ),
        12,
        8,
    )
    lay.add(
        timeseries(
            "Hottest sensor per chip",
            [
                target(
                    f"max by (instance, chip) (node_hwmon_temp_celsius{{{SEL}}})",
                    "{{instance}} {{chip}}",
                )
            ],
            "celsius",
        ),
        12,
        8,
    )

    lay.row("Power")
    lay.add(
        timeseries(
            "UPS",
            [
                target(f"node_ups_battery_charge_percent{{{SEL}}}", "{{ups}} charge"),
                target(f"node_ups_load_percent{{{SEL}}}", "{{ups}} load", ref="B"),
            ],
            "percent",
        ),
        12,
        8,
    )
    lay.add(
        timeseries(
            "Power draw",
            [
                target(f"node_ipmi_power_watts{{{SEL}}}", "{{instance}} {{name}}"),
                target(f"node_ups_power_watts{{{SEL}}}", "{{instance}} UPS {{ups}}", ref="B"),
                target(f"node_gpu_power_draw_watts{{{SEL}}}", "{{instance}} GPU {{gpu}}", ref="C"),
            ],
            "watt",
        ),
        12,
        8,
    )

    return {
        "title": "Proxmox Health",
        "uid": "proxmox-node-exporter-health",
        "description": "Guests, storage, ZFS, SMART and hardware health "
        "from proxmox-node-exporter.",
        "tags": ["proxmox", "proxmox-node-exporter"],
        "editable": True,
        "graphTooltip": 1,
        "refresh": "1m",
        "schemaVersion": 39,
        "time": {"from": "now-6h", "to": "now"},
        "timezone": "browser",
        "id": None,
        "__requires": [
            {"type": "datasource", "id": "prometheus", "name": "Prometheus", "version": "1.0.0"}
        ],
        "templating": {
            "list": [
                {
                    "name": "datasource",
                    "label": "Data source",
                    "type": "datasource",
                    "query": "prometheus",
                    "current": {},
                    "hide": 0,
                    "refresh": 1,
                },
                {
                    "name": "node",
                    "label": "Node",
                    "type": "query",
                    "datasource": dict(DS),
                    "query": {"query": "label_values(node_info, instance)", "refId": "node"},
                    "definition": "label_values(node_info, instance)",
                    "includeAll": True,
                    "multi": True,
                    "current": {},
                    "refresh": 1,
                    "sort": 1,
                },
            ]
        },
        "annotations": {"list": []},
        "panels": lay.panels,
    }


def render() -> str:
    return json.dumps(build(), indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the file is out of date")
    args = parser.parse_args()
    content = render()
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text() != content:
            print(f"{OUTPUT.relative_to(ROOT)} is out of date; run tools/gen_health_dashboard.py")
            return 1
        return 0
    OUTPUT.write_text(content)
    print(OUTPUT.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
