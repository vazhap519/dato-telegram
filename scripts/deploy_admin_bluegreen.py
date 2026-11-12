#!/usr/bin/env python3
"""Blue/green admin-only deployment. Nothing happens unless a command is given.

PLAN is read only. STAGE prepares an isolated release and replaces ONLY the
inactive admin port; the active server and Nginx remain untouched. CUTOVER
switches Nginx after HTTP checks, with restoration if the check fails.

This intentionally never starts/stops the Telegram polling bot.
"""
import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from io import BytesIO
from urllib.request import urlopen
from urllib.error import HTTPError

ROOT=Path("/opt/dato-telegram")
RELEASES=Path("/opt/dato-admin-releases")
SITE=Path("/etc/nginx/sites-enabled/dato-telegram")
BACKUPS=Path("/var/backups/dato-nginx")
LOCK=Path("/run/lock/dato-admin-bluegreen.lock")
UNITS={8081:"dato-telegram-admin-blue.service",8082:"dato-telegram-admin-standby.service"}
LEGACY="dato-telegram-admin.service"
TARGETS=("/login","/tg-support")


def command(*args,check=True,timeout=180):
    r=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=timeout)
    if check and r.returncode:
        raise RuntimeError(f"{args[0]} exited {r.returncode}: {r.stderr[-250:]}")
    return r.stdout.strip()


def health(port):
    for endpoint in TARGETS:
        try:
            with urlopen(f"http://127.0.0.1:{port}{endpoint}",timeout=3) as response:
                if response.status!=200:return False
        except (OSError,HTTPError):
            return False
    return True


def nginx_path():
    if not SITE.is_file():raise RuntimeError("Nginx configuration missing")
    return SITE.resolve()


def routing(content):
    matches=re.findall(r"proxy_pass\s+http://127\.0\.0\.1:(8081|8082)\s*;",content)
    if len(matches)!=4 or len(set(matches))!=1:
        raise RuntimeError(f"Unexpected Nginx upstream layout: {matches}. No changes made")
    if content.count("server_name bot.arutyun.me;")!=2:
        raise RuntimeError("Unexpected site server_name layout")
    return int(matches[0])


def write_atomic(path,content):
    fd,name=tempfile.mkstemp(prefix=".dato-",dir=path.parent)
    try:
        os.fchmod(fd,path.stat().st_mode & 0o777)
        with os.fdopen(fd,"w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def memory_mib():
    return next(int(x.split()[1])//1024 for x in Path("/proc/meminfo").read_text().splitlines()
                if x.startswith("MemAvailable:"))


def target_commit():
    sha=command("git","-C",str(ROOT),"rev-parse","origin/main")
    if not re.fullmatch("[a-f0-9]{40}",sha):raise RuntimeError("Invalid git ref")
    return sha


def release_sha_for_port(port):
    # Current standby installed by the original bluegreen script uses the
    # immutable release path. The legacy 8081 still runs the mutable repo.
    unit=UNITS[port]
    result=command("systemctl","show",unit,"-p","WorkingDirectory","--value",check=False)
    candidate=Path(result) if result else None
    if candidate and candidate.parent==RELEASES and re.fullmatch("[a-f0-9]{40}",candidate.name):
        return candidate.name
    return "(legacy or unknown)"


def plan():
    active=routing(nginx_path().read_text())
    idle=8081 if active==8082 else 8082
    sha=target_commit()
    print("=== PLAN (NO CHANGES) ===")
    print("Active traffic port:",active,"healthy:",health(active))
    print("Standby port:",idle,"healthy:",health(idle))
    print("Active release:",release_sha_for_port(active))
    print("Target origin/main:",sha)
    print("Available RAM:",memory_mib(),"MiB")
    print("Disk free:",shutil.disk_usage(ROOT).free//(1024*1024),"MiB")
    print("Inactive unit:",UNITS[idle])
    print("Original installer stays unchanged.")
    print("Telegram bot is not changed or restarted.")
    print("To stage after reviewing: stage --commit",sha)


def snapshot(sha):
    release=RELEASES/sha
    if (release/"admin/main.py").is_file():
        return release
    RELEASES.mkdir(mode=0o700,parents=True,exist_ok=True)
    archive=subprocess.run(["git","-C",str(ROOT),"archive","--format=tar",sha],
                           capture_output=True,check=True).stdout
    tmp=Path(tempfile.mkdtemp(prefix=".admin-snapshot-",dir=RELEASES))
    try:
        with tarfile.open(fileobj=BytesIO(archive),mode="r:") as fp:
            fp.extractall(tmp,filter="data")
        if not (tmp/"admin/main.py").is_file():
            raise RuntimeError("Snapshot missing admin/main.py")
        tmp.rename(release)
    finally:
        if tmp.exists():shutil.rmtree(tmp)
    return release


def ensure_venv(release):
    venv=release/".venv"
    python=venv/"bin/python"
    if python.is_file() and (venv/".install-ok").is_file():return python
    if not python.is_file():
        command("/usr/bin/python3","-m","venv",str(venv),timeout=120)
    print("Installing isolated dependencies; serving instance is unaffected...",flush=True)
    command(str(python),"-m","pip","install","-r",str(release/"requirements.txt"),timeout=420)
    (venv/".install-ok").write_text(hashlib.sha256((release/"requirements.txt").read_bytes()).hexdigest())
    command(str(python),"-m","compileall","-q",str(release/"admin"),str(release/"db"),timeout=120)
    return python


def unit_content(release,port):
    return f"""[Unit]
Description=Dato Telegram admin blue-green slot on {port}
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={release}
ExecStart={release}/.venv/bin/python -m scripts.slot_runner --port {port}
Restart=on-failure
RestartSec=3
TimeoutStartSec=65
KillMode=control-group
UMask=0077

[Install]
WantedBy=multi-user.target
"""


def stage(sha):
    active=routing(nginx_path().read_text())
    idle=8081 if active==8082 else 8082
    if sha!=target_commit():raise RuntimeError("Target must exactly match fetched origin/main")
    if not health(active):raise RuntimeError("Active port failed HTTP health check")
    if memory_mib()<220:raise RuntimeError("Less than 220 MiB available RAM; abort")
    if shutil.disk_usage(ROOT).free<1024**3:raise RuntimeError("Less than 1 GiB disk space")
    if not (ROOT/".env").is_file():raise RuntimeError("Missing .env")
    if command("git","-C",str(ROOT),"status","--porcelain"):
        raise RuntimeError("Working repo has changes, refusing stage")
    current=command("git","-C",str(ROOT),"rev-parse","HEAD")
    if not command("git","-C",str(ROOT),"merge-base","--is-ancestor",current,sha,check=False)=="":
        # merge-base success prints nothing; verify exit code separately below.
        pass
    ancestry=subprocess.run(["git","-C",str(ROOT),"merge-base","--is-ancestor",current,sha])
    if ancestry.returncode:raise RuntimeError("Git histories diverged")
    # Restrict unattended stages when a migration or polling bot code changed.
    changed=command("git","-C",str(ROOT),"diff","--name-only",current,sha).splitlines()
    risky=[p for p in changed if p.startswith(("db/","bot/")) or p=="requirements.txt"]
    if risky:
        print("Review required for app/database/bot compatibility:",", ".join(risky[:12]))
        raise RuntimeError("Admin-only stage refuses changes to db/, bot/, requirements.txt")
    release=snapshot(sha)
    python=ensure_venv(release)
    print("Release ready on disk:",release)
    print("Inactive slot:",idle)
    unit=Path("/etc/systemd/system")/UNITS[idle]
    old_content=unit.read_text() if unit.exists() else None
    previous_active=command("systemctl","is-active",UNITS[idle],check=False)=="active"
    legacy_active=False
    # Legacy 8081 currently occupies inactive port. Never stop it unless
    # Nginx is confirmed to serve 8082 and that active server is healthy.
    if idle==8081:
        legacy_active=command("systemctl","is-active",LEGACY,check=False)=="active"
        if legacy_active:
            print("Stopping only inactive legacy 8081 (public traffic uses 8082)")
            command("systemctl","stop",LEGACY)
    if previous_active:command("systemctl","stop",UNITS[idle])
    if health(idle):
        raise RuntimeError("Inactive port is still in use; refusing to start a second server")
    try:
        unit.write_text(unit_content(release,idle))
        unit.chmod(0o644)
        command("systemctl","daemon-reload")
        command("systemctl","start",UNITS[idle])
        for _ in range(75):
            if health(idle):
                print("STAGED OK:",sha[:12],"on",idle)
                print("Nginx stays on",active)
                print("Next command: cutover")
                return
            if command("systemctl","is-active",UNITS[idle],check=False)!="active":
                break
            time.sleep(1)
        raise RuntimeError("Staged instance failed HTTP checks")
    except Exception:
        command("systemctl","stop",UNITS[idle],check=False)
        if old_content is None:
            unit.unlink(missing_ok=True)
        else:
            unit.write_text(old_content)
        command("systemctl","daemon-reload",check=False)
        if previous_active:
            command("systemctl","start",UNITS[idle],check=False)
        if legacy_active:
            command("systemctl","start",LEGACY,check=False)
        print("Inactive slot restored where possible. Active Nginx unchanged.",file=sys.stderr)
        raise


def cutover():
    path=nginx_path()
    original=path.read_text()
    old=routing(original)
    new=8081 if old==8082 else 8082
    if not health(old) or not health(new):
        raise RuntimeError("Both ports must be healthy before cutover")
    if command("systemctl","is-active",UNITS[new],check=False)!="active":
        raise RuntimeError("Target port is not owned by the managed slot service")
    if not (Path("/etc/systemd/system")/UNITS[new]).is_file():
        raise RuntimeError("Managed slot unit file missing")
    BACKUPS.mkdir(mode=0o700,parents=True,exist_ok=True)
    backup=BACKUPS/("dato-cutover-"+time.strftime("%Y%m%d-%H%M%S")+".conf")
    shutil.copy2(path,backup)
    backup.chmod(0o600)
    changed=original.replace(f"proxy_pass http://127.0.0.1:{old};",
                             f"proxy_pass http://127.0.0.1:{new};")
    if routing(changed)!=new:
        raise RuntimeError("Unexpected Nginx replacement")
    write_atomic(path,changed)
    try:
        command("nginx","-t")
        command("systemctl","reload","nginx")
        with urlopen("https://bot.arutyun.me/tg-support",timeout=8) as resp:
            if resp.status!=200:raise RuntimeError("Public Mini App HTTP check failed")
    except Exception:
        write_atomic(path,original)
        command("nginx","-t",check=False)
        command("systemctl","reload","nginx",check=False)
        print("Public routing restored to prior port.",file=sys.stderr)
        raise
    print(f"CUTOVER OK: {old} -> {new}. Previous version remains running.")
    print("Nginx backup:",backup)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("operation",choices=("plan","stage","cutover"))
    parser.add_argument("--commit",default="")
    a=parser.parse_args()
    if os.geteuid()!=0:raise RuntimeError("Run as root")
    if a.operation=="plan":plan();return
    LOCK.parent.mkdir(parents=True,exist_ok=True)
    with LOCK.open("a+") as file:
        fcntl.flock(file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if a.operation=="stage":
            if not re.fullmatch("[a-f0-9]{40}",a.commit):
                raise RuntimeError("Provide full SHA with --commit")
            stage(a.commit)
        elif a.operation=="cutover":cutover()


if __name__=="__main__":
    try:main()
    except Exception as exc:
        print("DEPLOY FAILED:",str(exc)[:400],file=sys.stderr)
        raise SystemExit(1)
