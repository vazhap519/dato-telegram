#!/usr/bin/env bash
# Opt-in installation, runs only after an encrypted rclone destination is configured.
set -euo pipefail
if [[ "$EUID" -ne 0 ]]; then echo "Run as root"; exit 1; fi
CONF="/etc/dato-offsite-backup.conf"
if [[ ! -f "$CONF" ]]; then
  cat > "$CONF" <<'EOF'
# Configure an rclone remote of type crypt first.
# Example: encrypted_remote:community-backups
DATO_RCLONE_CRYPT_REMOTE=
DATO_BACKUP_DIR=/var/backups/dato-telegram
EOF
  chmod 0600 "$CONF"
  echo "Edit $CONF with your crypt remote, then rerun this installer."
  exit 1
fi
chmod 0600 "$CONF"
# The helper rejects non-crypt destinations, missing archives and bad round-trip hashes.
bash /opt/dato-telegram/scripts/offsite_backup.sh
cat > /etc/systemd/system/dato-offsite-backup.service <<'EOF'
[Unit]
Description=Encrypted offsite PostgreSQL backup
After=network-online.target dato-postgres-backup.service
Wants=network-online.target

[Service]
Type=oneshot
User=root
ExecStart=/bin/bash /opt/dato-telegram/scripts/offsite_backup.sh
UMask=0077
NoNewPrivileges=true
TimeoutStartSec=1800
EOF
cat > /etc/systemd/system/dato-offsite-backup.timer <<'EOF'
[Unit]
Description=Daily encrypted offsite PostgreSQL backup
[Timer]
OnCalendar=*-*-* 03:10:00 UTC
RandomizedDelaySec=300
Persistent=true
Unit=dato-offsite-backup.service
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now dato-offsite-backup.timer
echo "Offsite backup timer installed after verified upload and download."
