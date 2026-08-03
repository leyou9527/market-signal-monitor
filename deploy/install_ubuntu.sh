#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/market-signal-monitor"
SERVICE_FILE="/etc/systemd/system/market-signal-monitor.service"
TIMER_FILE="/etc/systemd/system/market-signal-monitor.timer"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run this script as root."
  exit 1
fi

if [[ ! -d "${APP_DIR}" ]]; then
  echo "Expected application directory at ${APP_DIR}"
  echo "Copy the project there first, then rerun this script."
  exit 1
fi

apt-get update
apt-get install -y python3 python3-venv python3-pip fonts-noto-cjk

cd "${APP_DIR}"
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

cp deploy/market-signal-monitor.service "${SERVICE_FILE}"
cp deploy/market-signal-monitor.timer "${TIMER_FILE}"

systemctl daemon-reload
systemctl enable --now market-signal-monitor.timer
systemctl restart market-signal-monitor.timer

echo "Market Signal Monitor timer is enabled."
echo "Check status with:"
echo "  systemctl status market-signal-monitor.timer"
echo "  systemctl list-timers market-signal-monitor.timer"
