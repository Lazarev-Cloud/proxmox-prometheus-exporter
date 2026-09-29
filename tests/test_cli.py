"""Tests for argument handling and the command line entry point."""

from __future__ import annotations

import http.client
import io
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from prometheus_client.parser import text_string_to_metric_families

from proxmox_node_exporter import cli
from proxmox_node_exporter.cli import build_parser, main, select_collectors, settings_from_args
from proxmox_node_exporter.collectors import ALL_COLLECTORS
from proxmox_node_exporter.collectors.base import Context
from proxmox_node_exporter.collectors.system import SystemCollector
from proxmox_node_exporter.config import (
    DEFAULT_INTERVAL,
    DEFAULT_LISTEN_ADDRESS,
    DEFAULT_PVE_API_URL,
    Settings,
)
from proxmox_node_exporter.runner import Runner
from proxmox_node_exporter.webconfig import hash_password, verify_password

FAKE_PROC = {
    "stat": (
        "cpu  100 5 50 1000 10 0 5 0 0 0\n"
        "cpu0 100 5 50 1000 10 0 5 0 0 0\n"
        "intr 12345 1 2\n"
        "ctxt 6789\n"
        "btime 1700000000\n"
        "processes 4242\n"
        "procs_running 2\n"
        "procs_blocked 1\n"
    ),
    "loadavg": "0.50 0.40 0.30 2/345 6789\n",
    "meminfo": (
        "MemTotal:       16384000 kB\n"
        "MemFree:         8000000 kB\n"
        "MemAvailable:   12288000 kB\n"
        "SwapTotal:             0 kB\n"
        "SwapFree:              0 kB\n"
    ),
    "uptime": "12345.67 54321.00\n",
}
ALL_NAMES = [cls.name for cls in ALL_COLLECTORS]
Samples = dict[str, dict[frozenset[tuple[str, str]], float]]


def write_tree(root: Path, files: Mapping[str, str]) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    root.mkdir(parents=True, exist_ok=True)
    return root


def parse(argv: Sequence[str], env: dict[str, str] | None = None) -> tuple[Settings, str]:
    return settings_from_args(build_parser().parse_args(list(argv)), env or {})


def exposition(text: str) -> Samples:
    out: Samples = {}
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            out.setdefault(sample.name, {})[frozenset(sample.labels.items())] = sample.value
    return out


def lbl(**labels: str) -> frozenset[tuple[str, str]]:
    return frozenset(labels.items())


@pytest.fixture(autouse=True)
def basic_config_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Keep main() from reconfiguring the root logger; record what it asked for."""
    for name in ("EXPORTER_PORT", "COLLECTION_INTERVAL", "DEBUG_MODE", "JOURNAL_STREAM"):
        monkeypatch.delenv(name, raising=False)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: calls.append(kwargs))
    return calls


@pytest.fixture
def fake_host(tmp_path: Path) -> tuple[str, str]:
    proc = write_tree(tmp_path / "proc", FAKE_PROC)
    sysfs = write_tree(tmp_path / "sys", {})
    return str(proc), str(sysfs)


# -- settings_from_args --------------------------------------------------------------


def test_defaults() -> None:
    settings, listen = parse([])
    assert listen == DEFAULT_LISTEN_ADDRESS == ":9101"
    assert settings == Settings()
    assert settings.interval == DEFAULT_INTERVAL
    assert settings.pve_api_url == DEFAULT_PVE_API_URL


def test_flags_are_mapped_to_settings() -> None:
    settings, listen = parse(
        [
            "--web.listen-address", "[::1]:9200",
            "--collectors", "system, zfs,,",
            "--collectors.disable", "smart",
            "--interval", "30",
            "--collector.interval", "zfs=120",
            "--collector.interval", "smart=0.5",
            "--path.procfs", "/host/proc",
            "--path.sysfs", "/host/sys",
            "--filesystem.fs-types-exclude", "^tmpfs$",
            "--filesystem.mount-points-exclude", "^/boot",
            "--diskstats.device-exclude", "^loop",
            "--network.device-exclude", "^veth",
            "--systemd.unit-include", "^pve.*",
            "--systemd.unit-exclude", "^foo$",
            "--zfs.no-datasets",
            "--ups.target", "ups1@localhost",
            "--ups.target", "ups2@nas",
            "--pve.node", "pve-01.lab",
            "--pve.api-url", "https://pve.example:8006/",
            "--pve.api-token-file", "/etc/pne/token",
            "--pve.api-ca-file", "/etc/pne/ca.pem",
            "--pve.api-insecure-skip-verify",
        ]
    )  # fmt: skip
    assert listen == "[::1]:9200"
    assert settings == Settings(
        procfs="/host/proc",
        sysfs="/host/sys",
        interval=30.0,
        intervals={"zfs": 120.0, "smart": 0.5},
        collectors=["system", "zfs"],
        disabled_collectors=["smart"],
        filesystem_fs_types_exclude="^tmpfs$",
        filesystem_mount_points_exclude="^/boot",
        diskstats_device_exclude="^loop",
        network_device_exclude="^veth",
        systemd_unit_include="^pve.*",
        systemd_unit_exclude="^foo$",
        zfs_datasets=False,
        pve_node="pve-01.lab",
        pve_api_url="https://pve.example:8006",
        pve_api_token_file="/etc/pne/token",
        pve_api_ca_file="/etc/pne/ca.pem",
        pve_api_insecure=True,
        ups_targets=["ups1@localhost", "ups2@nas"],
    )


REGEX_FLAGS = [
    "--filesystem.fs-types-exclude",
    "--filesystem.mount-points-exclude",
    "--diskstats.device-exclude",
    "--network.device-exclude",
    "--systemd.unit-include",
    "--systemd.unit-exclude",
]


@pytest.mark.parametrize(
    ("argv", "env", "error"),
    [
        (["--collectors", "system,nope"], {}, "unknown collector 'nope'"),
        (["--web.max-connections", "0"], {}, "--web.max-connections must be at least 1"),
        (["--web.request-timeout", "0"], {}, "--web.request-timeout: must be positive"),
        (["--web.request-timeout", "nan"], {}, "--web.request-timeout: must be positive"),
        (["--ups.target=-l"], {}, "--ups.target: invalid UPS name '-l'"),
        (["--ups.target", "ups;reboot"], {}, "--ups.target: invalid UPS name"),
        (["--collectors", "System"], {}, "unknown collector 'System'"),
        (["--collectors.disable", "nope"], {}, "unknown collector 'nope'"),
        (["--collector.interval", "system"], {}, "--collector.interval expects NAME=SECONDS"),
        (["--collector.interval", "nope=5"], {}, "--collector.interval expects NAME=SECONDS"),
        (["--collector.interval", "system=abc"], {}, "--collector.interval system: not a number"),
        (["--collector.interval", "system=0"], {}, "--collector.interval system: must be positive"),
        (
            ["--collector.interval", "system=-1"],
            {},
            "--collector.interval system: must be positive",
        ),
        (
            ["--collector.interval", "system=inf"],
            {},
            "--collector.interval system: must be positive",
        ),
        (
            ["--collector.interval", "system=nan"],
            {},
            "--collector.interval system: must be positive",
        ),
        (["--interval=0"], {}, "--interval: must be positive"),
        (["--interval=-5"], {}, "--interval: must be positive"),
        (["--interval=inf"], {}, "--interval: must be positive"),
        (["--interval=nan"], {}, "--interval: must be positive"),
        ([], {"COLLECTION_INTERVAL": "abc"}, "COLLECTION_INTERVAL: not a number"),
        ([], {"COLLECTION_INTERVAL": "-1"}, "COLLECTION_INTERVAL: must be positive"),
        ([], {"COLLECTION_INTERVAL": "0"}, "COLLECTION_INTERVAL: must be positive"),
        ([], {"COLLECTION_INTERVAL": "inf"}, "COLLECTION_INTERVAL: must be positive"),
        *[([flag, "(unclosed"], {}, f"{flag}: invalid regular expression") for flag in REGEX_FLAGS],
        (["--web.listen-address", "::1:9101"], {}, "IPv6 addresses must be bracketed"),
        (["--web.listen-address", "localhost"], {}, "listen address must be host:port"),
        (["--web.listen-address", ":0"], {}, "port out of range"),
        (["--web.listen-address", ":70000"], {}, "port out of range"),
        (["--web.listen-address", "127.0.0.1:http"], {}, "invalid port"),
        ([], {"EXPORTER_PORT": "abc"}, "invalid port"),
        ([], {"EXPORTER_PORT": "70000"}, "port out of range"),
        ([], {"EXPORTER_PORT": "127.0.0.1:9101"}, "IPv6 addresses must be bracketed"),
        (["--pve.node=-bad"], {}, "--pve.node: invalid node name"),
        (["--pve.node", ".hidden"], {}, "--pve.node: invalid node name"),
        (["--pve.node", "../../etc"], {}, "--pve.node: invalid node name"),
        (["--pve.node", "node/qemu"], {}, "--pve.node: invalid node name"),
        (["--pve.node", "node name"], {}, "--pve.node: invalid node name"),
        (["--pve.node", "node_1"], {}, "--pve.node: invalid node name"),
        (["--pve.node", "nöde"], {}, "--pve.node: invalid node name"),
        (["--pve.node", "node\n"], {}, "--pve.node: invalid node name"),
        (["--pve.node", "node\nx"], {}, "--pve.node: invalid node name"),
        (["--pve.api-url", "http://127.0.0.1:8006"], {}, "--pve.api-url must use https://"),
        (["--pve.api-url", "127.0.0.1:8006"], {}, "--pve.api-url must use https://"),
        (["--pve.api-url", "file:///etc/passwd"], {}, "--pve.api-url must use https://"),
    ],
)
def test_invalid_settings(argv: list[str], env: dict[str, str], error: str) -> None:
    with pytest.raises(ValueError, match=re.escape(error)):
        parse(argv, env)


@pytest.mark.parametrize("node", ["pve", "pve-01", "PVE01", "node1.example.com", "1node"])
def test_valid_node_names(node: str) -> None:
    assert parse(["--pve.node", node])[0].pve_node == node


def test_legacy_environment_variables() -> None:
    settings, listen = parse([], {"EXPORTER_PORT": "9200", "COLLECTION_INTERVAL": "30"})
    assert listen == ":9200"
    assert settings.interval == 30.0


def test_flags_override_legacy_environment() -> None:
    env = {"EXPORTER_PORT": "not-a-port", "COLLECTION_INTERVAL": "not-a-number"}
    settings, listen = parse(["--web.listen-address", "127.0.0.1:9300", "--interval", "5"], env)
    assert listen == "127.0.0.1:9300"
    assert settings.interval == 5.0


def test_empty_legacy_environment_is_ignored() -> None:
    settings, listen = parse([], {"EXPORTER_PORT": "", "COLLECTION_INTERVAL": ""})
    assert listen == DEFAULT_LISTEN_ADDRESS
    assert settings.interval == DEFAULT_INTERVAL


@pytest.mark.parametrize(
    ("level", "env", "expected"),
    [
        (None, {}, "INFO"),
        (None, {"DEBUG_MODE": "true"}, "DEBUG"),
        (None, {"DEBUG_MODE": "1"}, "DEBUG"),
        (None, {"DEBUG_MODE": "YES"}, "DEBUG"),
        (None, {"DEBUG_MODE": "false"}, "INFO"),
        (None, {"DEBUG_MODE": "0"}, "INFO"),
        ("warning", {"DEBUG_MODE": "true"}, "WARNING"),
        ("error", {}, "ERROR"),
    ],
)
def test_log_level(
    basic_config_calls: list[dict[str, Any]],
    level: str | None,
    env: dict[str, str],
    expected: str,
) -> None:
    cli._setup_logging(level, env)
    (call,) = basic_config_calls
    assert call["level"] == expected
    assert call["stream"] is sys.stderr
    assert call["format"].startswith("%(asctime)s ")


def test_log_format_under_journald(basic_config_calls: list[dict[str, Any]]) -> None:
    cli._setup_logging(None, {"JOURNAL_STREAM": "8:1234"})
    assert basic_config_calls[0]["format"] == "%(levelname)s %(name)s: %(message)s"


def test_debug_mode_env_reaches_main(
    monkeypatch: pytest.MonkeyPatch, basic_config_calls: list[dict[str, Any]]
) -> None:
    monkeypatch.setenv("DEBUG_MODE", "true")
    assert main(["--check-config"]) == 0
    assert basic_config_calls[0]["level"] == "DEBUG"


# -- select_collectors ---------------------------------------------------------------


def make_context(tmp_path: Path, proc: Mapping[str, str], **settings: Any) -> Context:
    """A context whose collectors see only the fake trees and no commands at all."""
    procfs = write_tree(tmp_path / "proc", proc)
    sysfs = write_tree(tmp_path / "sys", {})
    empty_bin = write_tree(tmp_path / "bin", {})
    return Context(
        Settings(procfs=str(procfs), sysfs=str(sysfs), **settings),
        Runner(path=str(empty_bin)),
        is_root=False,
    )


HOST_FILES = {"stat": FAKE_PROC["stat"], "diskstats": "", "net/dev": ""}


def test_select_collectors_by_detection(tmp_path: Path) -> None:
    ctx = make_context(tmp_path, HOST_FILES)
    selected, enabled = select_collectors(ctx)
    assert [c.name for c in selected] == ["system", "diskstats", "network"]
    assert all(c.ctx is ctx for c in selected)
    assert list(enabled) == ALL_NAMES
    assert {n for n, on in enabled.items() if on} == {"system", "diskstats", "network"}


def test_select_collectors_honours_collectors_flag(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="proxmox_node_exporter")
    ctx = make_context(tmp_path, HOST_FILES, collectors=["system", "zfs"])
    selected, enabled = select_collectors(ctx)
    assert [c.name for c in selected] == ["system"]
    assert {n for n, on in enabled.items() if on} == {"system"}
    assert list(enabled) == ALL_NAMES
    assert "collector zfs was requested but is not supported here" in caplog.text
    assert "diskstats" not in caplog.text  # not requested, so not warned about


def test_select_collectors_honours_disable_flag(tmp_path: Path) -> None:
    ctx = make_context(tmp_path, HOST_FILES, disabled_collectors=["system", "network"])
    selected, enabled = select_collectors(ctx)
    assert [c.name for c in selected] == ["diskstats"]
    assert enabled["system"] is False
    assert enabled["network"] is False

    both = make_context(tmp_path, HOST_FILES, collectors=["system"], disabled_collectors=["system"])
    assert select_collectors(both)[0] == []


def test_select_collectors_survives_failing_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: SystemCollector) -> bool:
        raise PermissionError("denied")

    monkeypatch.setattr(SystemCollector, "detect", broken)
    selected, enabled = select_collectors(make_context(tmp_path, HOST_FILES))
    assert [c.name for c in selected] == ["diskstats", "network"]
    assert enabled["system"] is False


# -- main ------------------------------------------------------------------------------


def test_list_collectors(fake_host: tuple[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    proc, sysfs = fake_host
    assert main(["--list-collectors", "--path.procfs", proc, "--path.sysfs", sysfs]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in lines] == ALL_NAMES
    state = {line.split()[0]: line.split()[1] for line in lines}
    assert state["system"] == "enabled"
    # procfs/sysfs based collectors see only the fake trees.
    for name in ("filesystem", "diskstats", "network", "hwmon", "mdadm", "btrfs"):
        assert state[name] == "disabled", name
    system_line = lines[ALL_NAMES.index("system")]
    assert system_line.endswith(SystemCollector.description)
    width = max(len(n) for n in ALL_NAMES)
    assert system_line.startswith("system".ljust(width) + "  enabled   ")


def test_list_collectors_with_selection(
    fake_host: tuple[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    proc, sysfs = fake_host
    argv = ["--list-collectors", "--path.procfs", proc, "--path.sysfs", sysfs]
    assert main([*argv, "--collectors.disable", "system"]) == 0
    state = {line.split()[0]: line.split()[1] for line in capsys.readouterr().out.splitlines()}
    assert state["system"] == "disabled"


def test_once_prints_parseable_metrics(
    fake_host: tuple[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    proc, sysfs = fake_host
    status = main(
        ["--once", "--collectors", "system", "--path.procfs", proc, "--path.sysfs", sysfs]
    )
    assert status == 0
    got = exposition(capsys.readouterr().out)
    one = lbl()
    assert got["node_load1"] == {one: 0.5}
    assert got["node_load15"] == {one: 0.3}
    assert got["node_memory_MemTotal_bytes"] == {one: 16384000 * 1024}
    assert got["node_uptime_seconds"] == {one: 12345.67}
    assert got["node_boot_time_seconds"] == {one: 1700000000}
    ticks = os.sysconf("SC_CLK_TCK")
    assert got["node_cpu_seconds_total"][lbl(cpu="0", mode="user")] == 100 / ticks
    assert got["proxmox_exporter_collector_success"] == {lbl(collector="system"): 1.0}
    enabled = got["proxmox_exporter_collector_enabled"]
    assert enabled == {lbl(collector=n): (1.0 if n == "system" else 0.0) for n in ALL_NAMES}
    assert "proxmox_exporter_build_info" in got


def test_once_exit_status_when_a_collector_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    proc = write_tree(tmp_path / "proc", {"stat": "cpu0 not-a-number\n"})
    sysfs = write_tree(tmp_path / "sys", {})
    argv = ["--once", "--collectors", "system", "--path.procfs", str(proc), "--path.sysfs"]
    assert main([*argv, str(sysfs)]) == 1
    got = exposition(capsys.readouterr().out)
    assert got["proxmox_exporter_collector_success"] == {lbl(collector="system"): 0.0}
    assert "node_load1" not in got


@pytest.fixture(scope="module")
def tls_pair(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    directory = tmp_path_factory.mktemp("cli-tls")
    crt, key = directory / "tls.crt", directory / "tls.key"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-nodes", "-keyout", str(key), "-out", str(crt), "-days", "2", "-subj", "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    return crt, key


def write_web_config(tmp_path: Path, text: str) -> str:
    path = tmp_path / "web.ini"
    path.write_text(text)
    path.chmod(0o600)
    return str(path)


def test_check_config_valid(
    tmp_path: Path, tls_pair: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    crt, key = tls_pair
    config = write_web_config(
        tmp_path,
        f"[tls]\ncert_file = {crt}\nkey_file = {key}\n\n"
        f"[basic_auth_users]\nprometheus = {hash_password('x' * 16, iterations=1000)}\n",
    )
    assert main(["--check-config", "--web.config-file", config]) == 0
    assert capsys.readouterr().out == "configuration OK\n"


def test_check_config_without_web_config(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--check-config"]) == 0
    assert capsys.readouterr().out == "configuration OK\n"


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("[bogus]\nkey = value\n", "unknown section [bogus]"),
        ("[tls]\ncert_file = /no/tls.crt\nkey_file = /no/tls.key\n", "cannot load TLS material"),
        ("[tls]\ncert_file = /nonexistent/tls.crt\n", "needs both cert_file and key_file"),
        ("[basic_auth_users]\nprometheus = plaintext-password\n", "malformed password hash"),
    ],
)  # fmt: skip
def test_check_config_invalid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str, error: str
) -> None:
    config = write_web_config(tmp_path, text)
    with pytest.raises(SystemExit) as excinfo:
        main(["--check-config", "--web.config-file", config])
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert error in captured.err


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (["--web.config-file", "/nonexistent/web.ini"], "cannot read web config"),
        (["--collectors", "nope"], "unknown collector 'nope'"),
        (["--interval=-1"], "--interval: must be positive"),
        (["--web.listen-address", "::1:9101"], "must be bracketed"),
    ],
)
def test_check_config_rejects_bad_flags(
    capsys: pytest.CaptureFixture[str], argv: list[str], error: str
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--check-config", *argv])
    assert excinfo.value.code == 2
    assert error in capsys.readouterr().err


def test_modes_are_mutually_exclusive(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--once", "--list-collectors"])
    assert excinfo.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == f"proxmox-node-exporter {cli.__version__}"


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_hash_password_from_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("correct horse battery staple\r\nignored\n"))
    assert main(["--hash-password"]) == 0
    captured = capsys.readouterr()
    encoded = captured.out.strip()
    assert captured.out == encoded + "\n"
    assert encoded.startswith("pbkdf2_sha256$")
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("correct horse battery staple\r", encoded)
    assert captured.err == ""


def test_hash_password_warns_about_short_passwords(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("short\n"))
    assert main(["--hash-password"]) == 0
    captured = capsys.readouterr()
    assert verify_password("short", captured.out.strip())
    assert "shorter than 12 characters" in captured.err


@pytest.mark.parametrize("stdin", ["", "\n", "\r\n"])
def test_hash_password_rejects_empty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stdin: str
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    assert main(["--hash-password"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "empty password" in captured.err


def test_hash_password_prompts_on_a_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", _Tty())
    prompts: list[str] = []
    answers = iter(["a long enough password", "a long enough password"])

    def fake_getpass(prompt: str = "Password: ") -> str:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr(cli.getpass, "getpass", fake_getpass)
    assert main(["--hash-password"]) == 0
    assert prompts == ["Password: ", "Repeat password: "]
    assert verify_password("a long enough password", capsys.readouterr().out.strip())

    answers = iter(["first password!!", "second password!"])
    assert main(["--hash-password"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "passwords do not match" in captured.err


# -- serving ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get(port: int, path: str) -> tuple[int, str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.read().decode()
    finally:
        conn.close()


def test_main_serves_until_sigterm(
    fake_host: tuple[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="proxmox_node_exporter")
    proc, sysfs = fake_host
    port = _free_port()
    results: dict[str, Any] = {}
    main_done = threading.Event()

    def client() -> None:
        deadline = time.monotonic() + 5
        try:
            while time.monotonic() < deadline and not main_done.is_set():
                try:
                    status, body = _get(port, "/metrics")
                except (http.client.HTTPException, OSError):
                    time.sleep(0.02)
                    continue
                if "node_load1 " in body:
                    results["metrics"] = (status, body)
                    results["healthz"] = _get(port, "/healthz")
                    break
                time.sleep(0.02)
        finally:
            if not main_done.is_set():
                os.kill(os.getpid(), signal.SIGTERM)

    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    # Never let a stray SIGTERM hit the default handler (it would kill pytest).
    signal.signal(signal.SIGTERM, lambda signum, frame: None)
    thread = threading.Thread(target=client, daemon=True)
    thread.start()
    try:
        argv = ["--web.listen-address", f"127.0.0.1:{port}", "--collectors", "system"]
        argv += ["--path.procfs", proc, "--path.sysfs", sysfs, "--interval", "0.05"]
        status = main(argv)
    finally:
        main_done.set()
        thread.join(10)
        for sig, handler in saved.items():
            signal.signal(sig, handler)
    assert status == 0
    metrics_status, body = results["metrics"]
    assert metrics_status == 200
    assert 'proxmox_exporter_collector_success{collector="system"} 1' in body
    assert results["healthz"] == (200, "ok\n")
    assert f"listening on http://127.0.0.1:{port}" in caplog.text
    assert "received SIGTERM, shutting down" in caplog.text
    assert "without TLS or authentication" not in caplog.text  # loopback only
    with pytest.raises(OSError, match="refused"):
        _get(port, "/healthz")
