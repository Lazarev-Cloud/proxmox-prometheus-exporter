from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import pytest

from conftest import FakeRunner, Response, Samples, collect, fixture, value
from proxmox_node_exporter.collectors.base import Context
from proxmox_node_exporter.collectors.containers import (
    ContainersCollector,
    parse_pair,
    parse_size,
)
from proxmox_node_exporter.runner import CommandError, CommandResult

DOCKER_PS = ("docker", "ps", "-a", "--no-trunc", "--format", "{{json .}}")
DOCKER_STATS = ("docker", "stats", "--no-stream", "--format", "{{json .}}")
PODMAN_PS = ("podman", "ps", "-a", "--no-trunc", "--format", "json")
PODMAN_STATS = ("podman", "stats", "--no-stream", "--format", "json")
MIB = 1024**2
GIB = 1024**3

WEB = {"name": "web", "id": "3f4e1c2b9a8d", "runtime": "docker"}
DB = {"name": "db", "id": "a1b2c3d4e5f6", "runtime": "docker"}
BACKUP = {"name": "backup-job", "id": "0f9e8d7c6b5a", "runtime": "docker"}
OLD_APP = {"name": "old-app", "id": "5566778899aa", "runtime": "docker"}
PROXY = {"name": "proxy", "id": "7c9e8d6f5a4b", "runtime": "podman"}
GRAFANA = {"name": "grafana", "id": "1d2c3b4a5968", "runtime": "podman"}

DOCKER_DOWN = CommandResult(
    1,
    "",
    "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
    "Is the docker daemon running?\n",
)


@pytest.fixture
def docker_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "docker.sock"
    monkeypatch.setattr(ContainersCollector, "DOCKER_SOCKET", str(path))
    path.touch()
    return path


def docker(**overrides: Response) -> dict[tuple[str, ...], Response]:
    responses: dict[tuple[str, ...], Response] = {
        DOCKER_PS: fixture("containers/docker_ps.jsonl"),
        DOCKER_STATS: fixture("containers/docker_stats.jsonl"),
    }
    commands = {"ps": DOCKER_PS, "stats": DOCKER_STATS}
    responses.update({commands[cmd]: r for cmd, r in overrides.items()})
    return responses


def podman(**overrides: Response) -> dict[tuple[str, ...], Response]:
    responses: dict[tuple[str, ...], Response] = {
        PODMAN_PS: fixture("containers/podman_ps.json"),
        PODMAN_STATS: fixture("containers/podman_stats.json"),
    }
    commands = {"ps": PODMAN_PS, "stats": PODMAN_STATS}
    responses.update({commands[cmd]: r for cmd, r in overrides.items()})
    return responses


def counts(samples: Samples, runtime: str) -> dict[str, float]:
    return {
        dict(k)["state"]: v
        for k, v in samples["node_container_count"].items()
        if ("runtime", runtime) in k
    }


# -- size parsing ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "size"),
    [
        ("0B", 0),
        ("512", 512),
        ("12.3kB", 12300),  # docker NetIO/BlockIO: decimal units
        ("1.95MB", 1.95e6),
        ("16.66GB", 16.66e9),
        ("1TB", 1e12),
        ("11.2MiB", 11.2 * MIB),  # docker MemUsage: binary units
        ("7.664GiB", 7.664 * GIB),
        ("1.5KiB", 1536),
        ("2TiB", 2 * 1024**4),
        (" 12 kB ", 12000),
        ("--", None),
        ("", None),
        ("1.2XB", None),
        ("-1B", None),
    ],
)
def test_parse_size(text: str, size: float | None) -> None:
    assert parse_size(text) == (None if size is None else pytest.approx(size))


def test_parse_pair() -> None:
    assert parse_pair("11.2MiB / 7.664GiB") == (
        pytest.approx(11.2 * MIB),
        pytest.approx(7.664 * GIB),
    )
    assert parse_pair("12.3kB / 0B") == (pytest.approx(12300), 0)
    assert parse_pair("-- / --") == (None, None)
    assert parse_pair("--") == (None, None)
    assert parse_pair(None) == (None, None)


# -- detection -------------------------------------------------------------------------


def test_detect_docker(make_ctx: Callable[..., Context], docker_socket: Path) -> None:
    collector = ContainersCollector(make_ctx(FakeRunner(docker())))
    assert collector.runtimes() == ["docker"]
    assert collector.detect()
    assert not ContainersCollector(make_ctx(FakeRunner(docker()), is_root=False)).detect()


def test_detect_docker_cli_without_daemon(
    make_ctx: Callable[..., Context], docker_socket: Path
) -> None:
    docker_socket.unlink()
    collector = ContainersCollector(make_ctx(FakeRunner(docker())))
    assert collector.runtimes() == []
    assert not collector.detect()


def test_detect_podman(make_ctx: Callable[..., Context], docker_socket: Path) -> None:
    collector = ContainersCollector(make_ctx(FakeRunner({**docker(), **podman()})))
    assert collector.runtimes() == ["docker", "podman"]
    docker_socket.unlink()
    collector = ContainersCollector(make_ctx(FakeRunner({**docker(), **podman()})))
    assert collector.runtimes() == ["podman"]
    assert collector.detect()


def test_detect_nothing(make_ctx: Callable[..., Context], docker_socket: Path) -> None:
    assert not ContainersCollector(make_ctx(FakeRunner())).detect()


# -- collection ------------------------------------------------------------------------


def test_docker(make_ctx: Callable[..., Context], docker_socket: Path) -> None:
    runner = FakeRunner(docker())
    samples = collect(ContainersCollector(make_ctx(runner)))
    assert runner.calls == [DOCKER_PS, DOCKER_STATS]
    assert counts(samples, "docker") == {"running": 2, "exited": 1, "created": 1}
    assert value(samples, "node_container_running", **WEB) == 1
    assert value(samples, "node_container_running", **DB) == 1
    assert value(samples, "node_container_running", **BACKUP) == 0
    assert value(samples, "node_container_running", **OLD_APP) == 0
    assert value(samples, "node_container_cpu_usage_percent", **WEB) == 0.02
    assert value(samples, "node_container_cpu_usage_percent", **DB) == 1.37
    assert value(samples, "node_container_memory_usage_bytes", **WEB) == pytest.approx(11.2 * MIB)
    assert value(samples, "node_container_memory_limit_bytes", **WEB) == pytest.approx(7.664 * GIB)
    assert value(samples, "node_container_memory_usage_bytes", **DB) == pytest.approx(64.5 * MIB)
    assert value(samples, "node_container_network_receive_bytes_total", **WEB) == 1.95e6
    assert value(samples, "node_container_network_transmit_bytes_total", **WEB) == 3.47e6
    assert value(samples, "node_container_network_receive_bytes_total", **DB) == 12300
    assert value(samples, "node_container_network_transmit_bytes_total", **DB) == 0
    # Only running containers have stats.
    assert len(samples["node_container_cpu_usage_percent"]) == 2


def test_podman(make_ctx: Callable[..., Context]) -> None:
    runner = FakeRunner(podman())
    samples = collect(ContainersCollector(make_ctx(runner)))
    assert runner.calls == [PODMAN_PS, PODMAN_STATS]
    assert counts(samples, "podman") == {"running": 1, "exited": 1}
    assert value(samples, "node_container_running", **PROXY) == 1
    assert value(samples, "node_container_running", **GRAFANA) == 0
    assert value(samples, "node_container_cpu_usage_percent", **PROXY) == 0.08
    assert value(samples, "node_container_memory_usage_bytes", **PROXY) == pytest.approx(24.87e6)
    assert value(samples, "node_container_memory_limit_bytes", **PROXY) == pytest.approx(16.66e9)
    assert value(samples, "node_container_network_receive_bytes_total", **PROXY) == 1.234e6
    assert value(samples, "node_container_network_transmit_bytes_total", **PROXY) == 5.678e6


def test_docker_and_podman(make_ctx: Callable[..., Context], docker_socket: Path) -> None:
    samples = collect(ContainersCollector(make_ctx(FakeRunner({**docker(), **podman()}))))
    assert counts(samples, "docker")["running"] == 2
    assert counts(samples, "podman")["running"] == 1
    assert {dict(k)["runtime"] for k in samples["node_container_running"]} == {"docker", "podman"}


def test_container_stopped_between_ps_and_stats(
    make_ctx: Callable[..., Context], docker_socket: Path
) -> None:
    web = fixture("containers/docker_stats.jsonl").splitlines()[0]
    gone = {
        "BlockIO": "--", "CPUPerc": "--", "Container": "a1b2c3d4e5f6", "ID": "a1b2c3d4e5f6",
        "MemPerc": "--", "MemUsage": "-- / --", "Name": "db", "NetIO": "--", "PIDs": "--",
    }  # fmt: skip
    runner = FakeRunner(docker(stats=f"{web}\n{json.dumps(gone)}\n"))
    samples = collect(ContainersCollector(make_ctx(runner)))
    assert value(samples, "node_container_cpu_usage_percent", **WEB) == 0.02
    for metric in (
        "node_container_cpu_usage_percent",
        "node_container_memory_usage_bytes",
        "node_container_memory_limit_bytes",
        "node_container_network_receive_bytes_total",
    ):
        assert value(samples, metric, **DB) is None


def test_no_running_containers_skips_stats(
    make_ctx: Callable[..., Context], docker_socket: Path
) -> None:
    exited = [
        line for line in fixture("containers/docker_ps.jsonl").splitlines() if "Exited" in line
    ]
    runner = FakeRunner(docker(ps="\n".join(exited) + "\n"))
    samples = collect(ContainersCollector(make_ctx(runner)))
    assert runner.calls == [DOCKER_PS]
    assert counts(samples, "docker") == {"running": 0, "exited": 1}


def test_no_containers(make_ctx: Callable[..., Context], docker_socket: Path) -> None:
    runner = FakeRunner({**docker(ps=""), **podman(ps="[]\n")})
    samples = collect(ContainersCollector(make_ctx(runner)))
    assert counts(samples, "docker") == {"running": 0, "exited": 0}
    assert counts(samples, "podman") == {"running": 0, "exited": 0}
    assert "node_container_running" not in samples
    assert DOCKER_STATS not in runner.calls
    assert PODMAN_STATS not in runner.calls


def test_one_failing_runtime_does_not_fail_the_collector(
    make_ctx: Callable[..., Context], docker_socket: Path
) -> None:
    runner = FakeRunner({**docker(ps=DOCKER_DOWN), **podman()})
    samples = collect(ContainersCollector(make_ctx(runner)))
    assert {dict(k)["runtime"] for k in samples["node_container_count"]} == {"podman"}


def test_invalid_output_counts_as_failure(
    make_ctx: Callable[..., Context], docker_socket: Path
) -> None:
    runner = FakeRunner({**docker(ps='template: :1: function "json" not defined\n'), **podman()})
    samples = collect(ContainersCollector(make_ctx(runner)))
    assert value(samples, "node_container_running", **PROXY) == 1
    runner = FakeRunner(docker(ps='template: :1: function "json" not defined\n'))
    with pytest.raises(RuntimeError, match="no container runtime responded"):
        collect(ContainersCollector(make_ctx(runner)))


def test_every_runtime_failing_fails_the_collector(
    make_ctx: Callable[..., Context], docker_socket: Path
) -> None:
    runner = FakeRunner(
        {**docker(ps=DOCKER_DOWN), **podman(stats=CommandError("podman: timed out after 30s"))}
    )
    with pytest.raises(RuntimeError, match="no container runtime responded"):
        collect(ContainersCollector(make_ctx(runner)))
