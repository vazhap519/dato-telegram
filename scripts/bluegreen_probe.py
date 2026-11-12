#!/usr/bin/env python3
"""Safely probe a separate FastAPI admin release on localhost:8082.

No Nginx change, no systemd edit, no bot restart. The test process is stopped
on completion, including on errors. The admin app's normal startup may execute
its existing idempotent database initializers.
"""
import argparse
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from io import BytesIO

BASE = Path("/opt/dato-telegram")
RELEASES = Path("/opt/dato-admin-releases")
PYTHON = BASE / ".venv/bin/python"
PORT = 8082
MAX_SECONDS = 65


def output(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs).stdout.strip()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--confirm",action="store_true",
                        help="Explicitly permit starting a second admin app against the production DB")
    args=parser.parse_args()
    if os.geteuid()!=0:
        raise SystemExit("Run as root")
    if not BASE.is_dir() or not PYTHON.is_file() or not (BASE/".env").is_file():
        raise SystemExit("Missing live repo, venv, or .env; no action taken")
    if shutil.disk_usage(BASE).free < 1024**3:
        raise SystemExit("Less than 1 GiB disk space; no action taken")
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1",PORT))==0:
            raise SystemExit("Port 8082 is in use; no action taken")
    commit=output("git","-C",str(BASE),"rev-parse","origin/main")
    if len(commit)!=40 or not all(x in "0123456789abcdef" for x in commit):
        raise SystemExit("Unexpected git ref")
    print("Target commit:",commit[:12])
    print("Current admin PID:",output("systemctl","show","dato-telegram-admin.service",
                                       "-p","MainPID","--value"))
    print("Probe URL: http://127.0.0.1:8082/login")
    print("Nginx and production systemd units will NOT be changed.")
    if not args.confirm:
        print("Dry run only. Pass --confirm to start a temporary second admin instance.")
        return
    # Use the exact version currently selected in GitHub. No working-tree edit.
    RELEASES.mkdir(mode=0o700,parents=True,exist_ok=True)
    release=RELEASES/commit
    if not release.exists():
        archive=subprocess.run(["git","-C",str(BASE),"archive","--format=tar",commit],
                               check=True,capture_output=True).stdout
        tmp=Path(tempfile.mkdtemp(prefix=".unpacked-",dir=RELEASES))
        try:
            with tarfile.open(fileobj=BytesIO(archive),mode="r:") as tar:
                tar.extractall(tmp,filter="data")
            tmp.rename(release)
        finally:
            if tmp.exists():shutil.rmtree(tmp)
    env=os.environ.copy()
    # Load the same .env used by the running admin, without shell evaluation.
    from dotenv import dotenv_values
    for key,value in dotenv_values(BASE/".env").items():
        if value is not None:
            env[key]=value
    env["PYTHONPATH"]=str(release)
    command=[str(PYTHON),"-m","uvicorn","admin.main:app","--host","127.0.0.1",
             "--port",str(PORT),"--workers","1"]
    started=time.monotonic()
    log_path=RELEASES/("probe-"+commit[:12]+".log")
    with log_path.open("a") as log:
        os.chmod(log_path,0o600)
        process=subprocess.Popen(command,cwd=release,env=env,stdout=log,stderr=subprocess.STDOUT,
                                 start_new_session=True)
        try:
            endpoints=("/login","/tg-support")
            success=set()
            while time.monotonic()-started<MAX_SECONDS:
                if process.poll() is not None:
                    raise RuntimeError("Staged admin exited before becoming ready")
                for endpoint in endpoints:
                    if endpoint in success:continue
                    try:
                        with urlopen(Request(f"http://127.0.0.1:{PORT}{endpoint}"),
                                     timeout=3) as resp:
                            if resp.status==200:success.add(endpoint)
                    except (OSError,HTTPError):
                        pass
                if len(success)==len(endpoints):
                    print("Probe PASS: both /login and /tg-support returned HTTP 200")
                    print("Temporary second instance ready after ~%d seconds"%
                          (time.monotonic()-started))
                    return
                time.sleep(1)
            raise RuntimeError("Staged admin not ready within timeout")
        finally:
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=4)
            print("Temporary admin stopped; existing services unchanged.")
            print("Private probe log path:",log_path)


if __name__=="__main__":
    try:main()
    except Exception as exc:
        print("Probe FAILED:",type(exc).__name__,str(exc)[:180],file=sys.stderr)
        raise SystemExit(1)
