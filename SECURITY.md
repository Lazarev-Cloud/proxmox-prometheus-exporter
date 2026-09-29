# Security policy

## Supported versions

Only the latest release gets security fixes. Upgrade with
`install.sh --update`, or enable `--auto-update`.

## Reporting a vulnerability

Please **do not open a public issue**. Report it privately through
[GitHub security advisories](https://github.com/Lazarev-Cloud/proxmox-prometheus-exporter/security/advisories/new).
Include the version, how to reproduce it and the impact you expect. You will
get an acknowledgement within a few days. The fix is released as soon as
possible, and the advisory is published once users have had a chance to
update.

## Design

[docs/security.md](docs/security.md) describes the threat model, the systemd
sandbox, the unprivileged mode, TLS/basic auth and how to verify releases.
