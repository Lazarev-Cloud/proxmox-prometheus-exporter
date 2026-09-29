"""Hardened execution of external commands.

Everything the exporter runs goes through :class:`Runner`:

* no shell is ever involved, argv is passed as a list;
* binaries are resolved from a fixed, root-owned ``PATH`` rather than the
  inherited environment;
* the child gets a minimal environment, no stdin and its own process group,
  so a timeout kills the whole tree;
* every call has a timeout.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import signal
import subprocess
from collections.abc import Collection, Sequence
from dataclasses import dataclass

log = logging.getLogger(__name__)

SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
DEFAULT_TIMEOUT = 10.0


class CommandError(Exception):
    """A command could not be run or failed."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class Runner:
    def __init__(self, path: str = SAFE_PATH) -> None:
        self._path = path
        self._which_cache: dict[str, str | None] = {}

    def which(self, name: str) -> str | None:
        if name not in self._which_cache:
            self._which_cache[name] = shutil.which(name, path=self._path)
        return self._which_cache[name]

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float = DEFAULT_TIMEOUT,
        ok_codes: Collection[int] | None = (0,),
    ) -> CommandResult:
        """Run ``argv`` and return its output.

        ``ok_codes=None`` accepts any exit status (for tools such as smartctl
        that report findings through the exit code).
        """
        if not argv:
            raise ValueError("empty command")
        if "/" in argv[0]:
            raise ValueError(f"{argv[0]!r}: only bare command names are resolved (via SAFE_PATH)")
        executable = self.which(argv[0])
        if executable is None:
            raise CommandError(f"{argv[0]}: command not found")
        env = {"PATH": self._path, "LC_ALL": "C", "LANG": "C"}
        if "HOME" in os.environ:
            env["HOME"] = os.environ["HOME"]
        try:
            proc = subprocess.Popen(
                [executable, *argv[1:]],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            raise CommandError(f"{argv[0]}: {exc}") from exc
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            raise CommandError(f"{argv[0]}: timed out after {timeout:g}s") from None
        result = CommandResult(
            proc.returncode,
            out.decode("utf-8", errors="replace"),
            err.decode("utf-8", errors="replace"),
        )
        if ok_codes is not None and result.returncode not in ok_codes:
            detail = result.stderr.strip().splitlines()
            reason = detail[-1][:200] if detail else "no output"
            raise CommandError(f"{argv[0]}: exit status {result.returncode}: {reason}")
        return result


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:  # stuck in uninterruptible sleep
        log.warning("pid %d did not exit after SIGKILL", proc.pid)
