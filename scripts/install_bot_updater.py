#!/usr/bin/env python3
"""Install independent Telegram bot updater from committed GitHub source.

No bot restart, no Nginx/systemd changes, no install without --install.
Does not replace legacy update-dato-telegram or admin blue/green updater.
"""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT=Path("/opt/dato-telegram")
DEST=Path("/usr/local/lib/dato-bot-update")
COMMAND=Path("/usr/local/bin/update-dato-bot")
FILES=("update_bot_safe.py",)


def git(*args):
    return subprocess.run(["git","-C",str(ROOT),*args],capture_output=True,
                          timeout=35,check=True).stdout


def write_atomic(path, content, mode):
    fd,temp=tempfile.mkstemp(prefix=".dato-install-",dir=path.parent)
    try:
        os.fchmod(fd,mode)
        with os.fdopen(fd,"wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--install",action="store_true")
    args=parser.parse_args()
    if os.geteuid()!=0:raise RuntimeError("Must run as root")
    sha=git("rev-parse","origin/main").decode().strip()
    if not re.fullmatch("[a-f0-9]{40}",sha):
        raise RuntimeError("Unexpected origin/main SHA")
    sources={}
    for filename in FILES:
        sources[filename]=git("show",f"{sha}:scripts/{filename}")
        compile(sources[filename].decode(),"scripts/"+filename,"exec")
    runner=git("show",f"{sha}:scripts/bot_runner.py")
    compile(runner.decode(),"scripts/bot_runner.py","exec")
    if not (ROOT/".venv/bin/python").is_file():
        raise RuntimeError("Existing project virtualenv not found")
    print("Validated GitHub source:",sha[:12])
    print("New command:",COMMAND)
    print("Legacy updater and all running services stay unchanged.")
    if not args.install:
        print("DRY CHECK PASSED. Add --install to install command.")
        return
    DEST.mkdir(parents=True,mode=0o700,exist_ok=True)
    DEST.chmod(0o700)
    backup=DEST/("backup-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    backup.mkdir(mode=0o700)
    for name in FILES:
        if (DEST/name).exists():shutil.copy2(DEST/name,backup/name)
    if COMMAND.exists():shutil.copy2(COMMAND,backup/"update-dato-bot")
    try:
        for name,content in sources.items():
            write_atomic(DEST/name,content,0o600)
        wrapper=(b"#!/bin/sh\nset -eu\n"
                 b"exec /opt/dato-telegram/.venv/bin/python "
                 b"/usr/local/lib/dato-bot-update/update_bot_safe.py \"$@\"\n")
        write_atomic(COMMAND,wrapper,0o755)
        write_atomic(DEST/"installed_commit",sha.encode()+b"\n",0o600)
    except Exception:
        for name in FILES:
            previous=backup/name
            if previous.exists():shutil.copy2(previous,DEST/name)
            else:(DEST/name).unlink(missing_ok=True)
        previous=backup/"update-dato-bot"
        if previous.exists():shutil.copy2(previous,COMMAND)
        else:COMMAND.unlink(missing_ok=True)
        raise
    print("INSTALLED: update-dato-bot")
    print("Use: update-dato-bot check")
    print("First run: update-dato-bot prepare")
    print("Do NOT run deploy until the preparation report is reviewed.")


if __name__=="__main__":
    try:main()
    except Exception as exc:
        print("INSTALL FAILED:",str(exc)[:300],file=sys.stderr)
        raise SystemExit(1)
