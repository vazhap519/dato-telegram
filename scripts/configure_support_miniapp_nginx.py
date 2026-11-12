#!/usr/bin/env python3
"""Idempotently exempt only /tg-support from Nginx Basic Auth.

Mini App API still requires valid Telegram-signed initData plus a linked,
enabled administrator. Does not modify other locations or expose admin pages.
Run explicitly as root after deploying the matching app code.
"""
from datetime import datetime, timezone
from pathlib import Path
import os
import shutil
import subprocess
import sys

CONFIG = Path("/etc/nginx/sites-enabled/dato-telegram")
BACKUPS = Path("/var/backups/dato-nginx")
MARKER = "    # Tribute webhook: HMAC verification in FastAPI"
MINIAPP = """    # Telegram support Mini App. API validates Telegram initData and staff role.
    location = /tg-support {
        auth_basic off;
        proxy_pass http://127.0.0.1:8081;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location ^~ /tg-support/ {
        auth_basic off;
        proxy_pass http://127.0.0.1:8081;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

"""


def run(*args):
    return subprocess.run(args, capture_output=True, text=True)


def main():
    if os.geteuid() != 0:
        raise SystemExit("Run with sudo/root")
    if not CONFIG.is_file():
        raise SystemExit("Nginx site configuration not found; no changes made")
    original = CONFIG.read_text()
    exact = "    location = /tg-support {"
    prefix = "    location ^~ /tg-support/ {"
    if exact in original or prefix in original:
        if exact in original and prefix in original and original.count(exact) == original.count(prefix) == 1:
            print("Mini App Nginx locations already present; no changes made")
            return
        raise SystemExit("Partial Mini App Nginx configuration; review manually")
    if original.count(MARKER) != 1 or original.count("server_name bot.arutyun.me;") != 2:
        raise SystemExit("Nginx site layout changed; no changes made")
    if 'auth_basic "Dato Telegram Admin";' not in original:
        raise SystemExit("Expected existing Basic Auth protection not found; no changes made")
    revised = original.replace(MARKER, MINIAPP + MARKER, 1)
    BACKUPS.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = BACKUPS / f"dato-telegram.before-miniapp-{stamp}.conf"
    shutil.copy2(CONFIG, backup)
    backup.chmod(0o600)

    # Write through possible sites-enabled symlink, preserving its target.
    CONFIG.write_text(revised)
    check = run("nginx", "-t")
    if check.returncode != 0:
        CONFIG.write_text(original)
        print(check.stderr, file=sys.stderr)
        raise SystemExit("Nginx syntax invalid; original configuration restored")

    reload = run("systemctl", "reload", "nginx")
    if reload.returncode != 0:
        CONFIG.write_text(original)
        run("nginx", "-t")
        run("systemctl", "reload", "nginx")
        print(reload.stderr, file=sys.stderr)
        raise SystemExit("Nginx reload failed; original configuration restored")

    print("Nginx updated: only /tg-support and /tg-support/ bypass Basic Auth")
    print("Backup:", backup)
    print("Check: curl -sSI https://bot.arutyun.me/tg-support/api/me")
    print("Expected: 401 application/json WITHOUT WWW-Authenticate: Basic")


if __name__ == "__main__":
    main()
