#!/usr/bin/env bash
# install-proxmox-exporter.sh — по уму
# - apt-пакеты сначала (python3-prometheus-client, python3-psutil)
# - если их нет в дистре — venv в /opt/proxmox-exporter/.venv
# - без pip в system-wide (PEP 668 не триггерим)
# - systemd unit с правильным интерпретатором
# - кэш-бастинг при скачивании файла из MinIO/HTTP

set -euo pipefail

# -------- CONFIG --------
INSTALL_DIR="/opt/proxmox-exporter"
PY_SCRIPT="${INSTALL_DIR}/node_exporter.py"
VENV_DIR="${INSTALL_DIR}/.venv"
SERVICE_NAME="proxmox-node-exporter.service"

# URL до python-скрипта экспортёра.
# Можно переопределить: EXPORTER_URL="https://..." ./install-proxmox-exporter.sh
EXPORTER_URL="${EXPORTER_URL:-https://c7.lazarev.cloud/api/v1/buckets/public/objects/download?prefix=scripts%2Fproxmox%2Fproxmox-node-exporter.py}"

# Если надо скачать приватный объект MinIO Console — можно передать куку:
#   AUTH_COOKIE='Cookie: <...>' ./install-proxmox-exporter.sh
AUTH_HEADER="${AUTH_COOKIE:-}"

# -------- UX --------
G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; N='\033[0m'
msg() { echo -e "${Y}[*]${N} $*"; }
ok()  { echo -e "${G}[✓]${N} $*"; }
die() { echo -e "${R}[x]${N} $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Запусти от root. Иначе sensors и systemd будут ворчать."

export DEBIAN_FRONTEND=noninteractive

# -------- APT DEPS --------
msg "Installing OS deps…"
apt update -y
apt install -y --no-install-recommends \
  python3 python3-venv curl ca-certificates \
  lm-sensors sysstat smartmontools nvme-cli

# -------- PYTHON DEPS: apt -> venv --------
PYBIN="/usr/bin/python3"

need_py=false
$PYBIN - <<'PY' 2>/dev/null || need_py=true
import sys
import importlib
for m in ("prometheus_client","psutil"):
    importlib.import_module(m)
PY

if $need_py; then
  if apt-cache show python3-prometheus-client >/dev/null 2>&1 && \
     apt-cache show python3-psutil >/dev/null 2>&1; then
    msg "Installing python libs via apt…"
    apt install -y python3-prometheus-client python3-psutil
    PYBIN="/usr/bin/python3"
  else
    msg "Apt packages not found, building venv…"
    mkdir -p "${VENV_DIR}"
    $PYBIN -m venv "${VENV_DIR}"
    "${VENV_DIR}/bin/pip" install -U pip
    "${VENV_DIR}/bin/pip" install prometheus-client psutil
    PYBIN="${VENV_DIR}/bin/python3"
    ok "Virtualenv ready: ${PYBIN}"
  fi
else
  ok "Python libs already present."
fi

# -------- SENSORS SETUP --------
msg "Detecting sensors…"
yes | sensors-detect --auto >/dev/null 2>&1 || true
modprobe coretemp 2>/dev/null || true
modprobe nct6775 2>/dev/null || true
grep -q '^coretemp$' /etc/modules 2>/dev/null || echo coretemp >> /etc/modules || true

# -------- FETCH EXPORTER --------
msg "Fetching exporter script…"
mkdir -p "${INSTALL_DIR}"

curl_args=( -fsSL --retry 3 --retry-connrefused --retry-delay 2 -H 'Cache-Control: no-cache' )
[ -n "${AUTH_HEADER}" ] && curl_args+=( -H "${AUTH_HEADER}" )

curl "${curl_args[@]}" "${EXPORTER_URL}&ts=$(date +%s)" -o "${PY_SCRIPT}" \
  || die "Download failed. Для приватных объектов используй presign или AUTH_COOKIE."

# защитимся от HTML-ответов
if head -n1 "${PY_SCRIPT}" | grep -qi '<!DOCTYPE html>'; then
  rm -f "${PY_SCRIPT}"
  die "MinIO вернул HTML (скорее всего 401/403). Сгенерируй presigned URL и передай через EXPORTER_URL."
fi

chmod +x "${PY_SCRIPT}"
ok "Saved: ${PY_SCRIPT}"

# -------- SYSTEMD UNIT --------
msg "Writing systemd unit…"
cat > "/etc/systemd/system/${SERVICE_NAME}" <<EOF
[Unit]
Description=Proxmox Node Exporter for Prometheus
After=network-online.target
Wants=network-online.target lm-sensors.service

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${PYBIN} ${PY_SCRIPT}
Restart=always
RestartSec=10

# Hardening (ослабь при необходимости)
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=${INSTALL_DIR}
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}" >/dev/null
systemctl restart "${SERVICE_NAME}"

sleep 2
if systemctl is-active --quiet "${SERVICE_NAME}"; then
  ok "Service is running."
else
  journalctl -u "${SERVICE_NAME}" -b --no-pager | tail -n 80 >&2
  die "Service failed to start."
fi

# -------- VERIFY --------
msg "Probing metrics…"
IP="$(hostname -I | awk '{print $1}')"
if curl -fsS "http://127.0.0.1:9101/metrics" >/dev/null; then
  ok "Metrics: http://${IP}:9101/metrics"
else
  die "No metrics on 9101. Проверь firewall/порт/логи."
fi
