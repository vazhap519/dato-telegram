#!/usr/bin/env python3
"""Build pinned owner alert release without touching any services or data."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path("/opt/dato-telegram")
RELEASES=Path("/opt/dato-admin-releases")
BASE="800b69ffa6790184467800ad40d0bef0174e0b64"
NEW="c1792778c7441dbec7c3efd8d829227493e83c55"
ALLOWED={".github/workflows/owner-alerts.yml",
         "admin/members_miniapp.py","admin/templates/analytics_miniapp.html",
         "db/owner_alerts.py","scripts/owner_alert_worker.py",
         "scripts/prepare_miniapp_analytics.py",
         "scripts/stage_miniapp_analytics_8082.py",
         "scripts/cutover_miniapp_analytics_8082.py",
         "tests/test_owner_alerts.py"}

def command(*args):
    return subprocess.check_output(args,text=True,timeout=30).strip()

def main():
    if os.geteuid()!=0:raise RuntimeError("Run as root")
    if command("git","-C",str(ROOT),"status","--porcelain"):
        raise RuntimeError("Current Git checkout not clean")
    if command("git","-C",str(ROOT),"rev-parse","--verify",NEW)!=NEW:
        raise RuntimeError("Fetch the pinned branch before preparing release")
    if subprocess.run(["git","-C",str(ROOT),"merge-base","--is-ancestor",BASE,NEW],
                      timeout=20).returncode:
        raise RuntimeError("Pinned candidate diverges from live release")
    changed=set(command("git","-C",str(ROOT),"diff","--name-only",BASE,NEW).splitlines())
    if changed!=ALLOWED:
        raise RuntimeError("Unreviewed diff: "+str(sorted(changed.symmetric_difference(ALLOWED))))
    available=next(int(line.split()[1])//1024 for line in Path("/proc/meminfo").read_text().splitlines()
                   if line.startswith("MemAvailable:"))
    free=shutil.disk_usage(ROOT).free//1048576
    print("OWNER ALERT CANDIDATE:",NEW,flush=True)
    print("Available RAM:",available,"MiB; disk:",free,"MiB",flush=True)
    if available<200 or free<2048:raise RuntimeError("Not enough headroom to prepare isolated release")
    if not (ROOT/".env").is_file():raise RuntimeError("Runtime .env missing")
    sys.path.insert(0,str(RELEASES/BASE/"scripts"))
    import deploy_admin_bluegreen as bg
    release=bg.snapshot(NEW)
    python=bg.ensure_venv(release)
    for pattern in ("test_owner_alerts.py","test_miniapp_analytics.py",
                    "test_members_miniapp.py","test_support_permissions.py"):
        subprocess.run([str(python),"-m","unittest","discover",
                        "-s",str(release/"tests"),"-p",pattern,"-v"],
                       check=True,cwd=release,timeout=120)
    if shutil.which("node"):
        import re
        from tempfile import NamedTemporaryFile
        html=(release/"admin/templates/analytics_miniapp.html").read_text()
        js=[m for m in re.findall(r"<script>(.*?)</script>",html,re.S) if m.strip()]
        if len(js)!=1:raise RuntimeError("Unexpected analytics UI JS structure")
        with NamedTemporaryFile(mode="w",suffix=".js",delete=False) as tmp:
            tmp.write(js[0]);jsfile=tmp.name
        try:subprocess.run(["node","--check",jsfile],check=True,timeout=15)
        finally:Path(jsfile).unlink(missing_ok=True)
    print("OWNER ALERT RELEASE PREPARED:",NEW,flush=True)
    print("No DB migration, Telegram send, bot restart, service or Nginx change.")

if __name__=="__main__":
    try:main()
    except Exception as exc:
        print("OWNER ALERT PREP STOPPED:",type(exc).__name__,str(exc).splitlines()[0][:350],file=sys.stderr)
        sys.exit(1)
