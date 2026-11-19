#!/usr/bin/env python3
"""Guarded rollout of the formatted analyst XLSX export.

Pinned to the verified layout after the signed CSV cutover:
public 8082 = b31373d..., standby 8081 = 1d33bb2...
Only the inactive admin slot is changed. The polling bot and timers are read-only.
"""
import argparse
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT = Path("/opt/dato-telegram")
RELEASES = Path("/opt/dato-admin-releases")
CURRENT = "b31373da0cfabb3168d4d900635f6afe5c21bb28"
OLD_STANDBY = "1d33bb2d312ded6cd17abbfbea22a0ae48dc58b5"
BOT_PID = "1127764"
EXPECTED = {
    "admin/analyst_miniapp.py",
    "admin/templates/analyst_miniapp.html",
    "tests/test_analyst_dashboard.py",
    "scripts/rollout_analyst_miniapp.py",
    "requirements.txt",
}
MARK = 'id="lifecycle-rules"'


def cmd(*args):
    return subprocess.check_output(args, text=True, timeout=30).strip()


def response(url, method="GET"):
    try:
        from urllib.request import Request
        request = Request(url, method=method)
        with urlopen(request, timeout=7) as r:
            return r.status, r.read(180000).decode("utf-8", errors="replace")
    except HTTPError as e:
        return e.code, ""


def bg_module():
    sys.path.insert(0, str(RELEASES / CURRENT / "scripts"))
    import deploy_admin_bluegreen
    return deploy_admin_bluegreen


def bot_state():
    pid = cmd("systemctl", "show", "dato-telegram-bot.service", "-p", "MainPID", "--value")
    wd = cmd("systemctl", "show", "dato-telegram-bot.service", "-p", "WorkingDirectory", "--value")
    if pid != BOT_PID or not wd.startswith(str(RELEASES) + "/"):
        raise RuntimeError("Telegram polling bot PID/release differs from pinned state")
    if cmd("systemctl", "is-active", "dato-owner-alerts.timer") != "active":
        raise RuntimeError("Owner alerts timer is not active")
    timer = subprocess.run(
        ["systemctl", "is-active", "dato-lifecycle-followups.timer"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15
    )
    if timer.stdout.strip() == "active" or timer.returncode not in (0, 3, 4):
        raise RuntimeError("Lifecycle sender timer is not in the expected inactive state")
    return pid


def topology(bg):
    active = bg.routing(bg.nginx_path().read_text())
    idle = 8081 if active == 8082 else 8082
    return active, idle


def prereqs(bg):
    active, idle = topology(bg)
    if active != 8082 or idle != 8081:
        raise RuntimeError(f"Unexpected live routing: public={active}, inactive={idle}")
    if bg.release_sha_for_port(active) != CURRENT:
        raise RuntimeError("Public admin release differs from pinned b31373d release")
    if bg.release_sha_for_port(idle) != OLD_STANDBY:
        raise RuntimeError("Standby admin release differs from pinned 1d33bb2 release")
    for port in (active, idle):
        if cmd("systemctl", "is-active", bg.UNITS[port]) != "active" or not bg.health(port):
            raise RuntimeError("Admin slot unhealthy on " + str(port))
    return bot_state()


def verify_git(target):
    if cmd("git", "-C", str(ROOT), "status", "--porcelain"):
        raise RuntimeError("Production Git working tree is dirty")
    if cmd("git", "-C", str(ROOT), "rev-parse", "--verify", target) != target:
        raise RuntimeError("Target commit unavailable; fetch the feature branch first")
    changed = set(cmd("git", "-C", str(ROOT), "diff", "--name-only", CURRENT, target).splitlines())
    if changed != EXPECTED:
        raise RuntimeError("Unexpected candidate files: " +
                           str(sorted(changed.symmetric_difference(EXPECTED))))
    for name in ("bot/main.py", "bot/support_admin.py", "db/support_telegram.py",
                 "scripts/deploy_admin_bluegreen.py", "db/lifecycle_followups.py"):
        if cmd("git", "-C", str(ROOT), "rev-parse", CURRENT + ":" + name) != cmd(
            "git", "-C", str(ROOT), "rev-parse", target + ":" + name
        ):
            raise RuntimeError("Unrelated bot/support/lifecycle implementation changed: " + name)


def prepare(bg, target):
    verify_git(target)
    pid = prereqs(bg)
    if bg.memory_mib() < 180 or shutil.disk_usage(ROOT).free < 1024**3:
        raise RuntimeError("Insufficient RAM/disk headroom")
    release = bg.snapshot(target)
    python = bg.ensure_venv(release)
    for pattern in ("test_support_permissions.py", "test_analyst_dashboard.py"):
        subprocess.run(
            [str(python), "-m", "unittest", "discover", "-s", str(release / "tests"),
             "-p", pattern, "-v"],
            cwd=release, timeout=120, check=True
        )
    subprocess.run(
        [str(python), "-m", "compileall", "-q", str(release / "admin"),
         str(release / "db"), str(release / "scripts")],
        cwd=release, timeout=45, check=True
    )
    probe = r"""
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('admin/templates'))
env.filters['tbilisi_time'] = lambda value, pattern='%d.%m.%Y %H:%M': value
env.get_template('admin_staff.html')
env.get_template('analyst_dashboard.html')
env.get_template('analyst_miniapp.html')
print('STAFF AND ANALYST TEMPLATES PARSE OK')
"""
    subprocess.run([str(python), "-c", probe], cwd=release, timeout=20, check=True)
    print("ANALYST XLSX EXPORT PREPARED:", target, flush=True)
    print("PUBLIC REMAINS 8081; BOT PID:", pid,
          "OWNER ALERTS ACTIVE; LIFECYCLE SENDER OFF", flush=True)


def stage(bg, target):
    release = RELEASES / target
    if not (release / ".venv/.install-ok").is_file():
        raise RuntimeError("Run prepare first")
    pid = prereqs(bg)
    active, idle = topology(bg)
    unit = Path("/etc/systemd/system") / bg.UNITS[idle]
    previous = unit.read_text()
    stopped = False
    print("STAGING ANALYST CSV FIX ON INACTIVE", idle, flush=True)
    try:
        bg.command("systemctl", "stop", bg.UNITS[idle])
        stopped = True
        if bg.health(idle):
            raise RuntimeError("Inactive port still responds after stop")
        unit.write_text(bg.unit_content(release, idle))
        unit.chmod(0o644)
        bg.command("systemctl", "daemon-reload")
        bg.command("systemctl", "start", bg.UNITS[idle])
        for _ in range(75):
            if bg.health(idle):
                break
            if bg.command("systemctl", "is-active", bg.UNITS[idle], check=False) != "active":
                raise RuntimeError("Staged analyst admin process exited")
            time.sleep(1)
        else:
            raise RuntimeError("Staged analyst admin startup timeout")
        status, html = response(f"http://127.0.0.1:{idle}/tg-support/automations")
        if status != 200 or MARK not in html:
            raise RuntimeError("Staged admin lost existing lifecycle interface")
        for path in ("/tg-support/api/automations/lifecycle",
                     "/tg-support/api/home", "/tg-support/analyst/api/me"):
            if response(f"http://127.0.0.1:{idle}" + path)[0] != 401:
                raise RuntimeError("Staged Telegram API auth failure: " + path)
        if response(f"http://127.0.0.1:{idle}/tg-support/analyst/api/export-link?period=30", "POST")[0] != 401:
            raise RuntimeError("Staged signed export-link endpoint did not enforce Telegram auth")
        if response(f"http://127.0.0.1:{idle}/tg-support/analyst/api/export-file/invalid.invalid")[0] != 403:
            raise RuntimeError("Staged signed export-file endpoint did not reject an invalid token")
        if bg.routing(bg.nginx_path().read_text()) != active or not bg.health(active) or bot_state() != pid:
            raise RuntimeError("Public admin, bot or timer state changed during stage")
    except BaseException:
        if stopped:
            print("RESTORING PREVIOUS INACTIVE", idle, flush=True)
            bg.command("systemctl", "stop", bg.UNITS[idle], check=False)
            unit.write_text(previous)
            bg.command("systemctl", "daemon-reload", check=False)
            bg.command("systemctl", "start", bg.UNITS[idle], check=False)
        raise
    print("SIGNED ANALYST CSV FIX STAGED ON", idle, ":", target, flush=True)
    print("PUBLIC UNCHANGED ON", active, "; BOT PID UNCHANGED:", pid, flush=True)


def check(bg, target):
    active, idle = topology(bg)
    if active != 8082 or idle != 8081:
        raise RuntimeError("Public routing changed before cutover (expected active 8082)")
    if bg.release_sha_for_port(active) != CURRENT or bg.release_sha_for_port(idle) != target:
        raise RuntimeError("Active/standby release SHA mismatch")
    if not bg.health(active) or not bg.health(idle):
        raise RuntimeError("Admin slot unhealthy")
    for port in (active, idle):
        status, html = response(f"http://127.0.0.1:{port}/tg-support/automations")
        if status != 200 or MARK not in html:
            raise RuntimeError("Existing lifecycle UI missing on port " + str(port))
        for path in ("/tg-support/api/automations/lifecycle", "/tg-support/api/home"):
            if response(f"http://127.0.0.1:{port}" + path)[0] != 401:
                raise RuntimeError("Admin API authorization failed on " + str(port) + ": " + path)
    if response(f"http://127.0.0.1:{idle}/tg-support/analyst/api/me")[0] != 401:
        raise RuntimeError("Analyst Telegram API authorization failed on staged port")
    if response(f"http://127.0.0.1:{idle}/tg-support/analyst/api/export-link?period=30", "POST")[0] != 401:
        raise RuntimeError("Signed export-link endpoint did not enforce Telegram auth")
    if response(f"http://127.0.0.1:{idle}/tg-support/analyst/api/export-file/invalid.invalid")[0] != 403:
        raise RuntimeError("Signed export-file endpoint did not reject an invalid token")
    page_status, page_html = response(f"http://127.0.0.1:{idle}/tg-support/analyst")
    if page_status != 200 or "Панель аналитика" not in page_html:
        raise RuntimeError("Analyst Mini App page missing on staged port")
    if "tg.downloadFile" not in page_html or 'data-export-format="xlsx"' not in page_html or 'data-export-format="csv"' not in page_html:
        raise RuntimeError("Telegram-native XLSX/CSV export UI missing on staged port")
    pid = bot_state()
    print(f"SIGNED ANALYST CSV FIX CUTOVER PRECHECK OK: {active} -> {idle}; bot {pid}", flush=True)
    return pid


def verify_public():
    for attempt in range(24):
        status, html = response("https://bot.arutyun.me/tg-support/automations")
        if status == 200 and MARK in html:
            print("PUBLIC LIFECYCLE UI VERIFIED; check", attempt + 1, flush=True)
            return
        time.sleep(0.5)
    raise RuntimeError("Public admin route did not converge after cutover")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("prepare", "stage", "check", "cutover"))
    parser.add_argument("--target", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("Run as root")
    if len(args.target) != 40 or any(c not in "0123456789abcdef" for c in args.target):
        raise RuntimeError("Target must be a full Git commit SHA")
    bg = bg_module()
    bg.LOCK.parent.mkdir(parents=True, exist_ok=True)
    with bg.LOCK.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.operation == "prepare":
            prepare(bg, args.target)
            return
        if args.operation == "stage":
            verify_git(args.target)
            stage(bg, args.target)
            return
        pid = check(bg, args.target)
        if args.operation == "check":
            print("READ ONLY COMPLETE; NO CHANGES", flush=True)
            return
        active, idle = topology(bg)
        bg.cutover()
        try:
            if bg.routing(bg.nginx_path().read_text()) != idle:
                raise RuntimeError("Nginx did not switch to staged slot")
            verify_public()
            for path in ("/tg-support/api/automations/lifecycle",
                         "/tg-support/api/home", "/tg-support/analyst/api/me"):
                if response("https://bot.arutyun.me" + path)[0] != 401:
                    raise RuntimeError("Public API authorization failed: " + path)
            public_status, public_html = response("https://bot.arutyun.me/tg-support/analyst")
            if public_status != 200 or "tg.downloadFile" not in public_html or 'data-export-format="xlsx"' not in public_html:
                raise RuntimeError("Public analyst XLSX export UI did not converge")
            if bot_state() != pid:
                raise RuntimeError("Telegram bot/timer state changed")
            print(f"SIGNED ANALYST CSV FIX PUBLIC CUTOVER VERIFIED: {active} -> {idle}; rollback {active} remains running",
                  flush=True)
            print("No bot restart, no .env changes, no lifecycle sender activation.", flush=True)
        except Exception:
            print("ANALYST CSV FIX CUTOVER FAILED; ROLLING BACK", file=sys.stderr, flush=True)
            bg.cutover()
            if bg.routing(bg.nginx_path().read_text()) != active:
                raise RuntimeError("CRITICAL: Nginx rollback failed")
            raise


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("ANALYST CSV ROLLOUT STOPPED:", type(exc).__name__,
              str(exc).splitlines()[0][:350], file=sys.stderr)
        sys.exit(1)
