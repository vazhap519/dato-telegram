#!/usr/bin/env python3
"""Prepare pinned command centre admin release; no service or DB mutations."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT=Path("/opt/dato-telegram")
RELEASES=Path("/opt/dato-admin-releases")
BASE="da7e07eaf7e79cf7228842efa4c77df0bec0ac45"
NEW="a6eee074d8088ef98819f6777931cd2936148197"
ALLOWED={".github/workflows/command-center.yml",
         "admin/support_miniapp.py",
         "admin/templates/analytics_miniapp.html",
         "admin/templates/command_center_miniapp.html",
         "admin/templates/members_miniapp.html",
         "admin/templates/support_miniapp.html",
         "db/miniapp_command_center.py",
         "scripts/cutover_owner_alert_feedback_8082.py",
         "scripts/stage_owner_alert_feedback_8082.py",
         "tests/test_command_center_miniapp.py"}

def git(*args):
    return subprocess.check_output(["git","-C",str(ROOT),*args],text=True,timeout=30).strip()

def main():
    if os.geteuid()!=0:raise RuntimeError("Root required")
    if git("status","--porcelain"):raise RuntimeError("Production working tree modified")
    if git("rev-parse","--verify",NEW)!=NEW:
        raise RuntimeError("Pinned commit missing: fetch feature branch first")
    if subprocess.run(["git","-C",str(ROOT),"merge-base","--is-ancestor",BASE,NEW],timeout=20).returncode:
        raise RuntimeError("Candidate diverged from published release")
    changes=set(git("diff","--name-only",BASE,NEW).splitlines())
    if changes!=ALLOWED:
        raise RuntimeError("Unexpected file differences: "+str(sorted(changes.symmetric_difference(ALLOWED))))
    if not (ROOT/".env").is_file():raise RuntimeError("Runtime env missing")
    ram=next(int(l.split()[1])//1024 for l in Path("/proc/meminfo").read_text().splitlines()
             if l.startswith("MemAvailable:"))
    disk=shutil.disk_usage(ROOT).free//1048576
    print("COMMAND CENTRE CANDIDATE:",NEW,"RAM:",ram,"MiB; Disk:",disk,"MiB",flush=True)
    if ram<180 or disk<1500:raise RuntimeError("Insufficient headroom")
    sys.path.insert(0,str(RELEASES/BASE/"scripts"))
    import deploy_admin_bluegreen as bg
    release=bg.snapshot(NEW)
    python=bg.ensure_venv(release)
    for test in ("test_command_center_miniapp.py","test_owner_alerts.py",
                 "test_miniapp_analytics.py","test_members_miniapp.py",
                 "test_support_permissions.py"):
        subprocess.run([str(python),"-m","unittest","discover",
                        "-s",str(release/"tests"),"-p",test,"-v"],
                       cwd=release,check=True,timeout=120)
    if shutil.which("node"):
        for name in ("command_center_miniapp.html","support_miniapp.html",
                     "members_miniapp.html","analytics_miniapp.html"):
            scripts=[v for v in re.findall(r"<script>(.*?)</script>",
                       (release/"admin/templates"/name).read_text(),re.S) if v.strip()]
            if len(scripts)!=1:raise RuntimeError("Bad JS blocks in "+name)
            with tempfile.NamedTemporaryFile("w",suffix=".js",delete=False) as f:
                f.write(scripts[0]);tmp=f.name
            try:subprocess.run(["node","--check",tmp],check=True,timeout=15)
            finally:Path(tmp).unlink(missing_ok=True)
    # Execute actual production SELECT statements inside a READ ONLY session.
    probe="""
import asyncio
from dotenv import load_dotenv
load_dotenv('/opt/dato-telegram/.env',override=True)
from db.miniapp_command_center import overview
async def check():
    for privileged in (False,True):
        r=await asyncio.wait_for(overview(include_members=privileged),timeout=25)
        assert r['support']['open']>=0
        assert len(r['urgent_tickets'])<=6
        assert (r['members'] is not None)==privileged
        assert (r['paid_events_30d'] is not None)==privileged
        print('LIVE READ-ONLY HUB QUERY OK: privileged=',privileged,
              'open_tickets=',r['support']['open'],flush=True)
asyncio.run(check())
"""
    subprocess.run([str(python),"-c",probe],cwd=release,check=True,timeout=65)
    print("COMMAND CENTRE RELEASE PREPARED:",NEW,flush=True)
    print("No migrations, no Nginx changes, no bot or alert timer restart.",flush=True)

if __name__=="__main__":
    try:main()
    except Exception as exc:
        print("COMMAND CENTRE PREP STOPPED:",type(exc).__name__,
              str(exc).splitlines()[0][:350],file=sys.stderr)
        sys.exit(1)
