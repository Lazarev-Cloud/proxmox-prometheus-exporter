#!/usr/bin/env bash
# End-to-end test of install.sh on a disposable systemd machine (CI runner VM
# or a privileged systemd container). DESTRUCTIVE: installs and removes a
# system service. Run as root from the repository root:
#
#   sudo bash tests/integration/installer.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVICE=proxmox-node-exporter
PASS=0

step() { printf '\n=== %s\n' "$*"; }
check() {
  local what="$1"; shift
  if "$@"; then PASS=$((PASS + 1)); printf '  ok: %s\n' "$what"; else printf '  FAIL: %s\n' "$what" >&2; exit 1; fi
}
metrics() { curl -fsS --max-time 5 http://127.0.0.1:9101/metrics; }
no_failed_collectors() { ! metrics | grep -E '^proxmox_exporter_collector_success\{.*\} 0$'; }
version() { /usr/local/sbin/proxmox-node-exporter --version | awk '{print $2}'; }
healthy() { curl -fsS --max-time 5 http://127.0.0.1:9101/healthz >/dev/null; }
# Not `metrics | grep -q`: grep would exit at the first match, and under
# pipefail curl's failed write of the rest of a large page fails the check.
has_metric() { local page; page="$(metrics)" && grep -q "^$1 " <<<"$page"; }
# /healthz only says the collectors are running; the first results may lag.
serves_metrics() {
  local tries=10
  until has_metric node_load1; do
    tries=$((tries - 1)); [ "$tries" -gt 0 ] || return 1; sleep 1
  done
}
hardened() { systemctl show "$SERVICE" -p ProtectSystem | grep -q full; }
low_exposure() {
  systemd-analyze security "$SERVICE" --no-pager | tail -n1 | grep -Eq 'level for .*: [0-4]\.'
}
loopback_only() { ss -ltn | grep -q '127\.0\.0\.1:9101'; }
service_gone() { ! systemctl cat "$SERVICE" >/dev/null 2>&1; }
files_gone() {
  ! ls "/opt/$SERVICE" "/etc/$SERVICE" "/etc/default/$SERVICE" "/usr/local/sbin/$SERVICE" 2>/dev/null
}

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }

step "fresh install from source, with legacy 2.x files present"
bash "$REPO_DIR/install.sh" --uninstall --purge --yes >/dev/null 2>&1 || true
mkdir -p /opt/proxmox-exporter/.venv
echo "# legacy" > /opt/proxmox-exporter/node_exporter.py
bash "$REPO_DIR/install.sh" --from-source --no-deps --no-sensors-detect --yes
check "service active" systemctl is-active --quiet "$SERVICE"
check "healthz" healthy
check "metrics served" serves_metrics
sleep 20  # let every collector run at least once (the slowest interval is 30s for some)
check "no failing collectors" no_failed_collectors
check "legacy files removed" test ! -e /opt/proxmox-exporter
check "defaults file created" test -f /etc/default/$SERVICE
check "hardening applied" hardened
check "exposure level acceptable" low_exposure

step "broken upgrade is rolled back"
BROKEN="$(mktemp -d)"
cp -r "$REPO_DIR/." "$BROKEN/"
sed -i 's/^    stop = threading.Event()/    raise SystemExit("simulated crash")\n&/' \
  "$BROKEN/src/proxmox_node_exporter/cli.py"
sed -i -E 's/^__version__ = "([0-9.]+)"/__version__ = "\1.post1"/' \
  "$BROKEN/src/proxmox_node_exporter/__init__.py"
before="$(version)"
if bash "$BROKEN/install.sh" --from-source --update --no-deps --yes; then
  echo "broken upgrade unexpectedly succeeded" >&2; exit 1
fi
rm -rf "$BROKEN"
check "previous version restored" test "$(version)" = "$before"
check "service active after rollback" systemctl is-active --quiet "$SERVICE"
check "healthz after rollback" healthy

step "re-running the installer is a no-op"
check "reports up to date" bash -c \
  "bash '$REPO_DIR/install.sh' --from-source --no-deps --yes | grep 'is up to date' >/dev/null"

step "custom listen address and auto-update timer"
sed -i 's/^ARGS=.*/ARGS="--web.listen-address=127.0.0.1:9101"/' /etc/default/$SERVICE
bash "$REPO_DIR/install.sh" --from-source --force --auto-update --no-deps --yes
check "timer enabled" systemctl is-enabled --quiet $SERVICE-update.timer
check "listening on loopback only" loopback_only

step "uninstall --purge"
bash "$REPO_DIR/install.sh" --uninstall --purge --yes
check "service removed" service_gone
check "files removed" files_gone

printf '\nAll %d installer checks passed.\n' "$PASS"
