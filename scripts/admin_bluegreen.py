#!/usr/bin/env python3
"""Stage and switch Dato admin releases with the old instance kept alive.

Explicit commands only: 'prepare' starts a standby server, 'switch' edits
the Nginx upstreams after checks, 'status' prints routing and unit status.
The Telegram bot process is never started or touched.
"""
import argparse
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen
from urllib.error import HTTPError

BASE=Path("/opt/dato-telegram")
RELEASES=Path("/opt/dato-admin-releases")
NGINX=Path("/etc/nginx/sites-enabled/dato-telegram")
UNIT=Path("/etc/systemd/system/dato-telegram-admin-standby.service")
STANDBY="dato-telegram-admin-standby.service"
OLD="dato-telegram-admin.service"
PORT=8082
UPSTREAM_OLD="http://127.0.0.1:8081"
UPSTREAM_NEW="http://127.0.0.1:8082"
BACKUP_DIR=Path("/var/backups/dato-nginx")
HEALTH=("/login","/tg-support")


def run(*args,check=True):
    p=subprocess.run(args,text=True,capture_output=True,timeout=30)
    if check and p.returncode:
        raise RuntimeError(f"{args[0]} failed (exit {p.returncode}): {p.stderr[:250]}")
    return p.stdout.strip()


def ok(port,endpoint):
    try:
        with urlopen(f"http://127.0.0.1:{port}{endpoint}",timeout=3) as resp:
            return resp.status==200
    except (OSError,HTTPError):
        return False


def healthy(port):
    return all(ok(port,path) for path in HEALTH)


def available_memory():
    return next((int(line.split()[1])//1024 for line in Path("/proc/meminfo").read_text().splitlines()
                 if line.startswith("MemAvailable:")),0)


def nginx_target():
    if not NGINX.is_file():
        raise RuntimeError("Expected Nginx site not found")
    return NGINX.resolve()


def routing(content):
    ports=re.findall(r"proxy_pass\\s+http://127\\.0\\.0\\.1:(8081|8082)\\s*;",content)
    if len(ports)!=4 or len(set(ports))!=1:
        raise RuntimeError(
            f"Unexpected Nginx routing: matched {len(ports)} upstreams, "
            f"ports={ports}; expected four identical ports. No changes made")
    server_names=content.count("server_name bot.arutyun.me;")
    if server_names!=2:
        raise RuntimeError(
            f"Unexpected Nginx server layout: {server_names} server_name entries; "
            "expected two. No changes made")
    return int(ports[0])


def atomic_write(path,text):
    fd,name=tempfile.mkstemp(prefix=".dato-nginx-",dir=path.parent)
    try:
        os.fchmod(fd,path.stat().st_mode & 0o777)
        with os.fdopen(fd,"w") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def create_unit():
    source=Path(__file__).resolve()
    # The helper installed in /usr/local/lib must exist independently of git pulls.
    launcher=Path("/usr/local/lib/dato-admin-slot-launcher.py")
    launcher.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(source,launcher)
    launcher.chmod(0o700)
    # For a persistent standby, use explicit release dir pinned at staging time.
    return launcher


def stage(commit):
    if available_memory()<250:
        raise RuntimeError("Less than 250 MiB available RAM")
    if not healthy(8081):
        raise RuntimeError("Current admin on 8081 is not healthy")
    if not (BASE/".venv/bin/python").is_file() or not (BASE/".env").is_file():
        raise RuntimeError("Missing runtime or .env")
    if not re.fullmatch("[0-9a-f]{40}",commit):
        raise RuntimeError("Expected a full 40-character commit SHA")
    found=run("git","-C",str(BASE),"rev-parse","--verify",commit+"^{commit}")
    if found!=commit:
        raise RuntimeError("Unknown release commit")
    release=RELEASES/commit
    if not (release/"admin/main.py").is_file():
        raise RuntimeError("Release snapshot not found. First run bluegreen_probe.py --confirm")
    # No live instance is stopped. Standby port must be available.
    unit_active=run("systemctl","is-active",STANDBY,check=False)=="active"
    if unit_active:
        raise RuntimeError("Standby already active. Refusing to replace running instance")
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1",PORT))==0:
            raise RuntimeError("Port 8082 is already occupied")
    launcher=create_unit()
    body=f"""[Unit]
Description=Dato Telegram staged admin server (blue/green standby)
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={release}
ExecStart={BASE}/.venv/bin/python {launcher} serve {release}
Restart=on-failure
RestartSec=3
TimeoutStartSec=65
KillMode=control-group
UMask=0077

[Install]
WantedBy=multi-user.target
"""
    UNIT.write_text(body)
    UNIT.chmod(0o644)
    run("systemctl","daemon-reload")
    run("systemctl","start",STANDBY)
    for _ in range(65):
        if healthy(PORT):
            print("STANDBY READY: 8082, commit:",commit[:12])
            print("Existing admin 8081 remains running.")
            print("Nginx NOT switched. Next: admin_bluegreen.py switch")
            return
        if run("systemctl","is-active",STANDBY,check=False)!="active":
            break
        time.sleep(1)
    run("systemctl","stop",STANDBY,check=False)
    raise RuntimeError("Standby failed health checks; stopped it. Nginx unchanged")


def serve(release):
    if not release.is_dir() or RELEASES not in release.parents:
        raise RuntimeError("Invalid release directory")
    from dotenv import dotenv_values
    env=os.environ.copy()
    for key,value in dotenv_values(BASE/".env").items():
        if value is not None:env[key]=value
    env["PYTHONPATH"]=str(release)
    os.chdir(release)
    py=BASE/".venv/bin/python"
    os.execve(str(py),[str(py),"-m","uvicorn","admin.main:app",
                        "--host","127.0.0.1","--port",str(PORT),"--workers","1"],env)


def switch():
    if run("systemctl","is-active",STANDBY,check=False)!="active" or not healthy(PORT):
        raise RuntimeError("Standby on 8082 is not healthy; no change")
    if not healthy(8081):
        raise RuntimeError("Existing 8081 not healthy; no change")
    target=nginx_target()
    old=target.read_text()
    active=routing(old)
    if active==8082:
        print("Nginx already routes to 8082");return
    if active!=8081:
        raise RuntimeError("Unexpected upstream")
    BACKUP_DIR.mkdir(mode=0o700,parents=True,exist_ok=True)
    backup=BACKUP_DIR/("dato-before-bluegreen-"+time.strftime("%Y%m%d-%H%M%S")+".conf")
    shutil.copy2(target,backup)
    backup.chmod(0o600)
    changed=old.replace("proxy_pass "+UPSTREAM_OLD+";","proxy_pass "+UPSTREAM_NEW+";")
    if routing(changed)!=8082:
        raise RuntimeError("Invalid replacement; Nginx unchanged")
    atomic_write(target,changed)
    try:
        run("nginx","-t")
        run("systemctl","reload","nginx")
        # Verify public entry point after reload, no Basic Auth credentials needed.
        with urlopen("https://bot.arutyun.me/tg-support",timeout=8) as resp:
            if resp.status!=200:raise RuntimeError("Public Mini App health check failed")
    except Exception:
        atomic_write(target,old)
        run("nginx","-t",check=False)
        run("systemctl","reload","nginx",check=False)
        raise
    print("SWITCHED to 8082. Previous 8081 still running.")
    print("Backup:",backup)
    print("To roll back: admin_bluegreen.py rollback")


def rollback():
    target=nginx_target()
    current=target.read_text()
    if routing(current)==8081:
        print("Already on 8081");return
    if not healthy(8081):
        raise RuntimeError("Old admin 8081 is not healthy; cannot roll back")
    corrected=current.replace("proxy_pass "+UPSTREAM_NEW+";","proxy_pass "+UPSTREAM_OLD+";")
    if routing(corrected)!=8081:raise RuntimeError("Unexpected routing")
    atomic_write(target,corrected)
    try:
        run("nginx","-t")
        run("systemctl","reload","nginx")
    except Exception:
        atomic_write(target,current)
        run("nginx","-t",check=False)
        run("systemctl","reload","nginx",check=False)
        raise
    print("ROLLED BACK to 8081; standby remains running.")


def status():
    print("Old admin:",run("systemctl","is-active",OLD,check=False),
          "healthy:",healthy(8081))
    print("Standby:",run("systemctl","is-active",STANDBY,check=False),
          "healthy:",healthy(8082))
    print("Nginx:",routing(nginx_target().read_text()))
    print("Available RAM:",available_memory(),"MiB")


def main():
    p=argparse.ArgumentParser()
    p.add_argument("command",choices=("stage","status","switch","rollback","serve"))
    p.add_argument("release",nargs="?")
    args=p.parse_args()
    if os.geteuid()!=0:raise RuntimeError("Run as root")
    if args.command=="status":status()
    elif args.command=="serve":
        if not args.release:raise RuntimeError("Missing release")
        serve(Path(args.release).resolve())
    elif args.command=="stage":
        if not args.release:raise RuntimeError("Missing commit hash")
        stage(args.release)
    elif args.command=="switch":switch()
    elif args.command=="rollback":rollback()


if __name__=="__main__":
    try:main()
    except Exception as exc:
        print("FAILED:",str(exc)[:250],file=sys.stderr)
        raise SystemExit(1)
