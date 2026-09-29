"""Collector base class and small helpers for reading procfs/sysfs."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, TypeVar

from ..config import Settings
from ..metrics import Batch
from ..runner import CommandResult, Runner

T = TypeVar("T")


@dataclass
class Context:
    settings: Settings = field(default_factory=Settings)
    runner: Runner = field(default_factory=Runner)
    is_root: bool = field(default_factory=lambda: os.geteuid() == 0)


class Collector:
    """A source of metrics, run periodically in its own thread.

    Subclasses set :attr:`name`, :attr:`description`, optionally
    :attr:`default_interval`, and implement :meth:`detect` and :meth:`collect`.
    :meth:`collect` should raise only when nothing useful could be gathered;
    partial failures (one disk, one sensor) are skipped silently.
    """

    name: ClassVar[str]
    description: ClassVar[str]
    default_interval: ClassVar[float | None] = None

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.settings = ctx.settings
        self._cache: dict[str, tuple[float, Any]] = {}

    def detect(self) -> bool:
        """Cheap check whether this collector can work on this host."""
        return True

    def collect(self, out: Batch) -> None:
        raise NotImplementedError

    # -- helpers -----------------------------------------------------------

    def proc_path(self, *parts: str) -> str:
        return os.path.join(self.settings.procfs, *parts)

    def sys_path(self, *parts: str) -> str:
        return os.path.join(self.settings.sysfs, *parts)

    def has_command(self, name: str) -> bool:
        return self.ctx.runner.which(name) is not None

    def run(self, *argv: str, timeout: float = 10.0, ok_codes: Any = (0,)) -> CommandResult:
        return self.ctx.runner.run(argv, timeout=timeout, ok_codes=ok_codes)

    def cached(self, key: str, ttl: float, compute: Callable[[], T]) -> T:
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit is not None and hit[0] > now:
            value: T = hit[1]
            return value
        value = compute()
        self._cache[key] = (now + ttl, value)
        return value


def read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except (OSError, ValueError):
        return None


def read_int(path: str) -> int | None:
    text = read_text(path)
    if text is None:
        return None
    try:
        return int(text.split()[0]) if text else None
    except ValueError:
        return None


def read_float(path: str) -> float | None:
    text = read_text(path)
    if not text:
        return None
    try:
        return float(text.split()[0])
    except ValueError:
        return None


def list_dir(path: str) -> list[str]:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def to_float(text: str | None) -> float | None:
    """Parse a number, returning None for blanks and markers like ``N/A``."""
    if text is None:
        return None
    text = text.strip()
    try:
        return float(text)
    except ValueError:
        return None
