"""Dashboards, alert rules and generated docs must match the metric catalog."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import proxmox_node_exporter.cli  # noqa: F401  (registers every collector's metrics)
from proxmox_node_exporter.metrics import CATALOG

ROOT = Path(__file__).resolve().parent.parent
DASHBOARDS = sorted((ROOT / "grafana").glob("*.json"))
RULES = ROOT / "prometheus" / "alerts.yml"
OUR_PREFIXES = ("node_", "pve_", "proxmox_exporter_", "process_")
_QUOTED = re.compile(r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'")
_IDENT = re.compile(r"(?<![\w$.])[a-zA-Z_:][a-zA-Z0-9_:]*")


def metric_names(expr: str) -> set[str]:
    """Identifiers in a PromQL expression that look like our metrics."""
    expr = _QUOTED.sub('""', expr)
    return {name for name in _IDENT.findall(expr) if name.startswith(OUR_PREFIXES)}


def dashboard_exprs(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("expr", "query", "definition") and isinstance(value, str):
                yield value
            else:
                yield from dashboard_exprs(value)
    elif isinstance(node, list):
        for item in node:
            yield from dashboard_exprs(item)


def rule_exprs() -> Iterator[tuple[str, str]]:
    # promtool validates the YAML in CI; a tiny parser is enough to pull the
    # expressions out here without a YAML dependency.
    lines = RULES.read_text().splitlines()
    alert = ""
    i = 0
    while i < len(lines):
        line = lines[i]
        match = re.match(r"\s*- alert: (\S+)", line)
        if match:
            alert = match.group(1)
        match = re.match(r"(\s*)expr: (.*)$", line)
        if match:
            indent, value = len(match.group(1)), match.group(2).strip()
            if value in ("|", ">"):
                block = []
                i += 1
                while i < len(lines) and (
                    not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > indent
                ):
                    block.append(lines[i].strip())
                    i += 1
                yield alert, " ".join(block)
                continue
            yield alert, value
        i += 1


def test_parser_sees_metrics_in_sample_expression() -> None:
    expr = 'sum by (instance) (rate(node_cpu_seconds_total{mode="idle", x="node_fake"}[5m]))'
    assert metric_names(expr) == {"node_cpu_seconds_total"}


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
def test_dashboard_only_uses_exported_metrics(path: Path) -> None:
    dashboard = json.loads(path.read_text())
    unknown = {name for expr in dashboard_exprs(dashboard) for name in metric_names(expr)} - set(
        CATALOG
    )
    assert not unknown, f"{path.name} queries metrics the exporter does not emit: {unknown}"


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
def test_dashboard_is_portable(path: Path) -> None:
    text = path.read_text()
    dashboard = json.loads(text)
    assert "__inputs" not in dashboard, "use the ${datasource} variable instead of __inputs"
    assert "DS_PROMETHEUS" not in text
    variables = {v["name"]: v for v in dashboard["templating"]["list"]}
    assert variables["datasource"]["type"] == "datasource"
    for expr in dashboard_exprs(dashboard):
        # The exporter never sets a `node` label; dashboards select by instance.
        assert not re.search(r"[{,]\s*node\s*=~?\s*[\"']", expr), expr


def test_dashboard_uids_are_unique() -> None:
    uids = [json.loads(p.read_text())["uid"] for p in DASHBOARDS]
    assert len(uids) == len(set(uids))


def test_alert_rules_only_use_exported_metrics() -> None:
    rules = list(rule_exprs())
    assert len(rules) > 30
    for alert, expr in rules:
        unknown = metric_names(expr) - set(CATALOG)
        assert not unknown, f"alert {alert} uses unknown metrics {unknown}"


@pytest.mark.parametrize("script", ["tools/gen_metrics_doc.py", "tools/gen_health_dashboard.py"])
def test_generated_files_are_up_to_date(script: str) -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / script), "--check"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
