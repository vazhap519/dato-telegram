#!/usr/bin/env python3
"""Send a verified local PostgreSQL archive to an opt-in rclone crypt remote.

This script never changes production DB and never deletes remote backups.
Requires rclone remote encryption to have been configured independently.
"""
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys

BACKUPS = Path(os.getenv("DATO_BACKUP_DIR", "/var/backups/dato-telegram"))
STATE = Path(os.getenv("DATO_RELIABILITY_STATE_DIR", "/var/lib/dato-reliability"))
DEST = os.getenv("DATO_OFFSITE_REMOTE", "").strip()
MAX_AGE_HOURS = 48

def cmd(argv,timeout=180):
    return subprocess.run(argv,check=True,capture_output=True,text=True,timeout=timeout)

def main():
    if not DEST or ":" not in DEST or DEST.startswith(":"):
        raise RuntimeError("Configure DATO_OFFSITE_REMOTE to an rclone crypt remote first")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,70}:[A-Za-z0-9_./-]*",DEST):
        raise RuntimeError("Invalid rclone remote/path")
    source=max(BACKUPS.glob("dato-*.dump"),key=lambda p:p.stat().st_mtime,default=None)
    if source is None or source.stat().st_size<100:
        raise RuntimeError("No valid local PostgreSQL archive")
    age=(dt.datetime.now(dt.timezone.utc).timestamp()-source.stat().st_mtime)/3600
    if age>MAX_AGE_HOURS:
        raise RuntimeError("Latest local archive is too old")
    cmd(["pg_restore","--list",str(source)],timeout=40)
    remote=DEST.rstrip("/")+"/"+source.name
    cmd(["rclone","copyto","--immutable",str(source),remote,
         "--retries","3","--low-level-retries","3"],timeout=900)
    remote_stat=json.loads(cmd(["rclone","lsjson","--stat",remote],timeout=120).stdout)
    if remote_stat.get("IsDir") or int(remote_stat.get("Size",-1))!=source.stat().st_size:
        raise RuntimeError("Remote archive size verification failed")
    STATE.mkdir(parents=True,exist_ok=True,mode=0o700)
    statefile=STATE/"offsite-last-success.json"
    temp=STATE/"offsite-last-success.tmp"
    payload={"completed_at":dt.datetime.now(dt.timezone.utc).isoformat(),
             "archive":source.name,"bytes":source.stat().st_size}
    temp.write_text(json.dumps(payload),encoding="utf-8")
    temp.chmod(0o600)
    temp.replace(statefile)
    print("Offsite archive verified:",source.name,"bytes:",source.stat().st_size)
    return 0

if __name__=="__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("Offsite backup failed:",type(exc).__name__,str(exc)[:250],file=sys.stderr)
        sys.exit(1)
