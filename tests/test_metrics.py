from __future__ import annotations

import math

import pytest
from prometheus_client.parser import text_string_to_metric_families

from proxmox_node_exporter.metrics import CATALOG, Batch, MetricGroup, format_value, render

T = MetricGroup("test")
G = T.gauge("test_metric_gauge", "A gauge.\nWith a newline and a \\ backslash.", "a", "b")
C = T.counter("test_metric_things_total", "A counter.")


def test_render_is_valid_exposition() -> None:
    batch = Batch()
    batch.add(G, 1.5, a='quote " here', b="back\\slash\nnewline")
    batch.add(C, 42)
    text = render(batch.families()).decode()
    families = {f.name: f for f in text_string_to_metric_families(text)}
    gauge = families["test_metric_gauge"]
    assert gauge.type == "gauge"
    assert gauge.samples[0].labels == {"a": 'quote " here', "b": "back\\slash\nnewline"}
    assert gauge.samples[0].value == 1.5
    counter = families["test_metric_things"]
    assert counter.type == "counter"
    assert counter.samples[0].value == 42
    assert "# TYPE test_metric_things_total counter" in text


def test_same_labels_keep_last_value() -> None:
    batch = Batch()
    batch.add(G, 1, a="x", b="y")
    batch.add(G, 2, a="x", b="y")
    assert len(batch) == 1
    assert b'test_metric_gauge{a="x",b="y"} 2\n' in render(batch.families())


def test_none_values_are_skipped_and_labels_validated() -> None:
    batch = Batch()
    batch.add(G, None, a="x", b="y")
    assert len(batch) == 0
    with pytest.raises(ValueError, match="expected labels"):
        batch.add(G, 1, a="x")
    with pytest.raises(ValueError, match="expected labels"):
        batch.add(G, 1, a="x", c="y")


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (1.0, "1"),
        (-3.0, "-3"),
        (0.25, "0.25"),
        (1e20, "1e+20"),
        (math.inf, "+Inf"),
        (-math.inf, "-Inf"),
        (math.nan, "NaN"),
    ],
)
def test_format_value(value: float, text: str) -> None:
    assert format_value(value) == text


def test_declarations_are_validated() -> None:
    with pytest.raises(ValueError, match="must end in _total"):
        T.counter("test_metric_bad", "x")
    with pytest.raises(ValueError, match="invalid metric name"):
        T.gauge("0bad", "x")
    with pytest.raises(ValueError, match="invalid label"):
        T.gauge("test_metric_bad_label", "x", "__reserved")
    with pytest.raises(ValueError, match="declared twice"):
        T.gauge("test_metric_gauge", "different help", "a", "b")
    # Re-declaring identically is allowed (module reloads).
    assert T.gauge("test_metric_gauge", G.help, "a", "b") == G


def test_empty_families_are_not_rendered() -> None:
    assert render([]) == b""


def test_catalog_contains_every_collector_group() -> None:
    import proxmox_node_exporter.cli  # noqa: F401  (imports every collector)
    from proxmox_node_exporter.collectors import ALL_COLLECTORS

    groups = {spec.group for spec in CATALOG.values()}
    for cls in ALL_COLLECTORS:
        assert cls.name in groups, f"collector {cls.name} declares no metrics"
