#!/usr/bin/env python3
"""Prepare the pinned analytics release and execute read-only live SQL smoke tests.

No service restart, Nginx change, Telegram send or database mutation.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path("/opt/dato-telegram")
RELEASES=Path("/opt/dato-admin-releases")
BASE="121b4ef32ec4bff530781df415cc966ced99ab12"
NEW="800b69ffa6790184467800ad40d0bef0174e0b64"
EXPECTED={".github/workflows/miniapp-analytics.yml","admin/members_miniapp.py",
          "admin/templates/analytics_miniapp.html","admin/templates/members_miniapp.html",
          "admin/templates/support_miniapp.html","db/miniapp_analytics.py",
          "scripts/cutover_member_actions_8081.py",
          "scripts/prepare_member_actions_candidate.py",
          "scripts/stage_member_actions_8081.py","tests/test_miniapp_analytics.py"}

def run(*args):
    return subprocess.check_output(args,text=True,timeout=30).strip()

def main():
    if os.geteuid()!=0:raise RuntimeError("Root required")
    if run("git","-C",str(ROOT),"status","--porcelain"):
        raise RuntimeError("Live working tree changed")
    if run("git","-C",str(ROOT),"rev-parse","--verify",NEW)!=NEW:
        raise RuntimeError("Fetch the pinned analytics commit first")
    if subprocess.run(["git","-C",str(ROOT),"merge-base","--is-ancestor",BASE,NEW],timeout=20).returncode:
        raise RuntimeError("Analytics candidate does not descend from approved live release")
    diff=set(run("git","-C",str(ROOT),"diff","--name-only",BASE,NEW).splitlines())
    if diff!=EXPECTED:
        raise RuntimeError("Unreviewed changes: "+str(sorted(diff.symmetric_difference(EXPECTED))))
    available=next(int(row.split()[1])//1024 for row in Path("/proc/meminfo").read_text().splitlines()
                   if row.startswith("MemAvailable:"))
    disk=shutil.disk_usage(ROOT).free//1048576
    print("ANALYTICS CANDIDATE:",NEW,flush=True)
    print("Available RAM:",available,"MiB; disk:",disk,"MiB",flush=True)
    if available<250 or disk<2048:
        raise RuntimeError("Insufficient resources to prepare isolated environment")
    if not (ROOT/".env").is_file():raise RuntimeError("Existing runtime environment missing")
    sys.path.insert(0,str(RELEASES/BASE/"scripts"))
    import deploy_admin_bluegreen as bg
    release=bg.snapshot(NEW)
    python=bg.ensure_venv(release)
    for pattern in ("test_miniapp_analytics.py","test_members_miniapp.py","test_support_permissions.py"):
        subprocess.run([str(python),"-m","unittest","discover","-s",
                        str(release/"tests"),"-p",pattern,"-v"],cwd=release,check=True,timeout=120)
    # All analytic SQL executes against a read-only repeatable-read PostgreSQL
    # transaction. Proves the production schema, indexes and joins are viable.
    smoke=r"""
import asyncio
from dotenv import load_dotenv
load_dotenv('/opt/dato-telegram/.env')
from db.miniapp_analytics import report
async def check():
    for p in ('30d','all'):
        result=await asyncio.wait_for(report(p),timeout=25)
        assert result['members']['active'] <= result['members']['registered']
        assert result['members']['lifetime'] <= result['members']['active']
        assert result['members']['active_subscribers_with_paid_receipt'] <= result['members']['active_subscription_users']
        assert result['members']['expiring_7d'] <= result['members']['active']
        assert len(result['expiring']) <= 100
        print('READ ONLY SQL OK:',p,'registered:',result['members']['registered'],
              'active:',result['members']['active'],'payments:',result['payments']['paid_events'],flush=True)
asyncio.run(check())
"""
    print("Checking REAL DB analytics using READ ONLY transactions...",flush=True)
    subprocess.run([str(python),"-c",smoke],cwd=release,check=True,timeout=70)
    print("ANALYTICS RELEASE PREPARED:",NEW,flush=True)
    print("Bot, Nginx, services and tables have not changed.",flush=True)

if __name__=="__main__":
    try:main()
    except Exception as exc:
        print("ANALYTICS PREP STOPPED:",type(exc).__name__,str(exc).splitlines()[0][:350],file=sys.stderr)
        sys.exit(1)
