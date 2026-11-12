#!/usr/bin/env python3
"""Transactional single-poller Telegram bot deployment from GitHub.

check: read-only view; prepare: immutable release + isolated venv + preflight;
deploy: prepare, stop old bot, activate new release, verify aiogram polling,
        restore previous systemd configuration on failure;
rollback: explicit switch back to previous known-good release;
history: private audit trail.

Admin services and Nginx are never restarted or reconfigured.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
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

ROOT=Path("/opt/dato-telegram")
RELEASES=Path("/opt/dato-admin-releases")
UNIT="dato-telegram-bot.service"
DROPIN=Path("/etc/systemd/system/dato-telegram-bot.service.d/90-dato-release.conf")
STATE=Path("/var/lib/dato-bot-deploy")
HISTORY=Path("/var/log/dato-deploy/bot-updates.jsonl")
LOCK=Path("/run/lock/dato-admin-bluegreen.lock")  # also excludes admin deployments
ENV=ROOT/".env"
COMMIT_RE=re.compile(r"^[a-f0-9]{40}$")
POLLING=("Run polling for bot", "Start polling")


def call(*args,check=True,timeout=90):
    result=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"{args[0]} failed ({result.returncode}): {result.stderr[-300:]}")
    return result.stdout.strip()


def bot_state():
    return call("systemctl","is-active",UNIT,check=False)


def current_pid():
    raw=call("systemctl","show",UNIT,"-p","MainPID","--value")
    return int(raw) if raw.isdecimal() else 0


def poller_pids():
    """Detect accidental independent bot.main or bot_runner processes."""
    p=subprocess.run(["ps","-eo","pid=,args="],capture_output=True,text=True,timeout=10)
    if p.returncode:
        raise RuntimeError("Unable to inspect Telegram poller processes")
    found=[]
    for line in p.stdout.splitlines():
        parts=line.strip().split(None,1)
        if len(parts)!=2 or not parts[0].isdigit():continue
        argv=parts[1]
        if re.search(r"(?:^|\\s)-m\\s+(?:bot\\.main|scripts\\.bot_runner)(?:\\s|$)",argv):
            found.append(int(parts[0]))
    return found


def recent_polling(pid):
    if pid<=0:return False
    # The logging string comes from aiogram's dispatcher after getMe.
    p=subprocess.run(["journalctl","--no-pager","-o","cat","_PID="+str(pid),
                      "-n","120"],capture_output=True,text=True,timeout=15)
    text=p.stdout
    return p.returncode==0 and any(marker in text for marker in POLLING)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def audit(event,**details):
    HISTORY.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    row={"time":now(),"event":event,**details}
    with HISTORY.open("a") as stream:
        os.chmod(HISTORY,0o600)
        stream.write(json.dumps(row,ensure_ascii=False,separators=(",",":"))+"\n")
        stream.flush()
        os.fsync(stream.fileno())


def current_ref():
    if DROPIN.exists():
        content=DROPIN.read_text()
        matches=re.findall(r"^WorkingDirectory=(/opt/dato-admin-releases/([0-9a-f]{40}))$",content,re.M)
        if len(matches)!=1:
            raise RuntimeError("Unknown bot systemd override; refusing to deploy")
        return matches[0][1]
    wd=call("systemctl","show",UNIT,"-p","WorkingDirectory","--value")
    if wd!=str(ROOT):
        raise RuntimeError("Bot is not running from expected legacy directory")
    return call("git","-C",str(ROOT),"rev-parse","HEAD")


def origin():
    commit=call("git","-C",str(ROOT),"rev-parse","origin/main")
    if not COMMIT_RE.fullmatch(commit):
        raise RuntimeError("Invalid origin/main commit")
    return commit


def clean_repo():
    if call("git","-C",str(ROOT),"status","--porcelain"):
        raise RuntimeError("Working tree not clean; inspect git status --short")
    return True


def check():
    print("=== BOT UPDATE CHECK (NO CHANGES) ===")
    print("Bot service:",bot_state(),"MainPID:",current_pid())
    print("Bot commit:",current_ref()[:12])
    print("Target origin/main:",origin()[:12])
    print("Override:", "installed" if DROPIN.exists() else "legacy")
    print("Working tree:", "clean" if not call("git","-C",str(ROOT),"status","--porcelain") else "modified")
    print("Available memory MiB:",memory_available())
    print("Free disk MiB:",shutil.disk_usage(ROOT).free//(1024**2))
    print("Admin and Nginx are not modified by bot deployments.")


def memory_available():
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1])//1024
    return 0


def snapshot(sha):
    release=RELEASES/sha
    if (release/"bot/main.py").is_file():
        return release
    RELEASES.mkdir(parents=True,mode=0o700,exist_ok=True)
    archive=subprocess.run(["git","-C",str(ROOT),"archive","--format=tar",sha],
                           capture_output=True,check=True).stdout
    temp=Path(tempfile.mkdtemp(prefix=".bot-release-",dir=RELEASES))
    try:
        with tarfile.open(fileobj=BytesIO(archive),mode="r:") as stream:
            stream.extractall(temp,filter="data")
        if not (temp/"bot/main.py").is_file():
            raise RuntimeError("Release missing bot/main.py")
        temp.rename(release)
    finally:
        if temp.exists():shutil.rmtree(temp)
    return release


def prepare(sha):
    if not COMMIT_RE.fullmatch(sha) or sha!=origin():
        raise RuntimeError("Target SHA must equal fetched origin/main")
    if not ENV.is_file():raise RuntimeError("Shared bot .env is missing")
    if memory_available()<180:raise RuntimeError("Not enough available RAM to prepare")
    if shutil.disk_usage(ROOT).free<1024**3:raise RuntimeError("Less than 1 GiB disk free")
    clean_repo()
    base=call("git","-C",str(ROOT),"rev-parse","HEAD")
    old=current_ref()
    for ancestor in (base,old):
        if subprocess.run(["git","-C",str(ROOT),"merge-base","--is-ancestor",
                           ancestor,sha],capture_output=True).returncode:
            raise RuntimeError("Git histories diverged; manual review required")
    changed=call("git","-C",str(ROOT),"diff","--name-only",old,sha).splitlines()
    risky=[name for name in changed if name.startswith("db/")]
    if risky:
        raise RuntimeError("Database changes require manual compatibility review: "+", ".join(risky[:10]))
    release=snapshot(sha)
    if not (release/"scripts/bot_runner.py").is_file():
        raise RuntimeError("Release lacks dedicated bot runner; refusing deployment")
    py=release/".bot-venv/bin/python"
    marker=release/".bot-venv/.prepared"
    expected=hashlib.sha256((release/"requirements.txt").read_bytes()).hexdigest()
    if not py.is_file():
        call("/usr/bin/python3","-m","venv",str(release/".bot-venv"),timeout=130)
    if not marker.is_file() or marker.read_text().strip()!=expected:
        print("Installing bot release dependencies separately; live bot unaffected...",flush=True)
        call(str(py),"-m","pip","install","-r",str(release/"requirements.txt"),timeout=500)
        marker.write_text(expected)
    if subprocess.run([str(py),"-m","compileall","-q",str(release/"bot"),str(release/"db"),str(release/"scripts")]).returncode:
        raise RuntimeError("Release Python preflight failed")
    # Static import of bot.main would instantiate Bot and may contact services;
    # no import or polling is attempted in preflight.
    print("PREPARED:",sha[:12],"isolated Python:",py)
    return release,py


def systemd_override(release,python):
    return ("[Service]\n"
            "WorkingDirectory="+str(release)+"\n"
            "ExecStart=\n"
            "ExecStart="+str(python)+" -m scripts.bot_runner\n"
            "Environment=PYTHONPATH="+str(release)+"\n")


def write_atomic(path,content):
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix=".dato-bot-",dir=path.parent)
    try:
        os.fchmod(fd,0o644)
        with os.fdopen(fd,"w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def wait_for_polling(previous_pid,timeout=65):
    first=None
    started=time.monotonic()
    while time.monotonic()-started<timeout:
        pid=current_pid()
        if bot_state()=="active" and pid>0 and pid!=previous_pid:
            if first is None:first=time.monotonic()
            # Healthy only after aiogram's polling message and stable process.
            if recent_polling(pid) and time.monotonic()-first>=5:
                return pid
        elif bot_state() in ("failed","inactive"):
            time.sleep(1)
        time.sleep(1)
    raise RuntimeError("New bot failed polling readiness check within timeout")


def deploy(sha):
    if bot_state()!="active" or current_pid()<=0:
        raise RuntimeError("Existing bot is not healthy; refuse automatic deployment")
    old_ref=current_ref()
    if sha==old_ref:
        print("NOOP: bot already at latest commit",sha[:12])
        audit("noop",commit=sha)
        return
    old_pid=current_pid()
    found=poller_pids()
    if found!=[old_pid]:
        raise RuntimeError(f"Expected exactly one supervised polling process; found {found}, service PID {old_pid}")
    release,python=prepare(sha)
    old_override=DROPIN.read_text() if DROPIN.exists() else None
    # Never start a second poller before stop completed.
    audit("begin",old_commit=old_ref,new_commit=sha,old_pid=old_pid)
    try:
        call("systemctl","stop",UNIT,timeout=65)
        if bot_state()=="active" or current_pid()!=0 or poller_pids():
            raise RuntimeError("Old polling bot did not fully stop; refusing to start new bot")
        write_atomic(DROPIN,systemd_override(release,python))
        call("systemctl","daemon-reload")
        call("systemctl","reset-failed",UNIT,check=False)
        call("systemctl","start",UNIT,timeout=80)
        new_pid=wait_for_polling(old_pid)
        audit("success",old_commit=old_ref,new_commit=sha,new_pid=new_pid)
        print("BOT UPDATE OK:",old_ref[:12],"->",sha[:12],"PID",new_pid)
        print("Admin/Nginx untouched. Previous configuration retained for rollback.")
        STATE.mkdir(mode=0o700,parents=True,exist_ok=True)
        os.chmod(STATE,0o700)
        (STATE/"previous.json").write_text(json.dumps({"old_override":old_override,
                                                         "old_commit":old_ref,"new_commit":sha}))
        os.chmod(STATE/"previous.json",0o600)
    except Exception as exc:
        # Stop the new poller before restoring old configuration, even if
        # the initial restart has partially succeeded.
        call("systemctl","stop",UNIT,check=False,timeout=65)
        if old_override is None:
            DROPIN.unlink(missing_ok=True)
        else:
            write_atomic(DROPIN,old_override)
        call("systemctl","daemon-reload",check=False)
        call("systemctl","reset-failed",UNIT,check=False)
        call("systemctl","start",UNIT,check=False,timeout=80)
        restored=bot_state()=="active"
        audit("failed",old_commit=old_ref,new_commit=sha,
              error=type(exc).__name__,restored=restored)
        if not restored:
            print("CRITICAL: bot restore failed; inspect systemctl status.",file=sys.stderr)
        raise


def rollback():
    file=STATE/"previous.json"
    if not file.is_file():raise RuntimeError("No saved previous deployment")
    data=json.loads(file.read_text())
    if not COMMIT_RE.fullmatch(data["old_commit"]):
        raise RuntimeError("Invalid saved commit")
    if current_ref()!=data["new_commit"]:
        raise RuntimeError("Current bot not at recorded deployment; refusing rollback")
    if bot_state()!="active":
        raise RuntimeError("Current bot inactive; manual recovery needed")
    old=data["old_override"]
    if old is None:
        if call("git","-C",str(ROOT),"rev-parse","HEAD")!=data["old_commit"]:
            raise RuntimeError("Legacy working repo changed; refusing rollback")
    old_pid=current_pid()
    call("systemctl","stop",UNIT,timeout=65)
    if bot_state()=="active" or current_pid()!=0 or poller_pids():
        raise RuntimeError("Current poller still running; abort")
    if old is None:DROPIN.unlink(missing_ok=True)
    else:write_atomic(DROPIN,old)
    call("systemctl","daemon-reload")
    call("systemctl","reset-failed",UNIT,check=False)
    call("systemctl","start",UNIT,timeout=80)
    new_pid=wait_for_polling(old_pid)
    audit("rollback",from_commit=data["new_commit"],to_commit=data["old_commit"],
          pid=new_pid)
    print("BOT ROLLBACK OK:",data["old_commit"][:12])


def history():
    if not HISTORY.is_file():
        print("No bot deployment records");return
    for line in HISTORY.read_text().splitlines()[-15:]:
        try:
            data=json.loads(line)
            print(data.get("time",""),data.get("event",""),
                  data.get("new_commit",data.get("to_commit",data.get("commit","")))[:12],
                  "restored:",data.get("restored","-"))
        except (ValueError,TypeError):
            print("(invalid log entry)")


def main():
    parser=argparse.ArgumentParser(description="Single-poller bot updater")
    parser.add_argument("action",choices=("check","prepare","deploy","rollback","history"))
    parser.add_argument("--commit",default="")
    a=parser.parse_args()
    if os.geteuid()!=0:raise RuntimeError("Run as root")
    if a.action=="check":return check()
    if a.action=="history":return history()
    LOCK.parent.mkdir(parents=True,exist_ok=True)
    with LOCK.open("a+") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if a.action=="rollback":return rollback()
        call("git","-C",str(ROOT),"fetch","origin","main",timeout=140)
        sha=a.commit or origin()
        if sha!=origin():raise RuntimeError("Only latest fetched origin/main can be deployed")
        if a.action=="prepare":
            prepare(sha)
        else:deploy(sha)


if __name__=="__main__":
    try:main()
    except Exception as exc:
        print("BOT UPDATE FAILED:",str(exc)[:400],file=sys.stderr)
        raise SystemExit(1)
