#!/usr/bin/env python3
"""Build the pinned membership management release; never touch live services."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path("/opt/dato-telegram")
RELEASES=Path("/opt/dato-admin-releases")
BASE="32ae988990a378985f0b8a4c4b3dadd6ab66ef14"
CANDIDATE="121b4ef32ec4bff530781df415cc966ced99ab12"
ALLOWED={"admin/members_miniapp.py","admin/templates/members_miniapp.html",
         "tests/test_members_miniapp.py","scripts/check_members_branch_readonly.sh"}

def git(*args):
    return subprocess.check_output(["git","-C",str(ROOT),*args],text=True,timeout=25).strip()

def main():
    if os.geteuid()!=0:raise RuntimeError("Root required")
    if git("status","--porcelain"):raise RuntimeError("Live Git working tree must be clean")
    if git("rev-parse","--verify",CANDIDATE)!=CANDIDATE:
        raise RuntimeError("Pinned SHA unavailable")
    if subprocess.run(["git","-C",str(ROOT),"merge-base","--is-ancestor",BASE,CANDIDATE],
                      timeout=20).returncode:
        raise RuntimeError("Pinned candidate does not descend from approved base")
    changed=set(git("diff","--name-only",BASE,CANDIDATE).splitlines())
    if changed!=ALLOWED:
        raise RuntimeError("Unexpected diff: "+str(sorted(changed.symmetric_difference(ALLOWED))))
    if not (ROOT/".env").is_file():raise RuntimeError("Missing runtime .env")
    available=next(int(l.split()[1])//1024 for l in Path("/proc/meminfo").read_text().splitlines()
                   if l.startswith("MemAvailable:"))
    disk=shutil.disk_usage(ROOT).free//(1024*1024)
    print("MEMBER ACTIONS CANDIDATE:",CANDIDATE,flush=True)
    print("Memory:",available,"MiB; disk:",disk,"MiB",flush=True)
    if available<250 or disk<2048:
        raise RuntimeError("Insufficient resources for isolated dependency preparation")
    # The prior published release is immutable and contains the reviewed
    # snapshot and isolated environment helpers; never import from live checkout.
    sys.path.insert(0,str(RELEASES/"f7ea20273c15e170bb58f8eaa97341e84974ec77"/"scripts"))
    import deploy_admin_bluegreen as bg
    release=bg.snapshot(CANDIDATE)
    if release!=RELEASES/CANDIDATE:raise RuntimeError("Unexpected release directory")
    python=bg.ensure_venv(release)
    if not python.is_file():raise RuntimeError("Candidate Python not prepared")
    for pattern in ("test_members_miniapp.py","test_support_permissions.py"):
        subprocess.run([str(python),"-m","unittest","discover","-s",
                        str(release/"tests"),"-p",pattern,"-v"],check=True,timeout=100)
    print("MEMBER ACTIONS RELEASE PREPARED:",CANDIDATE,flush=True)
    print("No live service, Nginx, bot or database change.")

if __name__=="__main__":
    try:main()
    except Exception as exc:
        print("MEMBER ACTIONS PREP STOPPED:",type(exc).__name__,str(exc).splitlines()[0][:320],file=sys.stderr)
        sys.exit(1)
