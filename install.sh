#!/usr/bin/env bash
# Install, update or remove proxmox-node-exporter on a Proxmox VE / Debian host.
#
# Recommended: download, read, then run it.
#
#   curl -fsSLO https://github.com/Lazarev-Cloud/proxmox-prometheus-exporter/releases/latest/download/install.sh
#   less install.sh
#   sudo bash install.sh                 # or: sudo bash install.sh --help
#
# What it does:
#   * downloads the release tarball from GitHub and verifies its SHA-256
#     against the release's SHA256SUMS (or installs from a local checkout);
#   * installs the single-file exporter to /opt/proxmox-node-exporter and a
#     sandboxed systemd unit; nothing is fetched from PyPI, the exporter only
#     needs the system python3;
#   * checks that the new version answers on /healthz and rolls back to the
#     previous version automatically if it does not.

set -euo pipefail
umask 022

REPO="Lazarev-Cloud/proxmox-prometheus-exporter"
NAME="proxmox-node-exporter"
INSTALL_DIR="/opt/${NAME}"
PYZ="${INSTALL_DIR}/${NAME}.pyz"
WRAPPER="/usr/local/sbin/${NAME}"
CONFIG_DIR="/etc/${NAME}"
DEFAULTS_FILE="/etc/default/${NAME}"
UNIT_DIR="/etc/systemd/system"
SERVICE="${NAME}.service"
UPDATE_SERVICE="${NAME}-update.service"
UPDATE_TIMER="${NAME}-update.timer"
DROPIN_DIR="${UNIT_DIR}/${SERVICE}.d"
DROPIN="${DROPIN_DIR}/10-unprivileged.conf"
SERVICE_USER="proxmox-exporter"
PVE_USER="proxmox-exporter@pve"
TOKEN_FILE="${CONFIG_DIR}/pve-api-token"
LEGACY_DIR="/opt/proxmox-exporter"
PYTHON="/usr/bin/python3"

ACTION="install"
VERSION=""
ARCHIVE=""
EXPECTED_SHA256=""
FROM_SOURCE=0
LISTEN=""
WEB_CONFIG=""
MODE=""          # "", unprivileged, privileged
AUTO_UPDATE=""   # "", on, off
INSTALL_DEPS=1
SENSORS_DETECT=1
PURGE=0
ASSUME_YES=0
FORCE=0
VERIFY_ATTESTATION=0
WORK=""

if [ -t 1 ]; then
  C_OK=$'\033[0;32m' C_WARN=$'\033[1;33m' C_ERR=$'\033[0;31m' C_OFF=$'\033[0m'
else
  C_OK="" C_WARN="" C_ERR="" C_OFF=""
fi
msg()  { printf '%s[*]%s %s\n' "$C_WARN" "$C_OFF" "$*"; }
ok()   { printf '%s[+]%s %s\n' "$C_OK" "$C_OFF" "$*"; }
warn() { printf '%s[!]%s %s\n' "$C_WARN" "$C_OFF" "$*" >&2; }
die()  { printf '%s[x]%s %s\n' "$C_ERR" "$C_OFF" "$*" >&2; exit 1; }

usage() {
  cat <<EOF
Usage: install.sh [options]

Actions (default: install or upgrade):
  --update               Upgrade an existing installation to the latest (or
                         --version) release; does nothing if already current.
  --uninstall            Remove the exporter (keeps configuration).
  --uninstall --purge    Also remove configuration, the service user and the
                         Proxmox API token created by --unprivileged.

Source (default: latest GitHub release, SHA-256 verified):
  --version vX.Y.Z       Install this release instead of the latest.
  --archive FILE         Install from a downloaded release tarball
                         (requires --sha256).
  --sha256 HASH          Expected SHA-256 of the release tarball (pins the
                         download; required with --archive).
  --from-source          Install from the checkout this script lives in.
  --verify-attestation   Also verify the GitHub build provenance of the
                         tarball (needs the gh CLI, authenticated).

Configuration (applied on first install; later edit ${DEFAULTS_FILE}):
  --listen ADDR          Listen address, e.g. 10.0.0.5:9101 (default :9101).
  --web-config FILE      Enable TLS/basic auth with this web config file.
  --unprivileged         Run as user '${SERVICE_USER}' without capabilities and
                         read guest data through a read-only Proxmox API token
                         (created automatically). SMART/IPMI are unavailable.
  --privileged           Switch back to the default mode (root, sandboxed).
  --auto-update          Enable a weekly timer that runs --update.
  --no-auto-update       Disable that timer.
  --no-deps              Do not install packages with apt.
  --no-sensors-detect    Do not run sensors-detect when no sensors are found.

  -y, --yes              Do not ask for confirmation.
  --force                Reinstall even if the version is unchanged.
  -h, --help             Show this help.
EOF
}

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --update) ACTION="update" ;;
      --uninstall) ACTION="uninstall" ;;
      --purge) PURGE=1 ;;
      --version) VERSION="${2:?--version needs a value}"; shift ;;
      --version=*) VERSION="${1#*=}" ;;
      --archive) ARCHIVE="${2:?--archive needs a value}"; shift ;;
      --archive=*) ARCHIVE="${1#*=}" ;;
      --sha256) EXPECTED_SHA256="${2:?--sha256 needs a value}"; shift ;;
      --sha256=*) EXPECTED_SHA256="${1#*=}" ;;
      --from-source) FROM_SOURCE=1 ;;
      --verify-attestation) VERIFY_ATTESTATION=1 ;;
      --listen) LISTEN="${2:?--listen needs a value}"; shift ;;
      --listen=*) LISTEN="${1#*=}" ;;
      --web-config) WEB_CONFIG="${2:?--web-config needs a value}"; shift ;;
      --web-config=*) WEB_CONFIG="${1#*=}" ;;
      --unprivileged) MODE="unprivileged" ;;
      --privileged) MODE="privileged" ;;
      --auto-update) AUTO_UPDATE="on" ;;
      --no-auto-update) AUTO_UPDATE="off" ;;
      --no-deps) INSTALL_DEPS=0 ;;
      --no-sensors-detect) SENSORS_DETECT=0 ;;
      -y|--yes) ASSUME_YES=1 ;;
      --force) FORCE=1 ;;
      -h|--help) usage; exit 0 ;;
      *) usage >&2; die "unknown option: $1" ;;
    esac
    shift
  done

  if [ -n "$VERSION" ] && ! [[ "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]]; then
    die "--version must look like v3.0.0"
  fi
  if [ -n "$EXPECTED_SHA256" ] && ! [[ "$EXPECTED_SHA256" =~ ^[0-9a-fA-F]{64}$ ]]; then
    die "--sha256 must be 64 hex characters"
  fi
  if [ -n "$ARCHIVE" ] && [ -z "$EXPECTED_SHA256" ]; then
    die "--archive requires --sha256 (copy it from the release's SHA256SUMS)"
  fi
  if [ -n "$LISTEN" ] && ! [[ "$LISTEN" =~ ^(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]*):[0-9]{1,5}$ ]]; then
    die "--listen must look like :9101, 10.0.0.5:9101 or [::1]:9101"
  fi
  if [ -n "$WEB_CONFIG" ] && ! [[ "$WEB_CONFIG" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
    die "--web-config must be an absolute path without spaces"
  fi
  if [ "$PURGE" = 1 ] && [ "$ACTION" != "uninstall" ]; then
    die "--purge is only valid with --uninstall"
  fi
}

confirm() {
  [ "$ASSUME_YES" = 1 ] && return 0
  if [ ! -t 0 ]; then
    die "refusing to $1 without confirmation; re-run with --yes"
  fi
  local answer
  read -r -p "$1? [y/N] " answer
  [[ "$answer" =~ ^[Yy] ]] || die "aborted"
}

cleanup() {
  if [ -n "$WORK" ] && [ -d "$WORK" ]; then
    rm -rf "$WORK"
  fi
}

preflight() {
  [ "$(id -u)" -eq 0 ] || die "run as root (sudo bash install.sh)"
  [ "$(uname -s)" = "Linux" ] || die "Linux only"
  [ -d /run/systemd/system ] || die "systemd is required"
  if [ ! -d /etc/pve ]; then
    warn "/etc/pve not found: this does not look like a Proxmox VE host; Proxmox metrics will be off"
  fi
}

is_installed_pkg() {
  dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"
}

install_deps() {
  [ "$INSTALL_DEPS" = 1 ] || return 0
  if ! command -v apt-get >/dev/null 2>&1; then
    warn "apt-get not found; make sure python3 (>= 3.9) and curl are installed"
    return 0
  fi
  local wanted=(python3 curl ca-certificates smartmontools lm-sensors) missing=() pkg
  if [ -e /dev/ipmi0 ] || [ -e /dev/ipmi/0 ] || [ -e /dev/ipmidev/0 ]; then
    wanted+=(ipmitool)
  fi
  for pkg in "${wanted[@]}"; do
    is_installed_pkg "$pkg" || missing+=("$pkg")
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    msg "Installing packages: ${missing[*]}"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends "${missing[@]}" >/dev/null
  fi
}

check_python() {
  [ -x "$PYTHON" ] || die "$PYTHON not found (apt-get install python3)"
  "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 9))' \
    || die "python3 >= 3.9 is required (found $("$PYTHON" -V 2>&1))"
}

setup_sensors() {
  [ "$SENSORS_DETECT" = 1 ] || return 0
  command -v sensors-detect >/dev/null 2>&1 || return 0
  if compgen -G "/sys/class/hwmon/hwmon*/temp*_input" >/dev/null; then
    return 0  # the kernel already exposes temperature sensors
  fi
  msg "No hardware sensors visible; running sensors-detect --auto"
  sensors-detect --auto >/dev/null 2>&1 || true
  systemctl restart systemd-modules-load.service >/dev/null 2>&1 || true
}

sha256_of() {
  sha256sum "$1" | awk '{print $1}'
}

verify_sha256() {
  local file="$1" expected actual
  expected="$(printf '%s' "$2" | tr 'A-F' 'a-f')"
  actual="$(sha256_of "$file")"
  [ "$actual" = "$expected" ] || die "SHA-256 mismatch for $(basename "$file"): expected $expected, got $actual"
}

download() {
  curl --proto '=https' --tlsv1.2 -fsSL --retry 3 --retry-delay 2 --connect-timeout 15 \
    -o "$2" "$1" || die "download failed: $1"
}

resolve_latest() {
  local url
  command -v curl >/dev/null 2>&1 || die "curl is required"
  url="$(curl --proto '=https' --tlsv1.2 -fsSLI -o /dev/null -w '%{url_effective}' \
    --connect-timeout 15 "https://github.com/${REPO}/releases/latest")" \
    || die "cannot reach GitHub to find the latest release"
  VERSION="${url##*/}"
  [[ "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+ ]] || die "could not determine the latest release (got '$VERSION')"
}

# Sets SRC (directory with the release contents), NEW_PYZ and NEW_VERSION.
fetch() {
  local here tarball sums expected
  WORK="$(mktemp -d)"
  here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"

  if [ "$FROM_SOURCE" = 1 ]; then
    [ -f "$here/tools/build_pyz.py" ] || die "--from-source: run install.sh from a repository checkout"
    msg "Building from source checkout $here"
    SRC="$here"
    NEW_PYZ="$WORK/${NAME}.pyz"
    "$PYTHON" "$here/tools/build_pyz.py" "$NEW_PYZ" >/dev/null
  else
    if [ -n "$ARCHIVE" ]; then
      [ -f "$ARCHIVE" ] || die "no such file: $ARCHIVE"
      tarball="$WORK/$(basename "$ARCHIVE")"
      cp "$ARCHIVE" "$tarball"
      verify_sha256 "$tarball" "$EXPECTED_SHA256"
    else
      [ -n "$VERSION" ] || resolve_latest
      local base="https://github.com/${REPO}/releases/download/${VERSION}"
      tarball="$WORK/${NAME}-${VERSION#v}.tar.gz"
      sums="$WORK/SHA256SUMS"
      msg "Downloading ${NAME} ${VERSION}"
      download "$base/SHA256SUMS" "$sums"
      download "$base/$(basename "$tarball")" "$tarball"
      expected="$(awk -v f="$(basename "$tarball")" '$2 == f || $2 == "*"f {print $1}' "$sums")"
      [ -n "$expected" ] || die "$(basename "$tarball") is not listed in SHA256SUMS"
      verify_sha256 "$tarball" "$expected"
      if [ -n "$EXPECTED_SHA256" ]; then
        verify_sha256 "$tarball" "$EXPECTED_SHA256"
      fi
    fi
    ok "Checksum verified: $(sha256_of "$tarball")"
    if [ "$VERIFY_ATTESTATION" = 1 ]; then
      command -v gh >/dev/null 2>&1 || die "--verify-attestation needs the gh CLI"
      gh attestation verify "$tarball" --repo "$REPO" >/dev/null \
        || die "build provenance verification failed"
      ok "Build provenance verified"
    fi
    tar -xzf "$tarball" -C "$WORK" --no-same-owner --no-same-permissions
    SRC="$(find "$WORK" -mindepth 1 -maxdepth 1 -type d -name "${NAME}-*" | head -n1)"
    [ -n "$SRC" ] && [ -f "$SRC/${NAME}.pyz" ] || die "unexpected tarball layout"
    NEW_PYZ="$SRC/${NAME}.pyz"
  fi
  [ -f "$SRC/packaging/${SERVICE}" ] || die "packaging files missing from $SRC"
  NEW_VERSION="$("$PYTHON" -I "$NEW_PYZ" --version | awk '{print $2}')" \
    || die "the new exporter does not run with $PYTHON"
}

installed_version() {
  if [ -f "$PYZ" ]; then
    "$PYTHON" -I "$PYZ" --version 2>/dev/null | awk '{print $2}'
  fi
}

# Files replaced on upgrade; the previous copies are kept until the new
# version is confirmed healthy.
backup_targets() {
  printf '%s\n' "$PYZ" "${INSTALL_DIR}/install.sh" "${UNIT_DIR}/${SERVICE}"
}

install_files() {
  local f
  install -d -m 0755 "$INSTALL_DIR"
  install -m 0755 "$NEW_PYZ" "${INSTALL_DIR}/.${NAME}.pyz.new"
  while read -r f; do
    if [ -f "$f" ]; then cp -p "$f" "${f}.prev"; fi
  done < <(backup_targets)
  mv -f "${INSTALL_DIR}/.${NAME}.pyz.new" "$PYZ"
  if [ -f "$SRC/install.sh" ] && [ "$(realpath "$SRC/install.sh")" != "${INSTALL_DIR}/install.sh" ]; then
    install -m 0755 "$SRC/install.sh" "${INSTALL_DIR}/install.sh"
  fi
  cat > "$WRAPPER" <<EOF
#!/bin/sh
exec ${PYTHON} -I ${PYZ} "\$@"
EOF
  chmod 0755 "$WRAPPER"

  install -d -m 0750 "$CONFIG_DIR"
  install -m 0644 "$SRC/packaging/web.ini.example" "${CONFIG_DIR}/web.ini.example"
  install -m 0644 "$SRC/packaging/${SERVICE}" "${UNIT_DIR}/${SERVICE}"
}

write_defaults() {
  local args=()
  [ -n "$LISTEN" ] && args+=("--web.listen-address=${LISTEN}")
  if [ -n "$WEB_CONFIG" ]; then
    [ -f "$WEB_CONFIG" ] || warn "$WEB_CONFIG does not exist yet; create it before the service starts"
    args+=("--web.config-file=${WEB_CONFIG}")
  fi
  if [ ! -f "$DEFAULTS_FILE" ]; then
    sed "s|^ARGS=\"\"$|ARGS=\"${args[*]}\"|" "$SRC/packaging/${NAME}.default" > "$DEFAULTS_FILE"
    chmod 0644 "$DEFAULTS_FILE"
  elif [ "${#args[@]}" -gt 0 ]; then
    if grep -qx 'ARGS=""' "$DEFAULTS_FILE"; then
      sed -i "s|^ARGS=\"\"$|ARGS=\"${args[*]}\"|" "$DEFAULTS_FILE"
    else
      warn "$DEFAULTS_FILE already has options; add ${args[*]} to ARGS yourself"
    fi
  fi
}

setup_unprivileged() {
  command -v pveum >/dev/null 2>&1 || die "--unprivileged needs a Proxmox VE host (pveum not found)"
  local node token_id json full secret api_url ca_arg=""
  node="$(hostname -s)"
  token_id="exporter-${node//[^A-Za-z0-9-]/-}"

  if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --user-group --no-create-home --home-dir /nonexistent \
      --shell /usr/sbin/nologin "$SERVICE_USER"
  fi
  chgrp "$SERVICE_USER" "$CONFIG_DIR"

  if ! pveum user list --output-format json | "$PYTHON" -c \
      'import json,sys; sys.exit(not any(u.get("userid")==sys.argv[1] for u in json.load(sys.stdin)))' \
      "$PVE_USER"; then
    pveum user add "$PVE_USER" --comment "Prometheus exporter (read-only)"
  fi
  pveum acl modify / --users "$PVE_USER" --roles PVEAuditor

  if [ ! -s "$TOKEN_FILE" ]; then
    msg "Creating read-only API token ${PVE_USER}!${token_id}"
    pveum user token remove "$PVE_USER" "$token_id" >/dev/null 2>&1 || true
    json="$(pveum user token add "$PVE_USER" "$token_id" --privsep 0 \
      --comment "proxmox-node-exporter on ${node}" --output-format json)"
    full="$(printf '%s' "$json" | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["full-tokenid"])')"
    secret="$(printf '%s' "$json" | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["value"])')"
    ( umask 077; printf '%s=%s\n' "$full" "$secret" > "${TOKEN_FILE}.new" )
    chown "root:${SERVICE_USER}" "${TOKEN_FILE}.new"
    chmod 0640 "${TOKEN_FILE}.new"
    mv -f "${TOKEN_FILE}.new" "$TOKEN_FILE"
  fi

  api_url="https://127.0.0.1:8006"
  if [ -f /etc/pve/local/pveproxy-ssl.pem ]; then
    # A custom certificate (e.g. ACME) is issued for the host name, not 127.0.0.1.
    api_url="https://$(hostname -f):8006"
  else
    install -m 0644 /etc/pve/pve-root-ca.pem "${CONFIG_DIR}/pve-root-ca.pem"
    ca_arg=" --pve.api-ca-file=${CONFIG_DIR}/pve-root-ca.pem"
  fi

  install -d -m 0755 "$DROPIN_DIR"
  {
    cat "$SRC/packaging/unprivileged.conf"
    printf 'Environment="MODE_ARGS=--pve.api-token-file=%s --pve.api-url=%s%s"\n' \
      "$TOKEN_FILE" "$api_url" "$ca_arg"
  } > "$DROPIN"
  chmod 0644 "$DROPIN"
  ok "Unprivileged mode configured (user ${SERVICE_USER}, token ${PVE_USER}!${token_id})"
}

setup_mode() {
  case "$MODE" in
    unprivileged) setup_unprivileged ;;
    privileged)
      if [ -f "$DROPIN" ]; then
        rm -f "$DROPIN"
        rmdir "$DROPIN_DIR" 2>/dev/null || true
        ok "Switched back to the default (root, sandboxed) mode"
      fi
      ;;
  esac
}

setup_auto_update() {
  local installed=0
  [ -f "${UNIT_DIR}/${UPDATE_TIMER}" ] && installed=1
  if [ "$AUTO_UPDATE" = "on" ] || { [ -z "$AUTO_UPDATE" ] && [ "$installed" = 1 ]; }; then
    install -m 0644 "$SRC/packaging/${UPDATE_SERVICE}" "${UNIT_DIR}/${UPDATE_SERVICE}"
    install -m 0644 "$SRC/packaging/${UPDATE_TIMER}" "${UNIT_DIR}/${UPDATE_TIMER}"
    systemctl daemon-reload
    systemctl enable --now "$UPDATE_TIMER" >/dev/null 2>&1
    [ "$AUTO_UPDATE" = "on" ] && ok "Weekly automatic updates enabled (${UPDATE_TIMER})"
  elif [ "$AUTO_UPDATE" = "off" ] && [ "$installed" = 1 ]; then
    systemctl disable --now "$UPDATE_TIMER" >/dev/null 2>&1 || true
    rm -f "${UNIT_DIR}/${UPDATE_SERVICE}" "${UNIT_DIR}/${UPDATE_TIMER}"
    ok "Automatic updates disabled"
  fi
  return 0
}

listen_address() {
  local addr=""
  if [ -f "$DEFAULTS_FILE" ]; then
    addr="$(grep -E '^ARGS=' "$DEFAULTS_FILE" | grep -oE -- '--web\.listen-address[= ][^ "]+' \
      | tail -n1 | sed -E 's/^--web\.listen-address[= ]//')" || true
  fi
  printf '%s' "${addr:-:9101}"
}

probe() {
  local addr host port
  addr="$(listen_address)"
  host="${addr%:*}"
  port="${addr##*:}"
  case "$host" in ""|0.0.0.0|"[::]") host="127.0.0.1" ;; esac
  # Plain HTTP first, then HTTPS without verification (this is a liveness
  # check against our own local process, not a trust decision).
  "$PYTHON" - "$host" "$port" <<'PY'
import ssl
import sys
import urllib.request

host, port = sys.argv[1], sys.argv[2]
insecure = ssl.create_default_context()
insecure.check_hostname = False
insecure.verify_mode = ssl.CERT_NONE
for scheme, handler in (
    ("http", urllib.request.HTTPHandler()),
    ("https", urllib.request.HTTPSHandler(context=insecure)),
):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), handler)
    try:
        with opener.open(f"{scheme}://{host}:{port}/healthz", timeout=3) as response:
            if response.status == 200:
                sys.exit(0)
    except Exception:
        pass
sys.exit(1)
PY
}

start_and_verify() {
  local i
  systemctl daemon-reload
  systemctl enable "$SERVICE" >/dev/null 2>&1
  systemctl restart "$SERVICE"
  for i in $(seq 1 20); do
    sleep 1
    if systemctl is-active --quiet "$SERVICE" && probe; then
      return 0
    fi
    [ "$i" -ge 5 ] && ! systemctl is-active --quiet "$SERVICE" && break
  done
  return 1
}

rollback_or_die() {
  local f
  journalctl -u "$SERVICE" -n 30 --no-pager >&2 || true
  if [ -f "${PYZ}.prev" ]; then
    warn "The new version did not come up; rolling back"
    while read -r f; do
      if [ -f "${f}.prev" ]; then mv -f "${f}.prev" "$f"; fi
    done < <(backup_targets)
    systemctl daemon-reload
    systemctl restart "$SERVICE" || true
    die "installation failed; the previous version ($(installed_version)) was restored"
  fi
  die "the exporter did not start; see the log above"
}

migrate_legacy() {
  # Files left by the 2.x script-based installer.
  if [ -f "${LEGACY_DIR}/node_exporter.py" ] || [ -d "${LEGACY_DIR}/.venv" ]; then
    rm -f "${LEGACY_DIR}/node_exporter.py"
    rm -rf "${LEGACY_DIR}/.venv"
    rmdir "$LEGACY_DIR" 2>/dev/null || true
    ok "Removed the old 2.x installation from ${LEGACY_DIR}"
  fi
}

verify_unprivileged_access() {
  [ -f "$DROPIN" ] || return 0
  local args
  args="$(sed -n 's/^Environment="MODE_ARGS=\(.*\)"$/\1/p' "$DROPIN")"
  # shellcheck disable=SC2086 # word splitting of the stored arguments is intended
  if ! runuser -u "$SERVICE_USER" -- "$PYTHON" -I "$PYZ" --once --collectors pve $args >/dev/null 2>&1; then
    warn "The exporter cannot read the Proxmox API as ${SERVICE_USER}; check $TOKEN_FILE and"
    warn "the API URL in $DROPIN (journalctl -u ${SERVICE} shows the error)"
  fi
}

do_install() {
  local current f
  current="$(installed_version || true)"
  install_deps
  check_python
  fetch
  if [ "$ACTION" = "update" ] && [ -z "$current" ]; then
    die "${NAME} is not installed; run install.sh without --update"
  fi
  # Re-running with the installed version and no new settings changes nothing.
  if [ "$current" = "$NEW_VERSION" ] && [ "$FORCE" = 0 ] \
      && [ -z "${MODE}${AUTO_UPDATE}${LISTEN}${WEB_CONFIG}" ]; then
    systemctl enable --now "$SERVICE" >/dev/null 2>&1 || true
    ok "${NAME} ${current} is up to date"
    return 0
  fi
  [ "$ACTION" = "install" ] && setup_sensors

  install_files
  [ "$ACTION" = "install" ] && write_defaults
  setup_mode
  setup_auto_update
  if ! start_and_verify; then
    rollback_or_die
  fi
  while read -r f; do rm -f "${f}.prev"; done < <(backup_targets)
  migrate_legacy
  verify_unprivileged_access

  local addr
  addr="$(listen_address)"
  if [ -n "$current" ] && [ "$current" != "$NEW_VERSION" ]; then
    ok "Upgraded ${NAME} ${current} -> ${NEW_VERSION}"
  else
    ok "${NAME} ${NEW_VERSION} is running"
  fi
  ok "Metrics: http(s)://$(hostname -f 2>/dev/null || hostname):${addr##*:}/metrics"
  msg "Configuration: ${DEFAULTS_FILE}; logs: journalctl -u ${SERVICE}"
}

do_uninstall() {
  confirm "remove ${NAME}$([ "$PURGE" = 1 ] && printf ' and its configuration')"
  systemctl disable --now "$SERVICE" "$UPDATE_TIMER" >/dev/null 2>&1 || true
  rm -f "${UNIT_DIR}/${SERVICE}" "${UNIT_DIR}/${UPDATE_SERVICE}" "${UNIT_DIR}/${UPDATE_TIMER}"
  rm -rf "$DROPIN_DIR" "$INSTALL_DIR"
  rm -f "$WRAPPER"
  systemctl daemon-reload
  if [ "$PURGE" = 1 ]; then
    if command -v pveum >/dev/null 2>&1; then
      local node
      node="$(hostname -s)"
      pveum user token remove "$PVE_USER" "exporter-${node//[^A-Za-z0-9-]/-}" >/dev/null 2>&1 || true
    fi
    rm -rf "$CONFIG_DIR"
    rm -f "$DEFAULTS_FILE"
    if id -u "$SERVICE_USER" >/dev/null 2>&1; then
      userdel "$SERVICE_USER" 2>/dev/null || true
    fi
    ok "Removed ${NAME} and its configuration"
    if command -v pveum >/dev/null 2>&1; then
      msg "The Proxmox user ${PVE_USER} (shared by all nodes) was kept: pveum user delete ${PVE_USER}"
    fi
  else
    ok "Removed ${NAME}; configuration kept in ${DEFAULTS_FILE} and ${CONFIG_DIR}"
  fi
}

main() {
  parse_args "$@"
  preflight
  trap cleanup EXIT
  case "$ACTION" in
    install|update) do_install ;;
    uninstall) do_uninstall ;;
  esac
}

main "$@"
