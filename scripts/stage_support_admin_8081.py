#!/usr/bin/env python3
"""Stage prebuilt, pinned support admin on inactive 8081 only.

The public Nginx route (8082) and Telegram polling bot are never deliberately
changed. The existing stage() routine restores the prior inactive slot on a
startup error. Admin application startup MAY perform idempotent DB writes.
"""
import fcntl
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path("/opt/dato-telegram")
SHA="2d609a3267261e2370e01e02596916d1bdc044c6"
RELEASE=Path("/opt/dato-admin-releases")/SHA

def shell(*args):
    return subprocess.check_output(args,text=True,timeout=25).strip()

def main():
    if os.geteuid()!=0:
        raise RuntimeError("Root required")
    if shell("git","-C",str(ROOT),"rev-parse","--verify",SHA)!=SHA:
        raise RuntimeError("Pinned candidate commit is not available")
    if shell("git","-C",str(ROOT),"status","--porcelain"):
        raise RuntimeError("Production Git working tree is not clean")
    if not (RELEASE/".venv/bin/python").is_file() or not (RELEASE/".venv/.install-ok").is_file():
        raise RuntimeError("Prebuilt admin environment not ready")
    if not (RELEASE/".bot-venv/.prepared").is_file():
        raise RuntimeError("Prebuilt bot environment not ready")
    # Only import reviewed code from the immutable, previously-built release.
    sys.path.insert(0,str(RELEASE/"scripts"))
    import update_system_safe as updater
    import deploy_admin_bluegreen as bg
    import update_bot_safe as bot
    bg.target_commit=lambda:SHA
    bot.origin=lambda:SHA

    bg.LOCK.parent.mkdir(parents=True,exist_ok=True)
    with bg.LOCK.open("a+") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if bg.routing(bg.nginx_path().read_text())!=8082:
            raise RuntimeError("Active route changed; expected 8082")
        if bg.release_sha_for_port(8082)!="7eba555d672a43887b6c6fe5374e383d8f3d263e":
            raise RuntimeError("Live admin changed since last review")
        if bg.release_sha_for_port(8081)!="62ab229a74a576b35b7666d5d0028c26bb42dea0":
            raise RuntimeError("Inactive admin changed since last review")
        old_bot=bot.current_pid()
        if old_bot<=0 or bot.poller_pids()!=[old_bot]:
            raise RuntimeError("Bot polling process not healthy/singleton")
        if not bg.health(8081) or not bg.health(8082):
            raise RuntimeError("Both admin slots must be healthy before stage")
        plan=updater.validate(fetch=False,reviewed_support=True)
        if plan["target"]!=SHA or not plan["reviewed_support"]:
            raise RuntimeError("Reviewed migration compatibility gate did not pass")
        updater.print_plan(plan)
        print("STAGING INACTIVE ADMIN 8081; PUBLIC ADMIN REMAINS 8082",flush=True)
        bg.stage(SHA,allow_bot_changes=True,reviewed_support=True)
        if bg.routing(bg.nginx_path().read_text())!=8082:
            raise RuntimeError("Critical: unexpected public Nginx route")
        if not bg.health(8082) or not bg.health(8081):
            raise RuntimeError("Post-stage health check failed; do not cut over")
        if bg.release_sha_for_port(8081)!=SHA:
            raise RuntimeError("Inactive service does not use pinned release")
        if bot.current_pid()!=old_bot or bot.poller_pids()!=[old_bot]:
            raise RuntimeError("Critical: Telegram bot PID changed during staging")
        print("STAGE VERIFIED: candidate on 8081, public 8082 unchanged, bot PID unchanged.")
        print("DO NOT CUT OVER until operator access and startup logs are reviewed.")

if __name__=="__main__":
    try:
        main()
    except Exception as exc:
        print("SAFE STAGE STOPPED:",type(exc).__name__,str(exc).splitlines()[0][:300],file=sys.stderr)
        sys.exit(1)
