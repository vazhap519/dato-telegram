#!/usr/bin/env bash
# One-time installer: sudo bash /opt/dato-telegram/scripts/install_reliability.sh
set -euo pipefail

APP_DIR="${DATO_APP_DIR:-/opt/dato-telegram}"
SCRIPT="$APP_DIR/scripts/reliability.py"
CONF="/etc/dato-reliability.conf"
BACKUPS="${DATO_BACKUP_DIR:-/var/backups/dato-telegram}"
STATE="/var/lib/dato-reliability"

if [[ "$EUID" -ne 0 ]]; then
  echo "Run as root: sudo bash $SCRIPT_DIR/install_reliability.sh" >&2
  exit 1
fi
for executable in python3 pg_dump pg_restore pg_isready systemctl; do
  if ! command -v "$executable" >/dev/null 2>&1; then
    echo "Missing $executable. Install PostgreSQL client tools first." >&2
    exit 1
  fi
done
if [[ ! -f "$SCRIPT" || ! -f "$APP_DIR/.env" ]]; then
  echo "Missing $SCRIPT or $APP_DIR/.env" >&2
  exit 1
fi
install -d -m 0700 "$BACKUPS" "$STATE"
if [[ ! -e "$CONF" ]]; then
  cat > "$CONF" <<EOF
DATO_APP_DIR=$APP_DIR
DATO_ENV_FILE=$APP_DIR/.env
DATO_BACKUP_DIR=$BACKUPS
DATO_RELIABILITY_STATE_DIR=$STATE
DATO_BACKUP_RETENTION=14
DATO_BACKUP_MAX_AGE_HOURS=36
DATO_HEALTH_URL=https://bot.arutyun.me/login
# Fill with the actual systemd unit names, comma-separated, for process checks.
DATO_MONITOR_SERVICES=
# Your own Telegram chat ID; open the bot in private chat before enabling alerts.
DATO_ALERT_CHAT_ID=
EOF
fi
chmod 0600 "$CONF"

cat > /etc/systemd/system/dato-postgres-backup.service <<EOF
[Unit]
Description=Dato Telegram private PostgreSQL backup
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=root
EnvironmentFile=$CONF
ExecStart=/usr/bin/python3 $SCRIPT backup
UMask=0077
PrivateTmp=true
NoNewPrivileges=true
TimeoutStartSec=240
EOF

cat > /etc/systemd/system/dato-postgres-backup.timer <<EOF
[Unit]
Description=Daily PostgreSQL backup for Dato Telegram

[Timer]
OnCalendar=*-*-* 02:20:00 UTC
Persistent=true
RandomizedDelaySec=300
Unit=dato-postgres-backup.service

[Install]
WantedBy=timers.target
EOF

cat > /etc/systemd/system/dato-health-check.service <<EOF
[Unit]
Description=Dato Telegram reliability health check
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=root
EnvironmentFile=$CONF
ExecStart=/usr/bin/python3 $SCRIPT monitor
UMask=0077
PrivateTmp=true
NoNewPrivileges=true
TimeoutStartSec=80
# Errors must not trigger continuous systemd restarts: timer handles next check.
SuccessExitStatus=1
EOF

cat > /etc/systemd/system/dato-health-check.timer <<EOF
[Unit]
Description=Periodic Dato Telegram reliability check

[Timer]
OnBootSec=3min
OnUnitInactiveSec=15min
Persistent=true
Unit=dato-health-check.service

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
# Creating one good backup comes BEFORE enabling automated monitoring.
systemctl start dato-postgres-backup.service
systemctl enable --now dato-postgres-backup.timer dato-health-check.timer
echo "Installed: daily backup and 15-minute health check timers."
echo "Backups: $BACKUPS (root-only; 14 archives by default)."
echo "Alert settings: $CONF (edit DATO_ALERT_CHAT_ID and optional DATO_MONITOR_SERVICES)."
echo "Test alerts: sudo systemctl start dato-health-check.service"
echo "Review timers: systemctl list-timers 'dato-*'"
