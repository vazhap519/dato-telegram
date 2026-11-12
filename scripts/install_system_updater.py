#!/usr/bin/env python3
"""Install a separate coordinated system updater; dry-check by default.

Copies pinned trusted files from fetched origin/main. Does not alter currently
installed update-dato-admin, update-dato-bot, or old update-dato-telegram.
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
DEST=Path("/usr/local/lib/dato-system-update")
WRAPPER=Path("/usr/local/bin/update-dato-system")
FILES=("update_system_safe.py","deploy_admin_bluegreen.py",
       "update_admin_safe.py","update_bot_safe.py")


def git(*arguments):
    p=subprocess.run(("git","-C",str(ROOT),*arguments),capture_output=True,
                     timeout=50,check=True)
    return p.stdout


def write(path,data,mode):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix=".dato-system-",dir=path.parent)
    try:
        os.fchmod(fd,mode)
        with os.fdopen(fd,"wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--install",action="store_true")
    a=parser.parse_args()
    if os.geteuid()!=0:raise RuntimeError("Run as root")
    sha=git("rev-parse","origin/main").decode().strip()
    if not re.fullmatch("[a-f0-9]{40}",sha):
        raise RuntimeError("Malformed origin/main commit")
    contents={}
    for name in FILES:
        data=git("show",f"{sha}:scripts/{name}")
        compile(data.decode("utf-8"),name,"exec")
        contents[name]=data
    if not (ROOT/".venv/bin/python").is_file():
        raise RuntimeError("Shared project Python missing")
    print("Source commit:",sha[:12])
    print("New command:",WRAPPER)
    print("All existing updater commands and services stay unchanged.")
    if not a.install:
        print("DRY CHECK OK. Use --install to install the separate updater.")
        return
    DEST.mkdir(parents=True,mode=0o700,exist_ok=True)
    DEST.chmod(0o700)
    backup=DEST/("backup-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    backup.mkdir(mode=0o700)
    for name in FILES:
        if (DEST/name).exists():shutil.copy2(DEST/name,backup/name)
    if WRAPPER.exists():shutil.copy2(WRAPPER,backup/"update-dato-system")
    try:
        for name,data in contents.items():
            write(DEST/name,data,0o600)
        wrapper=(b"#!/bin/sh\nset -eu\n"
                 b"exec /opt/dato-telegram/.venv/bin/python "
                 b"/usr/local/lib/dato-system-update/update_system_safe.py \"$@\"\n")
        write(WRAPPER,wrapper,0o755)
        write(DEST/"installed_commit",sha.encode()+b"\n",0o600)
    except Exception:
        for name in FILES:
            previous=backup/name
            if previous.exists():shutil.copy2(previous,DEST/name)
            else:(DEST/name).unlink(missing_ok=True)
        previous=backup/"update-dato-system"
        if previous.exists():shutil.copy2(previous,WRAPPER)
        else:WRAPPER.unlink(missing_ok=True)
        raise
    print("INSTALLED: update-dato-system")
    print("Run FIRST: update-dato-system check")
    print("Then, separately: update-dato-system prepare")
    print("Never run deploy without reviewing the change report.")


if __name__=="__main__":
    try:main()
    except Exception as exc:
        print("SYSTEM INSTALL FAILED:",str(exc)[:320],file=sys.stderr)
        raise SystemExit(1)
