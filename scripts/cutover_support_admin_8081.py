#!/usr/bin/env python3
"""Guarded cutover of a pre-staged Telegram support admin from 8082 to 8081.

Only Nginx is reloaded. The old admin remains online for rollback.
No bot, database migration or service restart is performed.
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
NEW="2d609a3267261e2370e01e02596916d1bdc044c6"
OLD="7eba555d672a43887b6c6fe5374e383d8f3d263e"

def http_status(url):
    try:
        with urlopen(url,timeout=7) as resp:
            return resp.status
    except HTTPError as exc:
        return exc.code

def main():
    if os.geteuid()!=0:
        raise RuntimeError("Run as root")
    if not (RELEASES/NEW/".venv/bin/python").is_file():
        raise RuntimeError("Target admin venv missing")
    if subprocess.run(["git","-C",str(ROOT),"cat-file","-e",NEW+"^{commit}"],
                      capture_output=True,timeout=10).returncode:
        raise RuntimeError("Pinned candidate commit missing from local git")
    sys.path.insert(0,str(RELEASES/NEW/"scripts"))
    import deploy_admin_bluegreen as bg
    import update_admin_safe as adm
    import update_bot_safe as bot

    bg.LOCK.parent.mkdir(parents=True,exist_ok=True)
    with bg.LOCK.open("a+") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if bg.routing(bg.nginx_path().read_text())!=8082:
            raise RuntimeError("Public Nginx routing changed; expected 8082")
        if adm.release_for(8082)!=OLD or adm.release_for(8081)!=NEW:
            raise RuntimeError("Admin slot commit changed since stage review")
        if not bg.health(8081) or not bg.health(8082):
            raise RuntimeError("Both admin slots must be healthy")
        if any(bg.command("systemctl","is-active",bg.UNITS[port])!="active" for port in (8081,8082)):
            raise RuntimeError("Admin systemd service inactive")
        old_bot_pid=bot.current_pid()
        if old_bot_pid<1 or bot.poller_pids()!=[old_bot_pid]:
            raise RuntimeError("Bot polling process is not a healthy singleton")
        for route in ("/tg-support/api/me","/tg-support/api/tickets","/staff","/support"):
            status=http_status("http://127.0.0.1:8081"+route)
            if status!=401:
                raise RuntimeError(f"Unexpected unauthenticated response {route}: {status}")
        if http_status("https://bot.arutyun.me/tg-support")!=200:
            raise RuntimeError("Public Mini App route is unhealthy before cutover")
        print("GATES PASSED: pinned 8081, healthy 8082, unauthorized API blocked, singleton bot.",flush=True)
        print("Switching Nginx only: 8082 -> 8081",flush=True)
        bg.cutover()
        try:
            if bg.routing(bg.nginx_path().read_text())!=8081:
                raise RuntimeError("Unexpected public routing after cutover")
            if adm.release_for(8081)!=NEW or adm.release_for(8082)!=OLD:
                raise RuntimeError("Admin slot commit changed after cutover")
            if not bg.health(8081) or not bg.health(8082):
                raise RuntimeError("Admin health failed after cutover")
            if http_status("https://bot.arutyun.me/tg-support")!=200:
                raise RuntimeError("Public Mini App HTTP failed")
            if bot.current_pid()!=old_bot_pid or bot.poller_pids()!=[old_bot_pid]:
                raise RuntimeError("Bot polling process changed")
            print("CUTOVER VERIFIED: public=8081, backup=8082, bot unchanged.",flush=True)
            print("IMPORTANT: Staff login, permissions, Telegram reply end-to-end are not yet verified.")
        except Exception:
            print("POST-CUTOVER FAILURE: attempting Nginx rollback to 8082",file=sys.stderr)
            adm.switch_back(8081,8082)
            print("Previous public admin restored to 8082",file=sys.stderr)
            raise

if __name__=="__main__":
    try:
        main()
    except Exception as exc:
        print("CUTOVER STOPPED:",type(exc).__name__,str(exc).splitlines()[0][:300],file=sys.stderr)
        sys.exit(1)
