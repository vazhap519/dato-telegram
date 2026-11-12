#!/usr/bin/env python3
"""Make currently healthy blue/green admin slots persistent across reboot.

Only systemctl enable/disable; NO service restarts, nginx writes or bot changes.
Requires the observed layout: Nginx -> 8081, managed blue on 8081,
managed standby on 8082, legacy admin inactive.
"""
from pathlib import Path
import os
import re
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

NGINX = Path("/etc/nginx/sites-enabled/dato-telegram")
BLUE = "dato-telegram-admin-blue.service"
STANDBY = "dato-telegram-admin-standby.service"
LEGACY = "dato-telegram-admin.service"
UNITS = (BLUE, STANDBY, LEGACY)


def run(*args):
    p = subprocess.run(args, capture_output=True, text=True, timeout=20)
    if p.returncode:
        raise RuntimeError(f"{' '.join(args[:2])} exited {p.returncode}: {p.stderr.strip()[:240]}")
    return p.stdout.strip()


def state(unit, operation):
    p = subprocess.run(["systemctl", operation, unit], capture_output=True, text=True, timeout=15)
    return p.stdout.strip() if p.returncode == 0 else "unknown"


def healthy(port):
    for path in ("/login", "/tg-support"):
        try:
            with urlopen(f"http://127.0.0.1:{port}{path}", timeout=4) as r:
                if r.status != 200:
                    return False
        except (URLError, HTTPError, TimeoutError):
            return False
    return True


def preflight():
    if os.geteuid() != 0:
        raise RuntimeError("Run as root")
    if not NGINX.is_file():
        raise RuntimeError("Expected Nginx site file missing")
    config = NGINX.read_text()
    ports = re.findall(r"proxy_pass\s+http://127\.0\.0\.1:(8081|8082)\s*;", config)
    if len(ports) != 4 or set(ports) != {"8081"}:
        raise RuntimeError(f"Expected all four Nginx upstreams on 8081; found {ports}")
    if config.count("server_name bot.arutyun.me;") != 2:
        raise RuntimeError("Unexpected Nginx server layout")
    for unit in UNITS:
        if run("systemctl", "show", unit, "--property=LoadState", "--value") != "loaded":
            raise RuntimeError(f"Service not loaded: {unit}")
    if state(BLUE, "is-active") != "active" or not healthy(8081):
        raise RuntimeError("Blue 8081 is not active and healthy")
    if state(STANDBY, "is-active") != "active" or not healthy(8082):
        raise RuntimeError("Standby 8082 is not active and healthy")
    if state(LEGACY, "is-active") == "active":
        raise RuntimeError("Legacy admin is still running; refusing to change boot policy")
    blue_unit = run("systemctl", "cat", BLUE)
    standby_unit = run("systemctl", "cat", STANDBY)
    if "--port 8081" not in blue_unit or "--port 8082" not in standby_unit:
        raise RuntimeError("Unexpected managed slot ExecStart; inspect units manually")
    if run("systemctl", "show", BLUE, "--property=WorkingDirectory", "--value").startswith("/opt/dato-admin-releases/") is False:
        raise RuntimeError("Blue unit is not pinned to an isolated release")
    if run("systemctl", "show", STANDBY, "--property=WorkingDirectory", "--value").startswith("/opt/dato-admin-releases/") is False:
        raise RuntimeError("Standby unit is not pinned to an isolated release")


def main():
    preflight()
    old_states = {unit:state(unit,"is-enabled") for unit in UNITS}
    print("Preflight PASS: 8081 and 8082 healthy; Nginx uses 8081; legacy inactive.")
    print("Current boot policy:", old_states)
    try:
        # Prevent legacy 8081 from racing with managed blue 8081 on next boot.
        run("systemctl", "disable", LEGACY)
        run("systemctl", "enable", BLUE)
        run("systemctl", "enable", STANDBY)
        for unit, expected in ((BLUE,"enabled"),(STANDBY,"enabled"),(LEGACY,"disabled")):
            if state(unit,"is-enabled") != expected:
                raise RuntimeError(f"{unit}: expected boot={expected}")
        if not healthy(8081) or not healthy(8082):
            raise RuntimeError("Health check failed after changing boot policy")
    except Exception:
        # Restore prior boot flags as far as possible; running units untouched.
        for unit, previous in old_states.items():
            if previous == "enabled":
                subprocess.run(["systemctl","enable",unit],capture_output=True)
            elif previous == "disabled":
                subprocess.run(["systemctl","disable",unit],capture_output=True)
        raise
    print("BOOT POLICY OK: blue=enabled, standby=enabled, legacy=disabled")
    print("All running services left untouched. Telegram bot unchanged.")
    print("No VPS reboot performed.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("BOOT CONFIG FAILED:",str(exc),file=sys.stderr)
        sys.exit(1)
