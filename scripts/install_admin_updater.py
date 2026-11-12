#!/usr/bin/env python3
"""Install admin-only blue/green updater from the fetched GitHub commit.

Does not overwrite /usr/local/bin/update-dato-telegram, restart services,
change Nginx or run the deployment. Read-only by default. Explicit --install.
"""
import argparse
from datetime import datetime,timezone
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT=Path("/opt/dato-telegram")
INSTALL=Path("/usr/local/lib/dato-admin-update")
COMMAND=Path("/usr/local/bin/update-dato-admin")
SCRIPTS=("deploy_admin_bluegreen.py","update_admin_safe.py")


def run(*args):
    proc=subprocess.run(args,capture_output=True,check=True,timeout=45)
    return proc.stdout


def put(path, content, mode):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=".dato-update-",dir=path.parent)
    try:
        os.fchmod(fd,mode)
        with os.fdopen(fd,"wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--install",action="store_true")
    args=p.parse_args()
    if os.geteuid()!=0:raise RuntimeError("Must run as root")
    sha=run("git","-C",str(ROOT),"rev-parse","origin/main").decode().strip()
    if not re.fullmatch("[a-f0-9]{40}",sha):raise RuntimeError("Unexpected commit")
    sources={}
    for name in SCRIPTS:
        sources[name]=run("git","-C",str(ROOT),"show",f"{sha}:scripts/{name}")
        compile(sources[name].decode(),"scripts/"+name,"exec")
    print("Validated updater sources from commit:",sha[:12])
    print("Install:",INSTALL)
    print("New command:",COMMAND)
    print("Old /usr/local/bin/update-dato-telegram remains unchanged.")
    print("No Nginx, systemd or Telegram bot changes.")
    if not args.install:
        print("CHECK ONLY: add --install to create the separate admin updater")
        return
    if not (ROOT/".venv/bin/python").is_file():
        raise RuntimeError("Project Python missing")
    if not COMMAND.parent.is_dir():
        raise RuntimeError("/usr/local/bin missing")
    if INSTALL.exists() and not INSTALL.is_dir():
        raise RuntimeError("Unexpected updater target")
    INSTALL.mkdir(mode=0o700,parents=True,exist_ok=True)
    os.chmod(INSTALL,0o700)
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup=INSTALL/("backup-"+stamp)
    backup.mkdir(mode=0o700)
    for name in SCRIPTS:
        current=INSTALL/name
        if current.exists():
            shutil.copy2(current,backup/name)
    if COMMAND.exists():
        shutil.copy2(COMMAND,backup/"update-dato-admin")
    try:
        for name,bytes_ in sources.items():
            put(INSTALL/name,bytes_,0o600)
        wrapper=b'#!/bin/sh\nset -eu\nexec /opt/dato-telegram/.venv/bin/python /usr/local/lib/dato-admin-update/update_admin_safe.py "$@"\n'
        put(COMMAND,wrapper,0o755)
        put(INSTALL/"installed_commit",sha.encode()+b"\n",0o600)
    except Exception:
        # Restore previous installed version when available.
        for name in SCRIPTS:
            old=backup/name
            if old.exists():shutil.copy2(old,INSTALL/name)
            else:(INSTALL/name).unlink(missing_ok=True)
        old_cmd=backup/"update-dato-admin"
        if old_cmd.exists():shutil.copy2(old_cmd,COMMAND)
        else:COMMAND.unlink(missing_ok=True)
        raise
    print("INSTALLED: update-dato-admin (admin only).")
    print("First run: update-dato-admin check")
    print("Deploy later: update-dato-admin deploy")
    print("History: update-dato-admin history")
    print("Rollback: update-dato-admin rollback")


if __name__=="__main__":
    try:main()
    except Exception as exc:
        print("INSTALL FAILED:",str(exc)[:250],file=sys.stderr)
        raise SystemExit(1)
