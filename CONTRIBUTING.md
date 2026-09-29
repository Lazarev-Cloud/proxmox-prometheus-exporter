# Contributing

Thanks for helping! Bug reports with real command output (e.g. an unusual
`zpool status` or `smartctl --json` from your hardware) are especially
valuable, because they become test fixtures.

## Setup

```sh
git clone https://github.com/Lazarev-Cloud/proxmox-prometheus-exporter.git
cd proxmox-prometheus-exporter
make dev          # .venv with the pinned, hash-checked tools
make check        # what CI runs: format, lint, mypy --strict, shellcheck, actionlint, tests
```

Try it on any Linux box without installing anything:

```sh
PYTHONPATH=src python3 -m proxmox_node_exporter --list-collectors
PYTHONPATH=src python3 -m proxmox_node_exporter --once --collectors system,zfs
```

On a Proxmox test node, `sudo ./install.sh --from-source` installs your
working copy. `sudo bash tests/integration/installer.sh` runs the end-to-end
installer test; it installs and removes the service, so only run it on a
throw-away machine.

## How it fits together

```
src/proxmox_node_exporter/
  cli.py          flags -> Settings, collector selection, main loop
  manager.py      one thread per collector, snapshots, self-metrics
  server.py       HTTP(S) endpoint: auth, TLS, limits
  webconfig.py    TLS/basic-auth config, PBKDF2
  runner.py       the only way to run a command (timeouts, fixed PATH, no shell)
  metrics.py      metric declarations (MetricGroup), Batch, text format
  collectors/     one module per collector
```

- The **runtime uses only the standard library.** Please don't add
  dependencies; the exporter runs as root on hypervisors, and a small supply
  chain is part of the design.
- **Every metric is declared up front** with `MetricGroup` (name, type, help,
  labels). `Batch.add()` rejects mismatched labels, and the declarations
  generate `docs/metrics.md`. Tests check that dashboards and alert rules only
  use declared metrics.
- **Collectors never run commands directly.** They call `self.run(...)`,
  which goes through `Runner`: bare command names only, resolved from a fixed
  PATH, no shell, a timeout on every call, and the process group is killed on
  timeout. Validate any value that comes from the system (device names, UPS
  names, …) before it goes into an argv.
- **Failures:** `collect()` raises only when nothing useful could be
  collected. Skip individual broken items (one disk, one sensor). A failed
  run drops that collector's data and sets `proxmox_exporter_collector_success`
  to 0.

## Adding a collector

1. Create `src/proxmox_node_exporter/collectors/<name>.py`:

   ```python
   M = MetricGroup("example")
   TEMP = M.gauge("node_example_temperature_celsius", "Example temperature.", "sensor")


   class ExampleCollector(Collector):
       name = "example"
       description = "What it collects"
       default_interval = 30.0  # optional; None = global --interval

       def detect(self) -> bool:  # cheap: does the host have it?
           return self.has_command("example-tool")

       def collect(self, out: Batch) -> None:
           result = self.run("example-tool", "--json", timeout=10)
           for item in json.loads(result.stdout):
               out.add(TEMP, item["temp"], sensor=item["name"])
   ```

2. Register it in `collectors/__init__.py`.
3. Add `tests/test_collector_<name>.py`. Use real tool output as fixtures in
   `tests/fixtures/<name>/`, `FakeRunner` for commands, and `make_ctx` +
   `write_tree` for procfs/sysfs trees (see `tests/conftest.py`).
4. Run `make docs` to regenerate `docs/metrics.md`. Add alerts and dashboard
   panels if they make sense.

Metric naming follows the [Prometheus conventions](https://prometheus.io/docs/practices/naming/):
base units (`_bytes`, `_seconds`, `_celsius`), `_total` for counters, and
node_exporter's names where the same data exists there.

## Pull requests

- Keep changes focused, and add or extend tests.
- `make check` must pass. CI also runs the tests on Python 3.9–3.13, runs
  promtool, builds and smoke-tests the release, and runs the installer
  end-to-end.
- Update `CHANGELOG.md` for anything users will notice.

## Releasing (maintainers)

1. Bump `__version__` in `src/proxmox_node_exporter/__init__.py` and move the
   changelog entries under the new version.
2. Merge, then `git tag -s vX.Y.Z -m vX.Y.Z && git push origin vX.Y.Z`.
3. The release workflow checks that the tag matches `__version__`. It builds
   the zipapp, tarball and wheel, writes `SHA256SUMS`, creates signed build
   provenance and publishes the GitHub release. Nodes with `--auto-update`
   pick it up within a week.

## Code of conduct

Be kind and constructive; assume good intent.
