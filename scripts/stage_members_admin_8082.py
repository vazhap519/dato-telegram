#!/usr/bin/env python3
"""Stage pinned members Mini App on inactive 8082 with automatic standby restore.

Public Nginx remains on 8081. Bot stays pinned on previous release. Only
inactive 8082 systemd unit may be restarted; rollback is handled by the
existing reviewed Blue/Green stage routine if the new instance fails.
"""
import fcntl
import os
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT=Path("/opt/dato-telegram")
RELEASES=Path("/opt/dato-admin-releases")
CANDIDATE="f7ea20273c15e170bb58f8eaa97341e84974ec77"
PUBLISHED="2d609a3267261e2370e01e02596916d1bdc044c6"
OLD_STANDBY="7eba555d672a43887b6c6fe5374e383d8f3d263e"

def code(url):
    try:
        with urlopen(url,timeout=8) as resp:
            return resp.status
    except HTTPError as exc:
        return exc.code

def run(*args):
    return subprocess.check_output(args,text=True,timeout=20).strip()

def main():
    if os.geteuid()!=0:
        raise RuntimeError("Requires root")
    if run("git","-C",str(ROOT),"status","--porcelain"):
        raise RuntimeError("Live repository has uncommitted changes")
    if run("git","-C",str(ROOT),"rev-parse","--verify",
           "refs/remotes/origin/feature/miniapp-members-subscriptions-20261116")!=CANDIDATE:
        raise RuntimeError("Pinned candidate branch has moved; re-review required")
    release=RELEASES/CANDIDATE
    if not (release/".venv/.install-ok").is_file() or not (release/"admin/members_miniapp.py").is_file():
        raise RuntimeError("Candidate must be prepared first")
    sys.path.insert(0,str(RELEASES/PUBLISHED/"scripts"))
    import deploy_admin_bluegreen as bg
    bg.target_commit=lambda:CANDIDATE
    bg.LOCK.parent.mkdir(parents=True,exist_ok=True)
    with bg.LOCK.open("a+") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if bg.routing(bg.nginx_path().read_text())!=8081:
            raise RuntimeError("Public admin is not routed to 8081")
        if bg.release_sha_for_port(8081)!=PUBLISHED:
            raise RuntimeError("Active admin release is not expected pinned version")
        if bg.release_sha_for_port(8082)!=OLD_STANDBY:
            raise RuntimeError("Standby changed unexpectedly; refusing replacement")
        if not (bg.health(8081) and bg.health(8082)):
            raise RuntimeError("Both admin slots must be healthy before stage")
        if code("https://bot.arutyun.me/tg-support")!=200:
            raise RuntimeError("Public support Mini App is not healthy")
        if code("http://127.0.0.1:8081/tg-support/api/me")!=401:
            raise RuntimeError("Active support Telegram auth gate incorrect")
        if code("http://127.0.0.1:8081/tg-support/api/members")!=404:
            raise RuntimeError("Unexpected members API present on active slot")
        wd=run("systemctl","show","dato-telegram-bot.service",
               "-p","WorkingDirectory","--value")
        if wd!=str(RELEASES/PUBLISHED):
            raise RuntimeError("Polling bot release mismatch")
        pid=run("systemctl","show","dato-telegram-bot.service",
                "-p","MainPID","--value")
        if int(pid)<=0 or run("systemctl","is-active","dato-telegram-bot.service")!="active":
            raise RuntimeError("Bot is not healthy")
        print("STAGE GATES PASSED: public 8081 unchanged, bot PID",pid,flush=True)
        # This reviewed routine stops only the inactive admin slot, writes the
        # pinned unit for 8082 and restores its old unit on startup failure.
        bg.stage(CANDIDATE,allow_bot_changes=True,reviewed_support=True)
        if bg.release_sha_for_port(8082)!=CANDIDATE or not bg.health(8082):
            raise RuntimeError("Candidate standby failed verification")
        if code("http://127.0.0.1:8082/tg-support/members")!=200:
            raise RuntimeError("Members page is not available in staged release")
        if code("http://127.0.0.1:8082/tg-support/api/members")!=401:
            raise RuntimeError("Members API does not enforce Telegram auth")
        if code("http://127.0.0.1:8082/tg-support/api/members/1")!=401:
            raise RuntimeError("Member detail does not enforce Telegram auth")
        if bg.routing(bg.nginx_path().read_text())!=8081 or not bg.health(8081):
            raise RuntimeError("Public active admin changed during staging")
        if run("systemctl","show","dato-telegram-bot.service",
               "-p","MainPID","--value")!=pid:
            raise RuntimeError("Bot PID changed during admin-only stage")
        print("MEMBERS STAGED OK: 8082 =",CANDIDATE[:12],flush=True)
        print("PUBLIC STILL: 8081 =",PUBLISHED[:12],flush=True)
        print("BOT UNCHANGED PID:",pid,flush=True)
        print("NO NGINX CUTOVER performed. Real Telegram staff access still requires manual test.")

if __name__=="__main__":
    try:main()
    except Exception as exc:
        print("MEMBERS STAGE STOPPED:",type(exc).__name__,str(exc).splitlines()[0][:320],file=sys.stderr)
        sys.exit(1)
