#!/usr/bin/env bash
# Encrypted offsite backup via an existing rclone crypt remote.
# No secrets or remote endpoints are stored in this repository.
set -euo pipefail
umask 077
CONF="${DATO_OFFSITE_CONF:-/etc/dato-offsite-backup.conf}"
if [[ "${EUID}" -ne 0 ]]; then echo "Run as root" >&2; exit 1; fi
if [[ ! -f "$CONF" ]]; then echo "Configure $CONF first" >&2; exit 1; fi
# This root-owned config contains only a remote name and optional backup folder.
source "$CONF"
remote="${DATO_RCLONE_CRYPT_REMOTE:-}"
dir="${DATO_BACKUP_DIR:-/var/backups/dato-telegram}"
if [[ ! "$remote" =~ ^[a-zA-Z0-9_-]+:.+$ ]]; then
  echo "Missing/invalid DATO_RCLONE_CRYPT_REMOTE in $CONF" >&2; exit 1
fi
for tool in rclone pg_restore sha256sum; do
  command -v "$tool" >/dev/null || { echo "$tool is missing" >&2; exit 1; }
done
name="${remote%%:*}"
if ! rclone config show "$name" 2>/dev/null | grep -Eq '^[[:space:]]*type[[:space:]]*=[[:space:]]*crypt[[:space:]]*$'; then
  echo "The destination must be a preconfigured rclone CRYPT remote" >&2
  exit 1
fi
latest=""
for f in "$dir"/dato-*.dump; do
  [[ -f "$f" ]] || continue
  if [[ -z "$latest" || "$f" -nt "$latest" ]]; then latest="$f"; fi
done
if [[ -z "$latest" ]]; then echo "No local PostgreSQL archive found" >&2; exit 1; fi
if (( $(date +%s) - $(stat -c %Y "$latest") > 36*3600 )); then
  echo "Latest local backup is stale; offsite upload aborted" >&2; exit 1
fi
pg_restore --list "$latest" >/dev/null
destination="${remote%/}/$(basename "$latest")"
rclone copyto "$latest" "$destination" --retries 3 --low-level-retries 5
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
rclone copyto "$destination" "$tmp" --retries 3
if ! cmp -s "$latest" "$tmp"; then
  echo "Offsite round-trip verification failed" >&2; exit 1
fi
install -d -m 0700 /var/lib/dato-reliability
date -u +%s > /var/lib/dato-reliability/offsite-last-success
chmod 0600 /var/lib/dato-reliability/offsite-last-success
echo "Encrypted offsite archive verified:" "$(basename "$latest")"
