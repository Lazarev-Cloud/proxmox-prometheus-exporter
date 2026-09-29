"""Shared test helpers.

* :class:`FakeRunner` replaces command execution with canned output.
* :func:`make_ctx` builds a collector context whose procfs/sysfs point at
  temporary directories populated by :func:`write_tree`.
* :func:`collect` runs a collector and returns its samples as
  ``{metric_name: {frozenset(labels.items()): value}}``; :func:`value` looks
  one sample up.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Callable, Union

import pytest

from proxmox_node_exporter.collectors.base import Collector, Context
from proxmox_node_exporter.config import Settings
from proxmox_node_exporter.metrics import Batch, render
from proxmox_node_exporter.runner import CommandError, CommandResult, Runner

FIXTURES = Path(__file__).parent / "fixtures"

Response = Union[str, CommandResult, Exception, Callable[[Sequence[str]], CommandResult]]
Samples = dict[str, dict[frozenset[tuple[str, str]], float]]


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


class FakeRunner(Runner):
    """Returns canned results keyed by the argv prefix.

    ``responses`` maps a tuple prefix of argv (e.g. ``("zpool", "list")``) to
    stdout text, a :class:`CommandResult`, an exception to raise, or a callable
    receiving argv.  The longest matching prefix wins.  Commands without a
    response are reported as not installed.
    """

    def __init__(self, responses: Mapping[tuple[str, ...], Response] | None = None) -> None:
        super().__init__()
        self.responses: dict[tuple[str, ...], Response] = dict(responses or {})
        self.calls: list[tuple[str, ...]] = []

    def which(self, name: str) -> str | None:
        return f"/usr/bin/{name}" if any(k[0] == name for k in self.responses) else None

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float = 10.0,
        ok_codes: Any = (0,),
    ) -> CommandResult:
        argv = tuple(argv)
        self.calls.append(argv)
        matches = [k for k in self.responses if argv[: len(k)] == k]
        if not matches:
            raise CommandError(f"{argv[0]}: command not found")
        response = self.responses[max(matches, key=len)]
        if isinstance(response, Exception):
            raise response
        if callable(response):
            result = response(argv)
        elif isinstance(response, CommandResult):
            result = response
        else:
            result = CommandResult(0, response, "")
        if ok_codes is not None and result.returncode not in ok_codes:
            raise CommandError(f"{argv[0]}: exit status {result.returncode}")
        return result


def write_tree(root: Path, files: Mapping[str, str]) -> Path:
    """Create ``files`` (relative path -> content) below ``root``."""
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return root


@pytest.fixture
def make_ctx(tmp_path: Path) -> Callable[..., Context]:
    def factory(runner: Runner | None = None, *, is_root: bool = True, **settings: Any) -> Context:
        settings.setdefault("procfs", str(tmp_path / "proc"))
        settings.setdefault("sysfs", str(tmp_path / "sys"))
        os.makedirs(settings["procfs"], exist_ok=True)
        os.makedirs(settings["sysfs"], exist_ok=True)
        return Context(Settings(**settings), runner or FakeRunner(), is_root)

    return factory


def collect(collector: Collector) -> Samples:
    batch = Batch()
    collector.collect(batch)
    render(batch.families())  # must always serialise
    out: Samples = {}
    for family in batch.families():
        labels = family.spec.labels
        out[family.spec.name] = {
            frozenset(zip(labels, key)): value for key, value in family.samples.items()
        }
    return out


def value(samples: Samples, metric: str, /, **labels: str) -> float | None:
    # Positional-only, so metrics with a `name` or `metric` label work too.
    return samples.get(metric, {}).get(frozenset(labels.items()))
