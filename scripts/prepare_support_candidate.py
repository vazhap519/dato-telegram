#!/usr/bin/env python3
"""Prepare a candidate release on disk without changing any live service.

Invoked only after the candidate-specific read-only preflight passes.
Never checks out the live working tree, modifies systemd/Nginx, starts a
polling process, touches database schema, or switches web traffic.
"""
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT=Path("/opt/dato-telegram")
RELEASES=Path("/opt/dato-admin-releases")
BRANCH="refs/remotes/origin/feature/support-staff-permissions-20261113"
MIN_RAM_MIB=300
MIN_DISK_BYTES=2*1024**3

def run(args,timeout=550):
    return subprocess.run(args,check=True,timeout=timeout,text=True)

def output(args):
    return subprocess.check_output(args,timeout=30,text=True).strip()

def available_ram():
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1])//1024
    raise RuntimeError("Cannot read MemAvailable")

def main():
    if os.geteuid()!=0:
        raise RuntimeError("Run as root")
    if not (ROOT/".env").is_file():
        raise RuntimeError("Shared configuration missing")
    if output(["git","-C",str(ROOT),"status","--porcelain"]):
        raise RuntimeError("Live Git working tree is modified")
    sha=output(["git","-C",str(ROOT),"rev-parse","--verify",BRANCH])
    if not re.fullmatch("[a-f0-9]{40}",sha):
        raise RuntimeError("Invalid candidate commit")
    if output(["git","-C",str(ROOT),"merge-base","--is-ancestor",
               "HEAD",sha]) not in ("",):
        raise RuntimeError("Candidate ancestry check returned unexpected data")
    available=available_ram()
    free=shutil.disk_usage(ROOT).free
    print("CANDIDATE:",sha,flush=True)
    print("Available RAM:",available,"MiB. Free disk:",free//(1024**2),"MiB",flush=True)
    if available<MIN_RAM_MIB or free<MIN_DISK_BYTES:
        raise RuntimeError("Resource gate not met for isolated dependency installation")
    # Import the exact candidate snapshot builder from an isolated temporary
    # directory. The live /opt project may not contain the installed updater.
    # This code path uses only snapshot() and ensure_venv(): no stage/cutover.
    with tempfile.TemporaryDirectory(prefix="dato-support-build-") as tmp:
        source=subprocess.check_output(
            ["git","-C",str(ROOT),"show",f"{sha}:scripts/deploy_admin_bluegreen.py"],
            timeout=30,
        )
        module=Path(tmp)/"deploy_admin_bluegreen.py"
        module.write_bytes(source)
        sys.path.insert(0,tmp)
        try:
            import deploy_admin_bluegreen as bg
            release=bg.snapshot(sha)
            if release!=RELEASES/sha:
                raise RuntimeError("Unexpected release directory")
            python=bg.ensure_venv(release)
        finally:
            sys.path.remove(tmp)
    if not python.is_file():
        raise RuntimeError("Admin virtual environment was not built")
    print("ADMIN PREPARED:",python,flush=True)
    if available_ram()<180:
        raise RuntimeError("Insufficient RAM to install bot environment")
    bot_python=release/".bot-venv/bin/python"
    marker=release/".bot-venv/.prepared"
    expected=hashlib.sha256((release/"requirements.txt").read_bytes()).hexdigest()
    if not bot_python.is_file():
        run(["/usr/bin/python3","-m","venv",str(release/".bot-venv")],timeout=130)
    if not marker.is_file() or marker.read_text().strip()!=expected:
        print("Installing isolated bot dependencies...",flush=True)
        run([str(bot_python),"-m","pip","install","-r",
             str(release/"requirements.txt")])
        marker.write_text(expected)
    run([str(bot_python),"-m","compileall","-q",str(release/"bot"),
         str(release/"db"),str(release/"scripts")],timeout=110)
    run([str(python),"-m","unittest","discover","-s",
         str(release/"tests"),"-p","test_support_permissions.py","-v"],timeout=90)
    print("ISOLATED CANDIDATE READY:",sha,flush=True)
    print("No live service, database, or Nginx changes were made.",flush=True)

if __name__=="__main__":
    try:
        main()
    except Exception as exc:
        print("PREPARATION STOPPED:",type(exc).__name__,str(exc).splitlines()[0][:250],file=sys.stderr)
        sys.exit(1)
