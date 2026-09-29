"""Command line entry point."""

from __future__ import annotations

import argparse
import getpass
import logging
import math
import os
import re
import signal
import sys
import threading
from collections.abc import Sequence

from . import __version__
from .collectors import ALL_COLLECTORS
from .collectors.base import Collector, Context
from .config import (
    DEFAULT_DISK_DEVICE_EXCLUDE,
    DEFAULT_FS_TYPES_EXCLUDE,
    DEFAULT_INTERVAL,
    DEFAULT_LISTEN_ADDRESS,
    DEFAULT_MOUNT_POINTS_EXCLUDE,
    DEFAULT_NETWORK_DEVICE_EXCLUDE,
    DEFAULT_PVE_API_URL,
    DEFAULT_SYSTEMD_UNIT_EXCLUDE,
    DEFAULT_SYSTEMD_UNIT_INCLUDE,
    Settings,
)
from .manager import Manager
from .metrics import render
from .server import make_server, parse_listen_address
from .webconfig import ConfigError, TLSContextProvider, WebConfig, hash_password, load_web_config

log = logging.getLogger("proxmox_node_exporter")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="proxmox-node-exporter",
        description="Prometheus exporter for Proxmox VE hosts.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once",
        action="store_true",
        help="collect once, print the metrics and exit (status 1 if a collector failed)",
    )
    mode.add_argument(
        "--list-collectors", action="store_true", help="show collectors and whether they run here"
    )
    mode.add_argument(
        "--check-config", action="store_true", help="validate the configuration and exit"
    )
    mode.add_argument(
        "--hash-password",
        action="store_true",
        help="read a password (prompt or stdin) and print a hash for the web config file",
    )

    web = parser.add_argument_group("web")
    web.add_argument(
        "--web.listen-address",
        dest="listen_address",
        metavar="ADDR",
        help=f"address to listen on, e.g. :9101, 127.0.0.1:9101 or [::1]:9101 "
        f"(default: {DEFAULT_LISTEN_ADDRESS})",
    )
    web.add_argument(
        "--web.config-file",
        dest="web_config_file",
        metavar="PATH",
        help="INI file enabling TLS and/or basic auth (see docs/security.md)",
    )
    web.add_argument(
        "--web.max-connections", dest="max_connections", type=int, default=32, metavar="N"
    )
    web.add_argument(
        "--web.request-timeout", dest="request_timeout", type=float, default=15.0, metavar="SEC"
    )

    col = parser.add_argument_group("collectors")
    col.add_argument(
        "--collectors",
        metavar="LIST",
        help="comma-separated collectors to run (default: every collector supported here)",
    )
    col.add_argument(
        "--collectors.disable", dest="disable", metavar="LIST", help="collectors to disable"
    )
    col.add_argument(
        "--interval",
        type=float,
        metavar="SEC",
        help=f"default seconds between collector runs (default: {DEFAULT_INTERVAL:g})",
    )
    col.add_argument(
        "--collector.interval",
        dest="intervals",
        action="append",
        default=[],
        metavar="NAME=SEC",
        help="override the interval of one collector (repeatable)",
    )
    col.add_argument("--path.procfs", dest="procfs", default="/proc", metavar="PATH")
    col.add_argument("--path.sysfs", dest="sysfs", default="/sys", metavar="PATH")
    col.add_argument(
        "--filesystem.fs-types-exclude", dest="fs_types_exclude", default=DEFAULT_FS_TYPES_EXCLUDE
    )
    col.add_argument(
        "--filesystem.mount-points-exclude",
        dest="mount_points_exclude",
        default=DEFAULT_MOUNT_POINTS_EXCLUDE,
    )
    col.add_argument(
        "--diskstats.device-exclude", dest="disk_exclude", default=DEFAULT_DISK_DEVICE_EXCLUDE
    )
    col.add_argument(
        "--network.device-exclude", dest="net_exclude", default=DEFAULT_NETWORK_DEVICE_EXCLUDE
    )
    col.add_argument(
        "--systemd.unit-include", dest="unit_include", default=DEFAULT_SYSTEMD_UNIT_INCLUDE
    )
    col.add_argument(
        "--systemd.unit-exclude", dest="unit_exclude", default=DEFAULT_SYSTEMD_UNIT_EXCLUDE
    )
    col.add_argument(
        "--zfs.no-datasets",
        dest="zfs_datasets",
        action="store_false",
        help="do not export per-dataset ZFS usage",
    )
    col.add_argument(
        "--ups.target",
        dest="ups_targets",
        action="append",
        default=[],
        metavar="UPS@HOST",
        help="NUT UPS to monitor (repeatable; default: all UPSes on localhost)",
    )

    pve = parser.add_argument_group("proxmox")
    pve.add_argument(
        "--pve.node", dest="pve_node", metavar="NAME", help="Proxmox node name (auto-detected)"
    )
    pve.add_argument(
        "--pve.api-token-file",
        dest="pve_token_file",
        metavar="PATH",
        help="read guest/storage data from the Proxmox API with the token in this file "
        "(USER@REALM!TOKENID=SECRET) instead of running pvesh as root",
    )
    pve.add_argument("--pve.api-url", dest="pve_api_url", default=DEFAULT_PVE_API_URL)
    pve.add_argument(
        "--pve.api-ca-file",
        dest="pve_ca_file",
        metavar="PATH",
        help="CA bundle for the API certificate (default: /etc/pve/pve-root-ca.pem if readable)",
    )
    pve.add_argument(
        "--pve.api-insecure-skip-verify",
        dest="pve_insecure",
        action="store_true",
        help="do not verify the API certificate (only sensible for 127.0.0.1)",
    )

    parser.add_argument(
        "--log.level",
        dest="log_level",
        choices=["debug", "info", "warning", "error"],
        default=None,
        help="log level (default: info)",
    )
    return parser


def _split(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def settings_from_args(args: argparse.Namespace, env: dict[str, str]) -> tuple[Settings, str]:
    """Build settings; raises ValueError with a user-facing message."""
    known = {cls.name for cls in ALL_COLLECTORS}
    requested = _split(args.collectors) if args.collectors else None
    disabled = _split(args.disable)
    for name in (requested or []) + disabled:
        if name not in known:
            raise ValueError(f"unknown collector {name!r} (known: {', '.join(sorted(known))})")

    intervals: dict[str, float] = {}
    for item in args.intervals:
        name, sep, seconds = item.partition("=")
        if not sep or name not in known:
            raise ValueError(f"--collector.interval expects NAME=SECONDS, got {item!r}")
        intervals[name] = _positive(seconds, f"--collector.interval {name}")

    # Environment variables of the 2.x script are still honoured.
    interval = args.interval
    if interval is not None:
        interval = _positive(str(interval), "--interval")
    elif env.get("COLLECTION_INTERVAL"):
        interval = _positive(env["COLLECTION_INTERVAL"], "COLLECTION_INTERVAL")
    listen = args.listen_address
    if listen is None and env.get("EXPORTER_PORT"):
        listen = f":{env['EXPORTER_PORT']}"
    listen = listen or DEFAULT_LISTEN_ADDRESS
    parse_listen_address(listen)

    for flag, pattern in (
        ("--filesystem.fs-types-exclude", args.fs_types_exclude),
        ("--filesystem.mount-points-exclude", args.mount_points_exclude),
        ("--diskstats.device-exclude", args.disk_exclude),
        ("--network.device-exclude", args.net_exclude),
        ("--systemd.unit-include", args.unit_include),
        ("--systemd.unit-exclude", args.unit_exclude),
    ):
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"{flag}: invalid regular expression: {exc}") from None

    if args.pve_node and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", args.pve_node):
        raise ValueError(f"--pve.node: invalid node name {args.pve_node!r}")
    if args.max_connections < 1:
        raise ValueError("--web.max-connections must be at least 1")
    _positive(str(args.request_timeout), "--web.request-timeout")
    for target in args.ups_targets:
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*(@[A-Za-z0-9_.:\[\]-]+)?", target):
            raise ValueError(f"--ups.target: invalid UPS name {target!r}")
    if not args.pve_api_url.startswith("https://"):
        raise ValueError("--pve.api-url must use https://")

    settings = Settings(
        procfs=args.procfs,
        sysfs=args.sysfs,
        interval=interval or DEFAULT_INTERVAL,
        intervals=intervals,
        collectors=requested,
        disabled_collectors=disabled,
        filesystem_fs_types_exclude=args.fs_types_exclude,
        filesystem_mount_points_exclude=args.mount_points_exclude,
        diskstats_device_exclude=args.disk_exclude,
        network_device_exclude=args.net_exclude,
        systemd_unit_include=args.unit_include,
        systemd_unit_exclude=args.unit_exclude,
        zfs_datasets=args.zfs_datasets,
        pve_node=args.pve_node,
        pve_api_url=args.pve_api_url.rstrip("/"),
        pve_api_token_file=args.pve_token_file,
        pve_api_ca_file=args.pve_ca_file,
        pve_api_insecure=args.pve_insecure,
        ups_targets=args.ups_targets,
    )
    return settings, listen


def _positive(text: str, what: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"{what}: not a number: {text!r}") from None
    if not 0 < value < math.inf:  # also rejects NaN
        raise ValueError(f"{what}: must be positive and finite")
    return value


def select_collectors(ctx: Context) -> tuple[list[Collector], dict[str, bool]]:
    settings = ctx.settings
    selected: list[Collector] = []
    enabled: dict[str, bool] = {}
    for cls in ALL_COLLECTORS:
        enabled[cls.name] = False
        if settings.collectors is not None and cls.name not in settings.collectors:
            continue
        if cls.name in settings.disabled_collectors:
            continue
        collector = cls(ctx)
        try:
            supported = collector.detect()
        except Exception as exc:  # noqa: BLE001
            log.debug("detection of %s failed: %s", cls.name, exc)
            supported = False
        if not supported:
            if settings.collectors is not None:
                log.warning("collector %s was requested but is not supported here", cls.name)
            continue
        enabled[cls.name] = True
        selected.append(collector)
    return selected, enabled


def _setup_logging(level_name: str | None, env: dict[str, str]) -> None:
    if level_name is None:
        debug = env.get("DEBUG_MODE", "").lower() in ("1", "true", "yes")
        level_name = "debug" if debug else "info"
    # journald timestamps every line itself.
    fmt = "%(levelname)s %(name)s: %(message)s"
    if "JOURNAL_STREAM" not in env:
        fmt = "%(asctime)s " + fmt
    logging.basicConfig(level=level_name.upper(), format=fmt, stream=sys.stderr)


def _hash_password() -> int:
    if sys.stdin.isatty():
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Repeat password: "):
            print("passwords do not match", file=sys.stderr)
            return 1
    else:
        password = sys.stdin.readline().rstrip("\r\n")
    if not password:
        print("empty password", file=sys.stderr)
        return 1
    if len(password) < 12:
        print("warning: passwords shorter than 12 characters are weak", file=sys.stderr)
    print(hash_password(password))
    return 0


class _Exporter:
    def __init__(self, manager: Manager) -> None:
        self.manager = manager

    def render_metrics(self) -> bytes:
        return render(self.manager.gather())

    def healthy(self) -> bool:
        return self.manager.healthy()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    env = dict(os.environ)
    _setup_logging(args.log_level, env)

    if args.hash_password:
        return _hash_password()

    try:
        settings, listen = settings_from_args(args, env)
        web_config = load_web_config(args.web_config_file) if args.web_config_file else WebConfig()
        if web_config.tls_enabled:
            TLSContextProvider(web_config)  # fail fast on unreadable certificates
    except (ValueError, ConfigError) as exc:
        parser.error(str(exc))

    ctx = Context(settings=settings)
    collectors, enabled = select_collectors(ctx)

    if args.list_collectors:
        width = max(len(cls.name) for cls in ALL_COLLECTORS)
        for cls in ALL_COLLECTORS:
            state = "enabled " if enabled[cls.name] else "disabled"
            print(f"{cls.name:<{width}}  {state}  {cls.description}")
        return 0
    if args.check_config:
        print("configuration OK")
        return 0

    manager = Manager(collectors, enabled, settings.interval, settings.intervals, settings.procfs)
    if args.once:
        ok = manager.run_once()
        sys.stdout.buffer.write(render(manager.gather()))
        sys.stdout.flush()
        return 0 if ok else 1

    try:
        server = make_server(
            listen,
            _Exporter(manager),
            web_config,
            max_connections=args.max_connections,
            request_timeout=args.request_timeout,
        )
    except OSError as exc:
        log.error("cannot listen on %s: %s", listen, exc)
        return 1

    stop = threading.Event()

    def _on_signal(signum: int, _frame: object) -> None:
        log.info("received %s, shutting down", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    host, _ = parse_listen_address(listen)
    if not web_config.users and not web_config.tls_enabled and host not in ("127.0.0.1", "::1"):
        log.warning(
            "serving metrics without TLS or authentication; restrict access with a firewall "
            "or configure --web.config-file"
        )
    if settings.pve_api_insecure:
        log.warning("--pve.api-insecure-skip-verify: the Proxmox API certificate is not verified")
    if os.geteuid() != 0 and not settings.pve_api_token_file:
        log.info("not running as root: SMART, IPMI and Proxmox guest metrics are unavailable")

    manager.start()
    scheme = "https" if web_config.tls_enabled else "http"
    log.info(
        "proxmox-node-exporter %s listening on %s://%s (collectors: %s)",
        __version__,
        scheme,
        listen,
        ", ".join(c.name for c in collectors) or "none",
    )
    serve = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    serve.start()
    while not stop.wait(1.0):
        pass
    server.shutdown()
    server.server_close()
    manager.stop()
    return 0
