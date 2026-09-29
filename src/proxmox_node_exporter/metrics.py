"""Metric definitions and the Prometheus text exposition format (version 0.0.4).

Every metric the exporter can emit is declared up front with :class:`MetricGroup`
and registered in :data:`CATALOG`.  The catalog is the single source of truth
for the generated metrics reference and for the tests that check the Grafana
dashboards and alert rules only use metrics that actually exist.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Union

GAUGE = "gauge"
COUNTER = "counter"

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

_METRIC_NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_LABEL_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

LabelValues = tuple[str, ...]
Number = Union[int, float]


@dataclass(frozen=True)
class MetricSpec:
    name: str
    type: str
    help: str
    labels: tuple[str, ...]
    group: str


CATALOG: dict[str, MetricSpec] = {}


class MetricGroup:
    """Declares the metrics owned by one collector."""

    def __init__(self, group: str) -> None:
        self.group = group

    def gauge(self, name: str, help: str, *labels: str) -> MetricSpec:
        return self._register(MetricSpec(name, GAUGE, help, tuple(labels), self.group))

    def counter(self, name: str, help: str, *labels: str) -> MetricSpec:
        if not name.endswith("_total"):
            raise ValueError(f"counter {name} must end in _total")
        return self._register(MetricSpec(name, COUNTER, help, tuple(labels), self.group))

    @staticmethod
    def _register(spec: MetricSpec) -> MetricSpec:
        if not _METRIC_NAME_RE.match(spec.name):
            raise ValueError(f"invalid metric name {spec.name!r}")
        for label in spec.labels:
            if not _LABEL_NAME_RE.match(label) or label.startswith("__"):
                raise ValueError(f"invalid label name {label!r} on {spec.name}")
        if len(set(spec.labels)) != len(spec.labels):
            raise ValueError(f"duplicate label on {spec.name}")
        existing = CATALOG.get(spec.name)
        if existing is not None and existing != spec:
            raise ValueError(f"metric {spec.name} declared twice with different definitions")
        CATALOG[spec.name] = spec
        return spec


@dataclass
class Family:
    spec: MetricSpec
    samples: dict[LabelValues, float] = field(default_factory=dict)


class Batch:
    """Accumulates samples for one collection run.

    Adding the same label set twice keeps the last value, so a collector can
    never produce duplicate series (which Prometheus would reject).
    """

    def __init__(self) -> None:
        self._families: dict[str, Family] = {}

    def add(self, spec: MetricSpec, value: Number | None, **labels: object) -> None:
        if value is None:
            return
        if len(labels) != len(spec.labels) or any(name not in labels for name in spec.labels):
            raise ValueError(
                f"{spec.name}: expected labels {sorted(spec.labels)}, got {sorted(labels)}"
            )
        family = self._families.get(spec.name)
        if family is None:
            family = self._families[spec.name] = Family(spec)
        key = tuple(str(labels[name]) for name in spec.labels)
        family.samples[key] = float(value)

    def families(self) -> list[Family]:
        return list(self._families.values())

    def __len__(self) -> int:
        return sum(len(f.samples) for f in self._families.values())


def format_value(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def _escape_help(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def _escape_label(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def render(families: Iterable[Family]) -> bytes:
    lines: list[str] = []
    for family in sorted(families, key=lambda f: f.spec.name):
        if not family.samples:
            continue
        spec = family.spec
        lines.append(f"# HELP {spec.name} {_escape_help(spec.help)}")
        lines.append(f"# TYPE {spec.name} {spec.type}")
        for values, value in family.samples.items():
            if spec.labels:
                pairs = ",".join(
                    f'{name}="{_escape_label(v)}"' for name, v in zip(spec.labels, values)
                )
                lines.append(f"{spec.name}{{{pairs}}} {format_value(value)}")
            else:
                lines.append(f"{spec.name} {format_value(value)}")
    lines.append("")
    return "\n".join(lines).encode("utf-8")
