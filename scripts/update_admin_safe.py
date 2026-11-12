#!/usr/bin/env python3
"""Single-command blue/green update for the FastAPI admin only.

Run from the same directory as deploy_admin_bluegreen.py (installed copies).
Does not update, stop, or restart Telegram polling. Does not modify the
mutable /opt/dato-telegram working tree or its shared virtual environment.

Commands:
  check    read-only readiness and current slot metadata (no git fetch)
  deploy   fetch main, stage inactive port, cut over, verify, roll back on failure
  rollback switch traffic to the healthy previous slot (no process restarts)
  history  show last 15 audit records
"""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.request import Request, urlopen

# This import is from a trusted, installed local pair of scripts, never
# from a newly fetched release which is only staged after review.
import deploy_admin_bluegreen as bg

AUDIT_DIR=Path("/var/log/dato-deploy")
AUDIT_FILE=AUDIT_DIR/"admin-updates.jsonl"
LOCK=bg.LOCK
BOT_UNIT="dato-telegram-bot.service"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def audit(event, **fields):
    AUDIT_DIR.mkdir(mode=0o700,parents=True,exist_ok=True)
    row={"time":now(),"event":event,**fields}
    with AUDIT_FILE.open("a") as file:
        os.chmod(AUDIT_FILE,0o600)
        file.write(json.dumps(row,ensure_ascii=False,separators=(",",":"))+"\n")
        file.flush()
        os.fsync(file.fileno())


def service_state(unit):
    p=subprocess.run(["systemctl","is-active",unit],capture_output=True,text=True,timeout=10)
    return p.stdout.strip()


def enabled(unit):
    p=subprocess.run(["systemctl","is-enabled",unit],capture_output=True,text=True,timeout=10)
    return p.stdout.strip()


def active_port():
    return bg.routing(bg.nginx_path().read_text())


def release_for(port):
    unit=bg.UNITS[port]
    wd=bg.command("systemctl","show",unit,"-p","WorkingDirectory","--value",check=False)
    p=Path(wd)
    if p.parent!=bg.RELEASES or not re.fullmatch("[0-9a-f]{40}",p.name):
        raise RuntimeError(f"Port {port} does not use an immutable managed release")
    return p.name


def check():
    port=active_port()
    spare=8081 if port==8082 else 8082
    active_sha=release_for(port)
    print("=== ADMIN BLUE/GREEN CHECK ===")
    print("Active port:",port, "commit:",active_sha[:12], "healthy:",bg.health(port))
    print("Inactive port:",spare, "commit:",bg.release_sha_for_port(spare),
          "healthy:",bg.health(spare))
    print("Available RAM:",bg.memory_mib(),"MiB")
    print("Free disk:",bg.shutil.disk_usage(bg.ROOT).free//(1024**2),"MiB")
    print("Git working tree:", "clean" if not bg.command("git","-C",str(bg.ROOT),"status","--porcelain") else "modified")
    print("Git origin/main:",bg.target_commit()[:12])
    print("Legacy admin boot:",enabled(bg.LEGACY),
          "managed slots:",enabled(bg.UNITS[8081]),enabled(bg.UNITS[8082]))
    print("Bot:",service_state(BOT_UNIT),"(not touched)")


def public_health():
    for path in ("/tg-support",):
        req=Request("https://bot.arutyun.me"+path,headers={"Cache-Control":"no-cache"})
        with urlopen(req,timeout=8) as response:
            if response.status!=200:
                raise RuntimeError(f"Public {path} returned {response.status}")


def switch_back(expected_from, destination):
    # Only alter all four Nginx reverse-proxy routes after checking the
    # previous release is still healthy.
    if active_port()!=expected_from:
        raise RuntimeError("Traffic route changed unexpectedly; manual intervention needed")
    if not bg.health(destination):
        raise RuntimeError("Previous slot unhealthy; cannot automatically roll back")
    path=bg.nginx_path()
    old=path.read_text()
    changed=old.replace(f"proxy_pass http://127.0.0.1:{expected_from};",
                        f"proxy_pass http://127.0.0.1:{destination};")
    if bg.routing(changed)!=destination:
        raise RuntimeError("Rollback Nginx replacement failed preflight")
    bg.write_atomic(path,changed)
    try:
        bg.command("nginx","-t")
        bg.command("systemctl","reload","nginx")
        public_health()
    except Exception:
        bg.write_atomic(path,old)
        bg.command("nginx","-t",check=False)
        bg.command("systemctl","reload","nginx",check=False)
        raise
    print(f"Traffic restored to previous port {destination}; no service was stopped.")


def do_deploy():
    # Early fail before fetching or touching systemd when current state is unsafe.
    old=active_port()
    spare=8081 if old==8082 else 8082
    old_sha=release_for(old)
    if not bg.health(old):
        raise RuntimeError("Active admin unhealthy; refusing update")
    if enabled(bg.LEGACY)!="disabled":
        raise RuntimeError("Legacy admin auto-start not disabled")
    for unit in bg.UNITS.values():
        if enabled(unit)!="enabled":
            raise RuntimeError(f"{unit} is not enabled on boot")
    if service_state(BOT_UNIT)!="active":
        raise RuntimeError("Bot is not active; investigate before deploying")
    if bg.command("git","-C",str(bg.ROOT),"status","--porcelain"):
        raise RuntimeError("Working repository not clean; check git status --short")
    print("Fetching GitHub main...",flush=True)
    bg.command("git","-C",str(bg.ROOT),"fetch","origin","main",timeout=150)
    sha=bg.target_commit()
    # Do not blindly cut over to a commit already deployed.
    if sha==old_sha:
        print("Already running newest origin/main commit:",sha[:12])
        audit("noop",commit=sha,port=old)
        return
    if subprocess.run(["git","-C",str(bg.ROOT),"merge-base","--is-ancestor",old_sha,sha],
                      capture_output=True).returncode:
        raise RuntimeError("Target is not descendant of running admin release; manual review required")
    print("Current:",old_sha[:12],"at",old,"Target:",sha[:12],"at",spare)
    audit("begin",from_commit=old_sha,to_commit=sha,active_port=old,stage_port=spare)
    try:
        bg.stage(sha)
        if not bg.health(old) or not bg.health(spare):
            raise RuntimeError("Health failed after stage; no cutover attempted")
        if release_for(spare)!=sha:
            raise RuntimeError("Staged release SHA does not match target")
        audit("staged",commit=sha,port=spare)
        bg.cutover()
        # Must inspect all four upstreams and active + old before success.
        for attempt in range(3):
            if active_port()==spare and bg.health(spare) and bg.health(old):
                public_health()
                if service_state(BOT_UNIT)!="active":
                    raise RuntimeError("Telegram bot unavailable following admin deploy")
                audit("success",commit=sha,from_port=old,to_port=spare)
                print("UPDATE COMPLETE: active port",spare,"commit",sha[:12])
                print("Previous admin remains on",old,"for rollback.")
                return
            time.sleep(2)
        raise RuntimeError("Post-cutover validation failed")
    except Exception as exc:
        try:
            if active_port()==spare:
                switch_back(spare,old)
                audit("rollback_auto",to_port=old,commit=old_sha)
            elif active_port()!=old:
                audit("rollback_manual_needed",reason="Unexpected routing")
        except Exception as rb:
            audit("rollback_failed",error=type(rb).__name__)
            print("CRITICAL: automatic rollback failed. Inspect Nginx and slot health.",file=sys.stderr)
        audit("failed",error=type(exc).__name__,commit=sha)
        raise


def manual_rollback():
    port=active_port()
    other=8081 if port==8082 else 8082
    if not bg.health(other):
        raise RuntimeError("Previous slot is not healthy; rollback refused")
    if release_for(other)==release_for(port):
        raise RuntimeError("Both slots serve the same release; rollback would not help")
    switch_back(port,other)
    audit("rollback_manual",from_port=port,to_port=other)
    print("Rollback complete. Check Telegram Mini App and admin pages.")


def history():
    if not AUDIT_FILE.exists():
        print("No deployment history yet.")
        return
    # Keep each record on one line. Never log .env or API tokens.
    for line in AUDIT_FILE.read_text().splitlines()[-15:]:
        try:
            record=json.loads(line)
            print(record.get("time",""),record.get("event",""),
                  record.get("commit",record.get("to_commit",""))[:12],
                  "port",record.get("to_port",record.get("active_port","")))
        except (ValueError,TypeError):
            print("(invalid audit entry)")


def main():
    parser=argparse.ArgumentParser(description="Safe blue/green FastAPI admin deploy; Telegram bot untouched")
    parser.add_argument("action",choices=("check","deploy","rollback","history"))
    a=parser.parse_args()
    if os.geteuid()!=0:
        raise RuntimeError("Run as root")
    if a.action in ("check","history"):
        {"check":check,"history":history}[a.action]()
        return
    LOCK.parent.mkdir(parents=True,exist_ok=True)
    with LOCK.open("a+") as file:
        fcntl.flock(file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if a.action=="deploy":do_deploy()
        else:manual_rollback()


if __name__=="__main__":
    try:
        main()
    except Exception as exc:
        print("ADMIN UPDATE FAILED:",str(exc)[:350],file=sys.stderr)
        raise SystemExit(1)
