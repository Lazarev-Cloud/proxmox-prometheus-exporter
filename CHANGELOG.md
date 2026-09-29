# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/).

## [3.0.0]

A rewrite. The exporter is now a tested, stdlib-only Python package that ships
as a single reproducible file, and it actually collects every metric it
advertises.

### Added

- **Proxmox:** per-guest status, CPU, vCPUs, memory, disk, I/O, network,
  uptime and metadata (`pve_vm_*`); guest counts; storage capacity and state
  (`pve_storage_*`); cluster quorum, node counts and member state
  (`pve_cluster_*`); storage replication (`pve_replication_*`); TLS
  certificate expiry (`pve_certificate_expiry_timestamp_seconds`); version.
  Data comes from `pvesh` (root) or, in the new unprivileged mode, from the
  API with a read-only token.
- **ZFS:** ARC/L2ARC, pool health (numeric and per-state), capacity,
  fragmentation, deduplication, scrub/resilver state and progress, last
  scrub time, per-vdev and per-pool read/write/checksum errors, permanent data
  errors, dataset usage.
- **SMART:** health, temperature, power-on hours, power cycles,
  reallocated/pending/uncorrectable sectors, CRC errors, SSD/NVMe wear, NVMe
  spare, critical warnings, media errors and bytes written; disks in standby
  are not woken.
- **GPUs:** NVIDIA via `nvidia-smi`; AMD and Intel through DRM sysfs, with no
  vendor tools needed.
- IPMI sensors, NUT UPS status, mdadm, Btrfs, bonds, TCP states, pressure
  stall information, OOM kills, device-mapper names, CPU guest time,
  filesystem statfs errors, and exporter self-metrics
  (`proxmox_exporter_*`, `process_*`).
- **HTTP endpoint:** optional TLS (hot reload, optional mutual TLS, TLS 1.2+),
  basic auth with PBKDF2 hashes (`--hash-password`), `/healthz`, gzip,
  connection caps (overall and per client), per-request deadlines, and rate
  limiting of failed logins.
- **Installer:** SHA-256-verified release downloads and optional provenance
  verification; a hardened systemd unit; automatic rollback if an upgrade does
  not become healthy; idempotent re-runs; `--unprivileged` mode; opt-in weekly
  `--auto-update`; `--uninstall [--purge]`; migration from 2.x.
- 44 Prometheus alerting rules with `promtool` unit tests, a new "Proxmox
  Health" dashboard and an Ansible playbook.
- **CI:** ruff, mypy `--strict`, shellcheck, actionlint, tests on Python
  3.9–3.13, promtool, a systemd-unit security score gate, a reproducible-build
  check, and an end-to-end installer test on a real systemd host. CodeQL,
  Dependabot, and tag-driven releases with SLSA provenance.

### Fixed

- Most of the metrics documented in 2.x were declared but never collected:
  GPU, ZFS, guests, SMART, IPMI, UPS, Btrfs and CPU throttling.
- `node_cpu_seconds_total` counted guest time twice (as `user` and `guest`),
  inflating CPU usage on busy hypervisors. It is now a proper counter with
  node_exporter's labels (`cpu="0"`).
- `node_filesystem_avail_bytes` reported free space including root's
  reserve. `node_filesystem_readonly` matched `errors=remount-ro`.
- `node_network_speed_bytes` was in bits, not bytes. `node_disk_io_now`
  reported a queue size setting.
- mdadm parsing ignored spares, multi-flag members and delayed resyncs, and
  misattributed sync lines between arrays.
- systemd states were double-counted, and series for units that changed state
  were never removed.
- Duplicate sensor names (two NVMe drives, repeated labels) silently
  overwrote each other.
- The exporter no longer runs `pip install` at runtime and no longer dies with
  a traceback on a missing tool.

### Changed

- Configuration is by command line flags in
  `/etc/default/proxmox-node-exporter`. The 2.x environment variables
  `EXPORTER_PORT`, `COLLECTION_INTERVAL` and `DEBUG_MODE` still work.
- Installed to `/opt/proxmox-node-exporter/proxmox-node-exporter.pyz`. The
  service name (`proxmox-node-exporter`) and port (9101) are unchanged.
- Collection runs in the background on per-collector intervals; scrapes only
  serialise the latest results.
- The dashboards were renamed (`proxmox-overview`, `proxmox-cluster`,
  `proxmox-node`). They get a data source picker and select nodes by
  `instance`, where they used to select by a `node` label the exporter never
  set.
- hwmon series gained a `device` label, so identical chips are
  distinguishable. TCP states are lowercase. mdadm `device` labels are `md0`
  instead of `/dev/md0`. UPS voltages end in `_volts`.
  `node_network_udp_connections` became `node_network_udp_sockets`.

### Removed

- `node_disk_utilization`, which was not a utilisation. Use
  `rate(node_disk_io_time_seconds_total[5m])`.
- `node_top_cpu_processes_info` and `node_top_memory_processes_info`, which
  exposed process names and PIDs.
- `node_exporter_collection_*`, `node_scrape_collector_*`,
  `node_exporter_feature_enabled`, `node_features_info`,
  `node_exporter_info` and `node_kernel_version_info`. They are replaced by
  `proxmox_exporter_collector_*`, `proxmox_exporter_build_info` and the
  `kernel` label of `node_info`.
- Metrics that were never populated: `node_gpu_throttle_reasons`,
  `node_systemd_unit_start_time_seconds`,
  `node_systemd_timer_last_trigger_seconds`, `node_zfs_arc_evicted_bytes_total`,
  `node_md_sync_action_info`, `node_container_restarts_total`,
  `node_disk_smart_raw_read_error_rate` and `node_disk_smart_seek_error_rate`.
  The last two are vendor-encoded values with no comparable meaning.

## [2.0.0]

Single-file script with feature detection and three Grafana dashboards.
