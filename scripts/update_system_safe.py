#!/usr/bin/env python3
"""Conservative coordinated release for Telegram bot + Blue/Green admin.

All release scripts are installed together into an isolated, trusted installer
directory. They target the SAME frozen origin/main SHA. Production DB migrations
are explicitly NOT automatic. No daemon, scheduler, or automatic deployment.

check     read-only state and compatibility report (does not fetch)
prepare   fetch and prepare both isolated venvs, without service changes
deploy    fresh DB backup, staged admin, public cutover, singleton bot switch,
          verification and compensating rollback
history   private deployment audit (no tokens)
"""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

import deploy_admin_bluegreen as bg
import update_admin_safe as admin
import update_bot_safe as bot

LOCK=bg.LOCK
AUDIT=Path("/var/log/dato-deploy/system-updates.jsonl")
SCHEMA_PARTS=("db/","migrations/","alembic/","database/")
EXTRA_RISK=(".sql",)
SHA_RE=re.compile(r"^[0-9a-f]{40}$")

# One-time reviewed exception: only these EXACT two source blobs can be used.
# All unrelated DB, migration, SQL, or dependency differences remain blocked.
# The exception additionally requires explicit CLI opt-in and verified DB schema.
REVIEWED_SUPPORT_BLOBS={
    "db/admin_access.py": "3f0bab355a57e558e7748829c848349522bf7e3e",
    "db/support_telegram.py": "a8bc3ae105166248a70f7cb30b19d043a500af9b",
}
SUPPORT_COLUMNS=("support_access","support_can_reply",
                 "support_can_change_status","support_can_close",
                 "support_can_delete")


def verified_support_schema():
    """Check already-applied migration, never run migrations from the deployer."""
    import psycopg2
    from urllib.parse import urlsplit, unquote, parse_qs
    envfile=bot.ENV
    values={}
    for line in envfile.read_text().splitlines():
        line=line.strip()
        if line.startswith("export "):line=line[7:]
        if line.startswith("#") or "=" not in line:continue
        key,val=line.split("=",1)
        if key.strip()=="DATABASE_URL":
            values[key.strip()]=val.strip().strip("'\\\"")
    url=os.environ.get("DATABASE_URL") or values.get("DATABASE_URL")
    if not url:raise RuntimeError("DATABASE_URL unavailable for compatibility verification")
    parsed=urlsplit(url)
    if not parsed.scheme.startswith("postgresql") or not parsed.hostname:
        raise RuntimeError("Invalid DATABASE_URL scheme")
    kwargs={"host":parsed.hostname,"port":parsed.port or 5432,
            "user":unquote(parsed.username or ""),"password":unquote(parsed.password or ""),
            "dbname":unquote(parsed.path.lstrip("/")),"connect_timeout":8}
    params=parse_qs(parsed.query)
    if "sslmode" in params:kwargs["sslmode"]=params["sslmode"][0]
    if "sslrootcert" in params:kwargs["sslrootcert"]=params["sslrootcert"][0]
    with psycopg2.connect(**kwargs) as conn:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            cur.execute("""SELECT column_name,data_type,is_nullable
                FROM information_schema.columns
                WHERE table_schema=current_schema() AND table_name='admins'
                AND column_name=ANY(%s)""",(list(SUPPORT_COLUMNS),))
            found={name:(dtype,nullable) for name,dtype,nullable in cur.fetchall()}
    if any(found.get(k)!=("boolean","NO") for k in SUPPORT_COLUMNS):
        raise RuntimeError("Support staff schema migration has not been verified")


def verify_reviewed_support_difference(target, blocked):
    if set(blocked)!=set(REVIEWED_SUPPORT_BLOBS):
        raise RuntimeError("Unreviewed DB/dependency changes: "+", ".join(blocked[:15]))
    for filename,expected in REVIEWED_SUPPORT_BLOBS.items():
        blob=cmd("git","-C",str(bg.ROOT),"rev-parse",f"{target}:{filename}")
        if blob!=expected:
            raise RuntimeError("Reviewed support source changed unexpectedly: "+filename)
    verified_support_schema()



def cmd(*args,timeout=100):
    p=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
    if p.returncode:raise RuntimeError(f"{args[0]} failed ({p.returncode}): {p.stderr[-240:]}")
    return p.stdout.strip()


def audit(event,**kwargs):
    AUDIT.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    line=json.dumps({"time":datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "event":event,**kwargs},separators=(",",":"))
    with AUDIT.open("a") as file:
        os.chmod(AUDIT,0o600)
        file.write(line+"\n")
        file.flush()
        os.fsync(file.fileno())


def commits():
    active=admin.active_port()
    other=8082 if active==8081 else 8081
    sha=bg.target_commit()
    admin_sha=admin.release_for(active)
    bot_sha=bot.current_ref()
    for value in (sha,admin_sha,bot_sha):
        if not SHA_RE.fullmatch(value):raise RuntimeError("Invalid release SHA")
    return active,other,admin_sha,bot_sha,sha


def changed(old,new):
    if old==new:return []
    # Must be descendants, not disjoint histories or force-pushed ancestry.
    cmd("git","-C",str(bg.ROOT),"merge-base","--is-ancestor",old,new)
    return cmd("git","-C",str(bg.ROOT),"diff","--name-only",old,new).splitlines()


def validate(fetch=False, require_resources=True, reviewed_support=False):
    if fetch:
        cmd("git","-C",str(bg.ROOT),"fetch","origin","main",timeout=150)
    if bg.command("git","-C",str(bg.ROOT),"status","--porcelain"):
        raise RuntimeError("Git working tree must be clean")
    if bot.bot_state()!="active" or bot.current_pid()<=0:
        raise RuntimeError("Telegram polling service not active")
    pids=bot.poller_pids()
    if pids!=[bot.current_pid()]:
        raise RuntimeError(f"Exactly one supervised polling process required; observed {pids}")
    if admin.enabled(bg.LEGACY)!="disabled":
        raise RuntimeError("Old admin service must stay disabled")
    if any(admin.enabled(unit)!="enabled" for unit in bg.UNITS.values()):
        raise RuntimeError("Both Blue/Green units must be enabled")
    active,other,admin_sha,bot_sha,target=commits()
    if not bg.health(active) or not bg.health(other):
        raise RuntimeError("Both admin slots must be healthy before updating")
    if admin.service_state(bot.UNIT)!="active":
        raise RuntimeError("Telegram bot inactive")
    base=cmd("git","-C",str(bg.ROOT),"rev-parse","HEAD")
    admin_files=changed(admin_sha,target)
    bot_files=changed(bot_sha,target)
    base_files=changed(base,target)
    all_files=set(admin_files+bot_files+base_files)
    blocked=sorted(f for f in all_files if f.startswith(SCHEMA_PARTS)
                   or f.endswith(EXTRA_RISK) or f=="requirements.txt"
                   or f in ("pyproject.toml","poetry.lock","uv.lock",
                             "Pipfile.lock"))
    if blocked:
        if not reviewed_support:
            raise RuntimeError("Manual DB/dependency compatibility review required: "+
                               ", ".join(blocked[:15])+
                               ". Explicit reviewed-support-staff flag required for reviewed change.")
        verify_reviewed_support_difference(target, blocked)
    available_ram=bg.memory_mib()
    available_disk=shutil.disk_usage(bg.ROOT).free
    if require_resources and available_ram<220:
        raise RuntimeError(f"Less than 220 MiB RAM available ({available_ram} MiB); deployment blocked")
    if require_resources and available_disk<1024**3:
        raise RuntimeError("Less than 1 GiB disk available; deployment blocked")
    if not bot.ENV.is_file():raise RuntimeError("Missing shared secrets file")
    return dict(active=active,other=other,admin_sha=admin_sha,
                bot_sha=bot_sha,target=target,
                admin_changes=len(admin_files),bot_changes=len(bot_files),
                available_ram=available_ram,available_disk=available_disk,
                reviewed_support=bool(blocked))


def print_plan(plan):
    print("=== COORDINATED RELEASE CHECK ===")
    print("Active admin:",plan["active"],plan["admin_sha"][:12])
    print("Standby admin:",plan["other"],"healthy: True")
    print("Bot:",plan["bot_sha"][:12],"PID:",bot.current_pid(),
          "single polling process verified")
    print("Target origin/main:",plan["target"][:12])
    print("Changes relative to admin:",plan["admin_changes"],
          "relative to bot:",plan["bot_changes"])
    print("Database/dependency compatibility gate:",
          "PASS (reviewed support migration already applied; exact blobs verified)"
          if plan.get("reviewed_support") else "PASS (no tracked changes)")
    print("Available RAM:",plan["available_ram"],"MiB")
    print("Disk free:",plan["available_disk"]//(1024**2),"MiB")
    print("Deployment resource gate:",
          "PASS" if plan["available_ram"]>=220 and plan["available_disk"]>=1024**3
          else "BLOCKED (diagnostics available; do not deploy)")
    print("Neither check nor prepare restarts any services.")
    if plan["admin_sha"]==plan["target"] and plan["bot_sha"]==plan["target"]:
        print("NOOP: both components already use latest commit.")


def prepare_all(plan):
    sha=plan["target"]
    # This does not stop/restart systemd units, and does not write Nginx.
    release=bg.snapshot(sha)
    bg.ensure_venv(release)
    bot.prepare(sha)
    print("PREPARED BOTH:",sha[:12],"without service changes")


def backup_database():
    # Use the already-established backup and manifest verification tool.
    script=bg.ROOT/"scripts/reliability.py"
    if not script.is_file():raise RuntimeError("Database backup script unavailable")
    print("Creating fresh PostgreSQL backup before any live changes...",flush=True)
    cmd("/usr/bin/python3",str(script),"backup",timeout=300)
    cmd("/usr/bin/python3",str(script),"verify",timeout=60)
    print("Database archive created and manifest verified. Restore test not implied.")


def rollback_admin(previous,expected):
    if admin.active_port()==expected:
        admin.switch_back(expected,previous)
        audit("admin_auto_rollback",to_port=previous)
    elif admin.active_port()!=previous:
        raise RuntimeError("Unexpected Nginx route, cannot auto-rollback safely")


def deploy_all(plan):
    if plan["admin_sha"]==plan["target"] and plan["bot_sha"]==plan["target"]:
        print("NOOP: bot and admin are already at",plan["target"][:12])
        audit("noop",commit=plan["target"])
        return
    old=plan["active"]
    new=plan["other"]
    sha=plan["target"]
    audit("begin",target=sha,previous_admin=plan["admin_sha"],
          previous_bot=plan["bot_sha"],port=old)
    admin_switched=False
    bot_switched=False
    try:
        # Prepare and backup before any live changes.
        prepare_all(plan)
        backup_database()
        if admin.active_port()!=old or not bg.health(old):
            raise RuntimeError("Traffic changed during preparation; abort")
        if plan["admin_sha"]!=sha:
            bg.stage(sha,allow_bot_changes=True)
            if admin.release_for(new)!=sha or not bg.health(new):
                raise RuntimeError("New admin release did not pass staging verification")
            audit("admin_staged",target=sha,port=new)
            bg.cutover()
            admin_switched=True
            admin.public_health()
            audit("admin_switched",target=sha,port=new)
        # Stop/start the only poller only after admin is healthy.
        if plan["bot_sha"]!=sha:
            bot.deploy(sha)
            bot_switched=True
            if bot.current_ref()!=sha or bot.poller_pids()!=[bot.current_pid()]:
                raise RuntimeError("Bot post-deploy identity/poller validation failed")
        if admin.release_for(admin.active_port())!=sha:
            raise RuntimeError("Admin did not reach target commit")
        if not bg.health(admin.active_port()) or not bg.health(old):
            raise RuntimeError("Admin health failed after bot update")
        if bot.bot_state()!="active" or not bot.recent_polling(bot.current_pid()):
            raise RuntimeError("Telegram polling is not confirmed")
        admin.public_health()
        audit("success",target=sha,admin_port=admin.active_port(),
              bot_pid=bot.current_pid())
        print("SYSTEM UPDATE OK:",sha[:12],
              "admin port",admin.active_port(),"bot PID",bot.current_pid())
    except Exception as exc:
        audit("failed",target=sha,error=type(exc).__name__)
        if bot_switched:
            try:
                bot.rollback()
                audit("bot_auto_rollback",target=plan["bot_sha"])
            except Exception as err:
                print("CRITICAL: bot rollback unsuccessful:",type(err).__name__,file=sys.stderr)
                audit("bot_rollback_failed",reason=type(err).__name__)
        if admin_switched:
            try:
                rollback_admin(old,new)
            except Exception as err:
                print("CRITICAL: admin rollback unsuccessful:",type(err).__name__,file=sys.stderr)
                audit("admin_rollback_failed",reason=type(err).__name__)
        raise


def history():
    if not AUDIT.is_file():
        print("No system updates recorded")
        return
    for line in AUDIT.read_text().splitlines()[-20:]:
        try:
            entry=json.loads(line)
            print(entry.get("time",""),entry.get("event",""),
                  entry.get("target","")[:12])
        except ValueError:print("(invalid audit event)")


def main():
    parser=argparse.ArgumentParser(description="Conservative coordinated Telegram + admin update")
    parser.add_argument("action",choices=("check","prepare","deploy","history"))
    parser.add_argument("--reviewed-support-staff",action="store_true",
                        help="Permit only pinned reviewed support DB blobs after schema verification")
    args=parser.parse_args()
    if os.geteuid()!=0:raise RuntimeError("Run as root")
    if args.action=="history":
        history();return
    if args.action=="check":
        print_plan(validate(require_resources=False,reviewed_support=args.reviewed_support_staff));return
    LOCK.parent.mkdir(parents=True,exist_ok=True)
    with LOCK.open("a+") as file:
        fcntl.flock(file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        plan=validate(fetch=True,reviewed_support=args.reviewed_support_staff)
        print_plan(plan)
        if args.action=="prepare":
            prepare_all(plan)
        else:
            deploy_all(plan)


if __name__=="__main__":
    try:main()
    except Exception as exc:
        print("SYSTEM UPDATE REFUSED:",str(exc)[:430],file=sys.stderr)
        raise SystemExit(1)
