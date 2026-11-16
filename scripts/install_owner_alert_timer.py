#!/usr/bin/env python3
"""Install a 15-minute systemd timer for owner alerts AFTER public cutover.

Default: READ ONLY. Explicit --install writes only these two systemd units,
never starts polling. No Telegram notification if owner hasn't opted in.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import textwrap

ROOT=Path("/opt/dato-telegram")
RELEASE=Path("/opt/dato-admin-releases/c1792778c7441dbec7c3efd8d829227493e83c55")
BOT=Path("/opt/dato-admin-releases/2d609a3267261e2370e01e02596916d1bdc044c6")
SERVICE=Path("/etc/systemd/system/dato-owner-alerts.service")
TIMER=Path("/etc/systemd/system/dato-owner-alerts.timer")
CALENDAR="*-*-* *:00/15:00"

def run(*args):
    return subprocess.check_output(args,text=True,timeout=30).strip()

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--install",action="store_true",help="Create and start periodic oneshot timer")
    args=ap.parse_args()
    if os.geteuid()!=0:raise RuntimeError("Root required")
    if run("git","-C",str(ROOT),"status","--porcelain"):
        raise RuntimeError("Working Git tree dirty")
    if run("systemctl","show","dato-telegram-admin-blue.service","-p","WorkingDirectory","--value")!=str(RELEASE):
        raise RuntimeError("Alert release is not public admin")
    if run("systemctl","show","dato-telegram-bot.service","-p","WorkingDirectory","--value")!=str(BOT):
        raise RuntimeError("Existing bot changed")
    if run("systemctl","is-active","dato-telegram-admin-blue.service")!="active":
        raise RuntimeError("Active admin is down")
    if run("systemctl","is-active","dato-telegram-bot.service")!="active":
        raise RuntimeError("Bot is down")
    if not (RELEASE/".venv/bin/python").is_file():
        raise RuntimeError("Pinned alert release absent")
    if not Path("/usr/bin/flock").is_file():
        raise RuntimeError("flock unavailable; no duplicate protection")
    result=subprocess.run(["systemd-analyze","calendar",CALENDAR],
                          capture_output=True,text=True,timeout=10)
    if result.returncode:raise RuntimeError("Invalid systemd schedule")
    if SERVICE.exists() or TIMER.exists():
        raise RuntimeError("Alert unit already installed; inspect before changing anything")
    print("OWNER ALERTS TIMER PLAN: one-shot every 15 min, owner opt-in only",flush=True)
    print("Admin release:",RELEASE.name,"Bot unchanged",flush=True)
    if not args.install:
        print("READ-ONLY COMPLETE. No units installed.")
        return
    # Dry-run checks all actual SQL without sending or writing delivery records.
    print("CHECKING ACTUAL ALERT QUERIES IN DRY-RUN",flush=True)
    subprocess.run([str(RELEASE/".venv/bin/python"),"-m",
                    "scripts.owner_alert_worker","--dry-run"],
                   cwd=RELEASE,check=True,timeout=65)
    unit=textwrap.dedent(f"""\
        [Unit]
        Description=Dato owner alerts (no Telegram polling)
        After=network-online.target postgresql.service
        Wants=network-online.target
        [Service]
        Type=oneshot
        WorkingDirectory={RELEASE}
        ExecStart=/usr/bin/flock -n -E 0 /run/lock/dato-owner-alerts.lock {RELEASE}/.venv/bin/python -m scripts.owner_alert_worker --run
        TimeoutStartSec=100
        UMask=0077
        NoNewPrivileges=true
        PrivateTmp=true
        """)
    timer=textwrap.dedent(f"""\
        [Unit]
        Description=Owner alert checks every 15 minutes (opt-in)
        [Timer]
        OnCalendar={CALENDAR}
        AccuracySec=1min
        RandomizedDelaySec=10s
        Persistent=true
        Unit={SERVICE.name}
        [Install]
        WantedBy=timers.target
        """)
    try:
        SERVICE.write_text(unit)
        TIMER.write_text(timer)
        SERVICE.chmod(0o644);TIMER.chmod(0o644)
        subprocess.run(["systemctl","daemon-reload"],check=True,timeout=20)
        subprocess.run(["systemctl","enable","--now",TIMER.name],check=True,timeout=20)
        if run("systemctl","is-active",TIMER.name)!="active":
            raise RuntimeError("Timer not active")
    except BaseException:
        subprocess.run(["systemctl","disable","--now",TIMER.name],
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=20)
        SERVICE.unlink(missing_ok=True);TIMER.unlink(missing_ok=True)
        subprocess.run(["systemctl","daemon-reload"],timeout=20)
        raise
    print("OWNER ALERT TIMER INSTALLED: every 15 minutes",flush=True)
    print("No Telegram polling bot started; existing polling service unchanged.",flush=True)
    print("Delivery is OFF until owner enables it in Telegram Mini App → Аналитика.",flush=True)

if __name__=="__main__":
    try:main()
    except Exception as exc:
        print("OWNER ALERT TIMER STOPPED:",type(exc).__name__,str(exc).splitlines()[0][:350],file=sys.stderr)
        sys.exit(1)
