# Security

A monitoring agent on a hypervisor runs next to every guest's data, often as
root, and listens on the network. This page covers how the exporter keeps that
exposure small and how to lock it down further.

## Threat model in short

| Threat | Mitigation |
| --- | --- |
| Compromised dependency or package index | The exporter uses only the Python standard library, so nothing from PyPI runs on the host. CI tooling is pinned by hash. |
| Tampered download | The installer verifies the release tarball against the release's `SHA256SUMS` (or a hash you pin with `--sha256`). Releases have signed [build provenance](#verifying-releases), and the zipapp is byte-for-byte reproducible. |
| Anyone on the network reads metrics | Optional TLS (incl. mutual TLS) and basic auth; bind to a management address with `--web.listen-address`; firewall port 9101. |
| Denial of service against the endpoint | Scrapes never spawn processes (collection runs in the background on fixed intervals). Connections are capped (overall and per client address), every request has a hard deadline enforced by a watchdog (slowloris), requests have size limits, and failed logins are rate-limited per client. |
| Command injection via names from the system | No shell anywhere. Commands are resolved from a fixed root-owned `PATH`, bare names only. Values from tool output (device names, UPS names, node names) are validated before they reach an argv. Every command has a timeout, and the whole process group is killed when it expires. |
| A bug in the exporter being abused | systemd sandbox (below), and optionally no root at all ([unprivileged mode](#unprivileged-mode)). |
| Information leakage | No version banner in HTTP headers. The top-process metrics of 2.x, which exposed process names, are gone. Proxmox API tokens are read from a `0640` file, never from the command line or the environment. |

## The default mode: root, sandboxed

`pvesh`, `smartctl` and `ipmitool` need root, so by default the service runs
as root. The unit ([packaging/proxmox-node-exporter.service](../packaging/proxmox-node-exporter.service))
removes everything the exporter doesn't need:

- `/usr`, `/boot` and `/etc` are read-only, home directories are read-only,
  and `/tmp` is private;
- the service cannot load kernel modules, change kernel tunables or cgroups,
  read the kernel log, set the clock, reboot, use raw sockets or configure the
  network;
- the service cannot create namespaces, set SUID/SGID bits, gain privileges
  (`NoNewPrivileges`) or use real-time scheduling. System calls are limited to
  the `@system-service` set and address families to UNIX/IP/netlink;
- memory is capped at 512 MiB and tasks at 128.

`systemd-analyze security proxmox-node-exporter` rates this at about **4.4
("OK")**; an unhardened root service scores about 9.6. CI fails if a change
pushes the score above 5.0.

## Unprivileged mode

```sh
sudo bash install.sh --unprivileged
```

The installer then:

1. creates the system user `proxmox-exporter`;
2. creates the Proxmox user `proxmox-exporter@pve`, grants it the built-in
   read-only **PVEAuditor** role, and creates an API token for this node
   (`proxmox-exporter@pve!exporter-<node>`);
3. stores the token in `/etc/proxmox-node-exporter/pve-api-token`
   (`0640 root:proxmox-exporter`) and copies the cluster CA certificate, so the
   API connection to `https://127.0.0.1:8006` is fully verified;
4. adds a drop-in that runs the service as that user, with no capabilities
   and a fully read-only filesystem.

Guest, storage, cluster, replication and certificate data then come from the
API. The exporter never sends the token through a proxy and never follows
redirects. SMART, IPMI and container metrics need root and are not available
in this mode; everything else works. If the node uses a custom certificate
(ACME), the installer connects to the node's FQDN and verifies against the
system CA store instead.

To create a token by hand (e.g. with different permissions):

```sh
pveum user add monitoring@pve
pveum acl modify / --users monitoring@pve --roles PVEAuditor
pveum user token add monitoring@pve exporter --privsep 0
echo 'monitoring@pve!exporter=<secret>' > /etc/proxmox-node-exporter/pve-api-token
chmod 640 /etc/proxmox-node-exporter/pve-api-token
# ARGS="--pve.api-token-file=/etc/proxmox-node-exporter/pve-api-token --pve.api-ca-file=/etc/pve/pve-root-ca.pem"
```

Switch back with `install.sh --privileged`.

## TLS and basic auth

1. Create a password hash (it is PBKDF2-SHA256; the password itself is never
   stored):

   ```sh
   proxmox-node-exporter --hash-password
   ```

2. Write `/etc/proxmox-node-exporter/web.ini` (mode `0600`; see
   [packaging/web.ini.example](../packaging/web.ini.example)):

   ```ini
   [tls]
   # The node's own Proxmox certificate works; so does any other.
   cert_file = /etc/pve/local/pve-ssl.pem
   key_file = /etc/pve/local/pve-ssl.key
   # client_ca_file = /etc/proxmox-node-exporter/prometheus-ca.pem   # require client certs
   # min_version = TLSv1.3

   [basic_auth_users]
   prometheus = pbkdf2_sha256$200000$...
   ```

3. Enable it and restart:

   ```sh
   echo 'ARGS="--web.config-file=/etc/proxmox-node-exporter/web.ini"' > /etc/default/proxmox-node-exporter
   systemctl restart proxmox-node-exporter
   ```

4. In Prometheus:

   ```yaml
   - job_name: proxmox-nodes
     scheme: https
     tls_config:
       ca_file: /etc/prometheus/pve-root-ca.pem     # copy of /etc/pve/pve-root-ca.pem
     basic_auth:
       username: prometheus
       password_file: /etc/prometheus/proxmox-node-exporter.password
     static_configs:
       - targets: [pve1.example.com:9101]
   ```

Certificates are reloaded automatically when the files change, so renewals
need no restart. `/healthz` is always served without authentication; it only
says `ok` or `unhealthy`.

## Firewall

Even with authentication, allow port 9101 only from Prometheus. With the
Proxmox firewall, go to Datacenter → Firewall → Add: direction `in`, action
`ACCEPT`, protocol `tcp`, destination port `9101`, source = your Prometheus
server. Since the Proxmox firewall drops unmatched inbound traffic when
enabled, that rule is all you need.

## Automatic updates

`install.sh --auto-update` installs a weekly timer that runs `install.sh
--update`. Each update is checked against the release's SHA-256 and is rolled
back if the new version does not become healthy. Keep in mind that automatic
updates mean trusting future releases of this repository without reviewing
them. If you would rather review them, leave the timer off and pin releases
with `--version` and `--sha256` (the Ansible playbook supports both).

## Verifying releases

Every release asset has signed SLSA build provenance created by GitHub
Actions:

```sh
gh attestation verify proxmox-node-exporter-3.0.0.tar.gz --repo Lazarev-Cloud/proxmox-prometheus-exporter
```

Builds are reproducible. Check out the tag and run `python3 tools/build_pyz.py`,
and you get exactly the `proxmox-node-exporter.pyz` listed in `SHA256SUMS`.

## Reporting a vulnerability

See [SECURITY.md](../SECURITY.md).
