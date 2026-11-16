#!/usr/bin/env python3
"""Prepare the membership Mini App candidate without touching any live service.

Uses the pinned, verified existing admin snapshot builder to create an
immutable release under /opt/dato-admin-releases. Does not restart services,
change Nginx, run application startup or alter PostgreSQL.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path("/opt/dato-telegram")
RELEASES=Path("/opt/dato-admin-releases")
BRANCH="refs/remotes/origin/feature/miniapp-members-subscriptions-20261116"
PUBLISHED="2d609a3267261e2370e01e02596916d1bdc044c6"
ALLOWED={
    "admin/main.py",
    "admin/members_miniapp.py",
    "admin/templates/members_miniapp.html",
    "admin/templates/support_miniapp.html",
    "tests/test_members_miniapp.py",
    ".github/workflows/members-miniapp.yml",
    "scripts/check_members_branch_readonly.sh",
    "scripts/prepare_members_candidate.py",
}

def git(*args):
    return subprocess.check_output(["git","-C",str(ROOT),*args],text=True,timeout=30).strip()

def main():
    if os.geteuid()!=0:raise RuntimeError("Root required")
    if git("status","--porcelain"):
        raise RuntimeError("Live Git working tree must be clean")
    sha=git("rev-parse","--verify",BRANCH)
    subprocess.run(["git","-C",str(ROOT),"merge-base","--is-ancestor",PUBLISHED,sha],
                   check=True,timeout=20)
    changed=set(git("diff","--name-only",PUBLISHED,sha).splitlines())
    extra=changed-ALLOWED
    if extra:
        raise RuntimeError("Unreviewed files: "+", ".join(sorted(extra)))
    if not {"admin/main.py","admin/members_miniapp.py"}.issubset(changed):
        raise RuntimeError("Expected feature files not found")
    memory=next(int(l.split()[1])//1024 for l in Path("/proc/meminfo").read_text().splitlines() if l.startswith("MemAvailable:"))
    free=shutil.disk_usage(ROOT).free//(1024*1024)
    print("PREPARING MEMBERS CANDIDATE:",sha,flush=True)
    print("RAM available:",memory,"MiB; disk available:",free,"MiB",flush=True)
    if memory<280 or free<2048:
        raise RuntimeError("Insufficient preparation resources")
    # Import only trusted functions from immutable already running release.
    sys.path.insert(0,str(RELEASES/PUBLISHED/"scripts"))
    import deploy_admin_bluegreen as bg
    release=bg.snapshot(sha)
    if release!=RELEASES/sha:raise RuntimeError("Unexpected release path")
    py=bg.ensure_venv(release)
    if not py.is_file():raise RuntimeError("Admin venv missing")
    for pattern in ("test_members_miniapp.py","test_support_permissions.py"):
        subprocess.run([str(py),"-m","unittest","discover","-s",
                       str(release/"tests"),"-p",pattern,"-v"],check=True,timeout=90)
    print("MEMBERS CANDIDATE PREPARED:",sha,flush=True)
    print("No running service, Nginx, or database changes made.")

if __name__=="__main__":
    try:main()
    except Exception as exc:
        print("PREPARE STOPPED:",type(exc).__name__,str(exc).splitlines()[0][:250],file=sys.stderr)
        sys.exit(1)
