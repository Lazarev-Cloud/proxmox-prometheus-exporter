"""Tests for the hardened command runner (real processes, default SAFE_PATH)."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path
from typing import Callable

import pytest

from proxmox_node_exporter.runner import SAFE_PATH, CommandError, CommandResult, Runner


def _process_alive(pid: int) -> bool:
    """True while ``pid`` exists and is not a zombie (containers may not reap)."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            text = fh.read()
    except OSError:
        return False
    return text[text.rfind(")") + 2 :].split()[0] not in ("Z", "X")


def _wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _make_executable(path: Path, content: str) -> Path:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def runner() -> Runner:
    return Runner()


def test_default_path_is_the_fixed_safe_path(runner: Runner) -> None:
    assert Runner()._path == SAFE_PATH
    assert all(p.startswith("/") for p in SAFE_PATH.split(":"))
    sh = runner.which("sh")
    assert sh is not None
    assert os.path.dirname(sh) in SAFE_PATH.split(":")


def test_echo_stdout_is_decoded(runner: Runner) -> None:
    result = runner.run(["echo", "hello", "wörld"])
    assert result == CommandResult(0, "hello wörld\n", "")


def test_invalid_utf8_is_replaced_not_fatal(runner: Runner) -> None:
    result = runner.run(["sh", "-c", r"printf 'a\377b'; printf 'e\376' >&2"])
    assert result.stdout == "a�b"
    assert result.stderr == "e�"


def test_non_zero_exit_raises_with_last_stderr_line(runner: Runner) -> None:
    with pytest.raises(CommandError, match=r"^sh: exit status 3: second line$"):
        runner.run(["sh", "-c", "echo first >&2; echo second line >&2; exit 3"])


def test_non_zero_exit_without_stderr(runner: Runner) -> None:
    with pytest.raises(CommandError, match="exit status 3: no output"):
        runner.run(["sh", "-c", "exit 3"])


def test_ok_codes_none_accepts_any_status(runner: Runner) -> None:
    result = runner.run(["sh", "-c", "echo out; echo err >&2; exit 3"], ok_codes=None)
    assert result == CommandResult(3, "out\n", "err\n")


def test_custom_ok_codes(runner: Runner) -> None:
    assert runner.run(["sh", "-c", "exit 4"], ok_codes=(0, 4)).returncode == 4
    with pytest.raises(CommandError, match="exit status 0"):
        runner.run(["true"], ok_codes=(1,))


def test_command_not_found(runner: Runner) -> None:
    with pytest.raises(CommandError, match=r"^no-such-command-pne: command not found$"):
        runner.run(["no-such-command-pne", "--help"])


def test_empty_argv_is_rejected(runner: Runner) -> None:
    with pytest.raises(ValueError, match="empty command"):
        runner.run([])


def test_inherited_path_is_ignored(
    runner: Runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_executable(tmp_path / "pne-planted", "#!/bin/sh\necho planted\n")
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    assert runner.which("pne-planted") is None
    with pytest.raises(CommandError, match="command not found"):
        runner.run(["pne-planted"])


def test_custom_path_is_used_for_lookup_and_child(tmp_path: Path) -> None:
    _make_executable(tmp_path / "pne-tool", '#!/bin/sh\necho "$PATH"\n')
    custom = Runner(path=f"{tmp_path}:{SAFE_PATH}")
    assert custom.which("pne-tool") == str(tmp_path / "pne-tool")
    assert custom.run(["pne-tool"]).stdout == f"{tmp_path}:{SAFE_PATH}\n"


def test_exec_failure_becomes_command_error(tmp_path: Path) -> None:
    _make_executable(tmp_path / "pne-garbage", "\x7fnot an executable\x00\x01")
    with pytest.raises(CommandError, match=r"^pne-garbage: "):
        Runner(path=str(tmp_path)).run(["pne-garbage"])


def test_minimal_environment(runner: Runner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PNE_TEST_SECRET", "hunter2")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/pne-nonexistent")
    monkeypatch.setenv("HOME", "/pne-home")
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    lines = runner.run(["env"]).stdout.splitlines()
    env = dict(line.split("=", 1) for line in lines)
    assert env == {"PATH": SAFE_PATH, "LC_ALL": "C", "LANG": "C", "HOME": "/pne-home"}


def test_environment_without_home(runner: Runner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOME", raising=False)
    env = dict(line.split("=", 1) for line in runner.run(["env"]).stdout.splitlines())
    assert set(env) == {"PATH", "LC_ALL", "LANG"}


def test_stdin_is_dev_null(runner: Runner) -> None:
    assert runner.run(["readlink", "/proc/self/fd/0"]).stdout.strip() == "/dev/null"
    # A command reading stdin gets EOF immediately instead of hanging.
    assert runner.run(["cat"], timeout=5).stdout == ""


def test_inheritable_descriptors_are_not_leaked(runner: Runner) -> None:
    read_fd, write_fd = os.pipe()
    high = 211
    try:
        os.dup2(read_fd, high, inheritable=True)
        script = f"test -e /proc/self/fd/{high} && echo leaked || echo closed"
        assert runner.run(["sh", "-c", script]).stdout == "closed\n"
    finally:
        for fd in (read_fd, write_fd, high):
            os.close(fd)


def test_child_runs_in_its_own_session(runner: Runner) -> None:
    out = runner.run(["sh", "-c", "echo $$; cut -d' ' -f5,6 /proc/$$/stat"]).stdout.split()
    pid, pgrp, session = (int(v) for v in out)
    assert pgrp == pid == session
    assert pgrp != os.getpgrp()


def test_timeout_kills_the_whole_process_group(runner: Runner, tmp_path: Path) -> None:
    pidfile = tmp_path / "bg.pid"
    script = f"sleep 30 & echo $! > {pidfile}; sleep 30"
    start = time.monotonic()
    with pytest.raises(CommandError, match=r"^sh: timed out after 0\.5s$"):
        runner.run(["sh", "-c", script], timeout=0.5)
    elapsed = time.monotonic() - start
    # If the background child survived, it would keep stdout open and the
    # runner would wait another 5 s for EOF.
    assert elapsed < 3.0
    assert _wait_until(pidfile.exists, 1.0)
    background = int(pidfile.read_text())
    assert _wait_until(lambda: not _process_alive(background)), "background child survived"


def test_which_is_cached(tmp_path: Path) -> None:
    tool = _make_executable(tmp_path / "pne-cached", "#!/bin/sh\n")
    cached = Runner(path=str(tmp_path))
    assert cached.which("pne-cached") == str(tool)
    tool.unlink()
    assert cached.which("pne-cached") == str(tool)
    assert Runner(path=str(tmp_path)).which("pne-cached") is None


@pytest.mark.parametrize("argv0", ["/bin/echo", "./echo", "bin/echo"])
def test_paths_are_refused_only_bare_names_are_resolved(runner: Runner, argv0: str) -> None:
    with pytest.raises(ValueError, match="only bare command names"):
        runner.run([argv0, "hi"])
