# Proxmox Node Exporter

[![CI](https://github.com/Lazarev-Cloud/proxmox-prometheus-exporter/actions/workflows/ci.yml/badge.svg)](https://github.com/Lazarev-Cloud/proxmox-prometheus-exporter/actions/workflows/ci.yml)
[![CodeQL](https://github.com/Lazarev-Cloud/proxmox-prometheus-exporter/actions/workflows/codeql.yml/badge.svg)](https://github.com/Lazarev-Cloud/proxmox-prometheus-exporter/actions/workflows/codeql.yml)
[![License: BSD-3-Clause](https://img.shields.io/badge/License-BSD_3--Clause-blue.svg)](LICENSE)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)

One Prometheus exporter per Proxmox VE node that covers the host, its hardware,
its storage and the guests running on it. It detects what the node has (ZFS,
NVIDIA/AMD/Intel GPUs, IPMI, UPS, software RAID, …) and exports only what
applies.

- **Proxmox:** per-guest CPU, memory, disk and network; guest counts; storage
  usage; cluster quorum and member state; storage replication; TLS certificate
  expiry; PVE version.
- **Storage:** ZFS pool health, capacity, fragmentation, scrubs, per-vdev
  errors, ARC/L2ARC and dataset usage; mdadm arrays; Btrfs; per-disk I/O;
  filesystems (a hung NFS mount or suspended ZFS pool can't stall the exporter).
- **Hardware:** SMART health and error counters, SSD/NVMe wear, disk
  temperatures (sleeping disks are not woken); hwmon temperatures, fans,
  voltages and power; IPMI/BMC sensors; GPUs; NUT UPS status.
- **Host:** CPU, memory, load, pressure stall information, network (incl.
  bonds), systemd units. Names follow node_exporter, so its dashboards work too.

**Secure by design:** the exporter uses only the Python standard library, so no
third-party code runs as root on your hypervisor. It runs in a tightly
sandboxed systemd unit, and optionally as an unprivileged user with a read-only
API token. It supports TLS and basic auth, and releases are checksummed,
reproducible and carry signed build provenance. See [docs/security.md](docs/security.md).

**Cheap to scrape:** collectors run on their own schedules in the background;
a scrape only serialises the latest results, so it takes milliseconds and can
never make the node spawn processes.

## Quick start

On each Proxmox node (Proxmox VE 7, 8 and 9; any Debian-based host works for the
non-Proxmox metrics):

```sh
curl -fsSLO https://github.com/Lazarev-Cloud/proxmox-prometheus-exporter/releases/latest/download/install.sh
less install.sh          # always read scripts before running them as root
sudo bash install.sh
curl -s http://localhost:9101/metrics | head
```

The installer:

1. installs the few Debian packages that help (`smartmontools`, `lm-sensors`,
   and `ipmitool` when a BMC exists);
2. downloads the release tarball and checks it against the release's
   `SHA256SUMS`;
3. installs the single-file exporter to `/opt/proxmox-node-exporter` and a
   hardened systemd unit;
4. checks that the exporter answers on `/healthz`, and rolls back to the
   previous version automatically if it doesn't.

Running it again with the same version changes nothing. Useful options:

| Option | Effect |
| --- | --- |
| `--listen 10.0.0.5:9101` | Listen on one address only (default `:9101`, all interfaces). |
| `--web-config /etc/proxmox-node-exporter/web.ini` | Turn on TLS and/or basic auth ([how](docs/security.md#tls-and-basic-auth)). |
| `--unprivileged` | Run as user `proxmox-exporter` with no capabilities; guest data comes from a read-only (PVEAuditor) API token that the installer creates. SMART, IPMI and container metrics are not available in this mode. |
| `--auto-update` | Weekly timer that installs new releases (checksum-verified, rolled back if unhealthy). |
| `--version v3.0.0` | Install a specific release. `--sha256 HASH` pins the download. |
| `--update` | Upgrade to the latest release. |
| `--uninstall [--purge]` | Remove the exporter (and its configuration). |
| `--from-source` | Install from a git checkout: `git clone … && sudo ./install.sh --from-source`. |

Run `bash install.sh --help` for everything else. For many nodes, use the
Ansible playbook in [`deploy/ansible/playbook.yml`](deploy/ansible/playbook.yml).

<details>
<summary>Verifying a release by hand</summary>

```sh
v=3.0.0
base=https://github.com/Lazarev-Cloud/proxmox-prometheus-exporter/releases/download/v$v
curl -fsSLO "$base/proxmox-node-exporter-$v.tar.gz" -O "$base/SHA256SUMS"
sha256sum -c --ignore-missing SHA256SUMS
# Signed build provenance (needs the GitHub CLI):
gh attestation verify proxmox-node-exporter-$v.tar.gz --repo Lazarev-Cloud/proxmox-prometheus-exporter
# Builds are reproducible: `python3 tools/build_pyz.py` on the tag gives the same bytes.
```
</details>

## Prometheus

```yaml
scrape_configs:
  - job_name: proxmox-nodes
    scrape_interval: 30s
    scrape_timeout: 20s
    static_configs:
      - targets: [pve1:9101, pve2:9101, pve3:9101]
rule_files:
  - /etc/prometheus/rules/proxmox-node-exporter.yml   # prometheus/alerts.yml
```

[`prometheus/alerts.yml`](prometheus/alerts.yml) holds 44 alerts, unit-tested
with `promtool`. They cover a lost cluster quorum, offline members, degraded or
erroring ZFS pools, overdue scrubs, degraded RAID, failing or wearing disks,
filling filesystems and storages, read-only remounts, hung mounts, failed
systemd units, hardware temperature alarms, BMC sensors, UPS on battery,
failing replication, expiring certificates, OOM kills and more.
[`prometheus/prometheus.example.yml`](prometheus/prometheus.example.yml) also
shows the TLS and basic-auth variant.

## Grafana

Import the dashboards from [`grafana/`](grafana/) (Dashboards → New → Import).
Each one has a data source picker and a node selector.

| Dashboard | Shows |
| --- | --- |
| `proxmox-overview.json` | Cluster-wide overview: CPU, memory, load, network, disks, ZFS, guests, GPUs, temperatures |
| `proxmox-cluster.json` | Per-node drill-down with history |
| `proxmox-node.json` | A single node in detail, including systemd and swap |
| `proxmox-health.json` | Guests table and top-N, Proxmox storage, ZFS pools, SMART disk table, quorum, replication, certificates, UPS and power |

A CI test fails if a dashboard queries a metric that the exporter doesn't produce.

## Collectors

`proxmox-node-exporter --list-collectors` shows which collectors are enabled on
a host. Each runs in its own thread at its own interval.

| Collector | Metrics | Needs | Interval |
| --- | --- | --- | --- |
| `system` | CPU, memory, load, PSI, vmstat, file descriptors, `node_info` | – | 15s |
| `filesystem` | size, free, available, inodes, read-only, statfs errors | – | 15s |
| `diskstats` | throughput, IOPS, latency, queue, busy time, LVM names | – | 15s |
| `network` | traffic, errors, drops, link, speed, bonds, TCP states | – | 15s |
| `hwmon` | temperatures (+ thresholds, alarms), fans, voltage, current, power | kernel drivers (`sensors-detect`) | 15s |
| `pve` | guests, storages, quorum, members, replication, certificates, version | root + `pvesh`, or an API token | 30s |
| `zfs` | ARC/L2ARC, pool health/capacity/fragmentation/dedup, scrubs, vdev errors, datasets | `zpool`, `zfs` | 30s |
| `smart` | health, temperature, reallocated/pending/uncorrectable sectors, CRC errors, SSD/NVMe wear, NVMe spare/warnings | root + `smartctl` | 120s |
| `gpu` | utilisation, memory, temperature, power, clocks, fan, PCIe link | `nvidia-smi`, or AMD/Intel DRM sysfs | 15s |
| `ipmi` | BMC sensor values and states | root + `ipmitool` + a BMC | 60s |
| `mdadm` | array state, members, degraded count, resync progress | `/proc/mdstat` | 15s |
| `btrfs` | chunk allocation, device errors, missing devices | `/sys/fs/btrfs` | 15s |
| `systemd` | system state, units by state, per-service state | systemd | 30s |
| `ups` | charge, runtime, load, voltages, on-battery/low/replace flags | NUT `upsc` with a configured UPS | 15s |
| `containers` | Docker/Podman container states, CPU, memory, network | root + docker/podman | 30s |

The full list of metrics, with types and labels, is in
[docs/metrics.md](docs/metrics.md). It is generated from the code.

## Configuration

Options go into `ARGS` in `/etc/default/proxmox-node-exporter`. Apply them with
`systemctl restart proxmox-node-exporter`.

```sh
ARGS="--web.listen-address=10.0.0.5:9101 --collectors.disable=containers --collector.interval=smart=300"
```

| Flag | Default | Purpose |
| --- | --- | --- |
| `--web.listen-address` | `:9101` | `host:port`, `[v6]:port` or `:port` (dual-stack) |
| `--web.config-file` | – | INI file with TLS and basic-auth settings ([example](packaging/web.ini.example)) |
| `--collectors` / `--collectors.disable` | all supported | comma-separated collector names |
| `--interval`, `--collector.interval NAME=SEC` | 15s, per collector | how often collectors run |
| `--filesystem.mount-points-exclude`, `--filesystem.fs-types-exclude` | pseudo filesystems | regexes |
| `--diskstats.device-exclude` | partitions, loop, ram | regex |
| `--network.device-exclude` | `lo`, firewall bridges | regex |
| `--systemd.unit-include` / `--systemd.unit-exclude` | `*.service` minus templated gettys etc. | regexes |
| `--zfs.no-datasets` | – | skip per-dataset usage (large pools) |
| `--ups.target UPS@HOST` | all UPSes on localhost | repeatable |
| `--pve.api-token-file`, `--pve.api-url`, `--pve.api-ca-file` | – | read Proxmox data over the API instead of `pvesh` |
| `--log.level` | `info` | `debug` shows every collector run |

Run `proxmox-node-exporter --help` for the full list. `proxmox-node-exporter
--once` runs every collector once, prints the metrics and exits; it is the
quickest way to debug.

## Upgrading from 2.x

Run the installer. It keeps the service name (`proxmox-node-exporter`) and port
(`9101`), and removes the old `/opt/proxmox-exporter` script and virtualenv.
Things to know:

- Version 2.x declared many metrics it never filled in: GPU, ZFS, guests,
  SMART, IPMI and UPS were missing. They are real now.
- Some values were wrong and are fixed. `node_cpu_seconds_total` counted
  guest time twice. `node_filesystem_avail_bytes` was the same as free space.
  `node_filesystem_readonly` matched `errors=remount-ro`.
  `node_network_speed_bytes` was off by a factor of 8. `node_disk_io_now` was a
  queue setting, not I/Os in flight.
- Removed: `node_disk_utilization` (use `rate(node_disk_io_time_seconds_total[5m])`),
  and the top-process info metrics, which leaked process names.
  `node_exporter_*` / `node_scrape_*` self-metrics became
  `proxmox_exporter_collector_*`.
- The dashboards moved to new file names and select nodes by `instance`
  instead of a `node` label that the exporter never set.

[CHANGELOG.md](CHANGELOG.md) has the complete list.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| A collector shows `disabled` | `--list-collectors`; the tool or device it needs is missing, or the exporter is not root |
| `proxmox_exporter_collector_success` is 0 | `journalctl -u proxmox-node-exporter` has the error; reproduce with `proxmox-node-exporter --once --collectors NAME --log.level debug` |
| No temperatures | run `sensors-detect` and reboot, or load the driver (`coretemp`, `k10temp`, `nct6775`, `drivetemp`) |
| Disk shows `node_disk_smart_standby 1` | the disk is spun down and was deliberately not woken |
| No guest metrics in `--unprivileged` mode | `systemctl cat proxmox-node-exporter` shows the API URL; the token needs the PVEAuditor role |
| `node_filesystem_device_error 1` | statfs on that mount timed out or failed (dead NFS server, suspended pool) |

## Development

```sh
make dev      # .venv with the pinned, hash-checked tools
make check    # ruff, mypy --strict, shellcheck, actionlint, 670+ tests, generated files
make dist     # reproducible zipapp, tarball, wheel, SHA256SUMS
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the architecture and how to add a
collector. Releases are published by pushing a `vX.Y.Z` tag.

## License

[BSD 3-Clause](LICENSE)
