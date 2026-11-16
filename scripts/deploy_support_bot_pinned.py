#!/usr/bin/env python3
"""Pinned coordinated support bot update after successful admin cutover.

Default is READ-ONLY preflight. --deploy stops exactly one old bot poller,
starts one new bot poller, and relies on update_bot_safe for restore on errors.
The admin, Nginx, and production database schema are not modified.
"""
import argparse
import fcntl
import os
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT=Path("/opt/dato-telegram")
SHA="2d609a3267261e2370e01e02596916d1bdc044c6"
OLD_BOT="62ab229a74a576b35b7666d5d0028c26bb42dea0"
OLD_ADMIN="7eba555d672a43887b6c6fe5374e383d8f3d263e"
RELEASE=Path("/opt/dato-admin-releases")/SHA

def shell(*args):
    return subprocess.check_output(args,timeout=20,text=True).strip()

def code_at(url):
    try:
        with urlopen(url,timeout=8) as response:
            return response.status
    except HTTPError as exc:
        return exc.code

def polling_confirmed(pid):
    """Find startup markers for THIS PID in the full journal, not last N log lines.

    Frequent maintenance messages may push polling startup messages beyond the
    former 120-line lookback window. Require exact PID and aiogram markers.
    """
    if not isinstance(pid, int) or pid<=0:
        return False
    result=subprocess.run(
        ["journalctl", "--no-pager", "-o", "cat", f"_PID={pid}",
         "--grep=Run polling for bot|Start polling", "-n", "5"],
        capture_output=True, text=True, timeout=20,
    )
    return result.returncode==0 and any(
        marker in result.stdout for marker in ("Run polling for bot", "Start polling")
    )


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--deploy",action="store_true",
                        help="Explicitly switch singleton bot after all checks pass")
    args=parser.parse_args()
    if os.geteuid()!=0:
        raise RuntimeError("Root required")
    if shell("git","-C",str(ROOT),"status","--porcelain"):
        raise RuntimeError("Live working tree must be clean")
    if shell("git","-C",str(ROOT),"rev-parse","--verify",SHA)!=SHA:
        raise RuntimeError("Pinned release commit unavailable")
    if not (RELEASE/".venv/.install-ok").is_file() or not (RELEASE/".bot-venv/.prepared").is_file():
        raise RuntimeError("Prebuilt release dependencies missing")
    if not (RELEASE/".bot-venv/bin/python").is_file():
        raise RuntimeError("Prepared bot Python missing")

    # Import the reviewed update logic from the immutable deployed release,
    # not the mutable working tree or outdated installed helper.
    sys.path.insert(0,str(RELEASE/"scripts"))
    import deploy_admin_bluegreen as bg
    import update_bot_safe as bot
    import update_system_safe as system
    bg.target_commit=lambda:SHA
    bot.origin=lambda:SHA
    # The pinned update_bot_safe uses only the last 120 journal entries.
    # Replace that single diagnostic with an exact-PID full-journal query.
    # All singleton checks, lock ownership and rollback logic remain intact.
    bot.recent_polling=polling_confirmed

    bg.LOCK.parent.mkdir(parents=True,exist_ok=True)
    with bg.LOCK.open("a+") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if bg.routing(bg.nginx_path().read_text())!=8081:
            raise RuntimeError("Public admin must be on 8081 before bot update")
        if bg.release_sha_for_port(8081)!=SHA or bg.release_sha_for_port(8082)!=OLD_ADMIN:
            raise RuntimeError("Admin release mismatch, refusing bot update")
        if not bg.health(8081) or not bg.health(8082):
            raise RuntimeError("Both admin instances must remain healthy")
        if code_at("https://bot.arutyun.me/tg-support")!=200:
            raise RuntimeError("Public Telegram support page is not healthy")
        if code_at("http://127.0.0.1:8081/tg-support/api/me")!=401:
            raise RuntimeError("Mini App auth check failed")
        if bot.bot_state()!="active":
            raise RuntimeError("Bot unit is inactive")
        pid=bot.current_pid()
        if pid<=0 or bot.poller_pids()!=[pid] or not bot.recent_polling(pid):
            raise RuntimeError("Expected one healthy supervised polling bot")
        old=bot.current_ref()
        if old not in (OLD_BOT,SHA):
            raise RuntimeError("Bot changed unexpectedly since stage review")
        plan=system.validate(fetch=False,require_resources=bool(args.deploy),reviewed_support=True)
        if plan["target"]!=SHA or not plan["reviewed_support"]:
            raise RuntimeError("Migration compatibility attestation failed")
        system.print_plan(plan)
        print("PRECHECK OK: admin 8081 pinned, bot singleton PID",pid,"at",old[:12],flush=True)
        if not args.deploy:
            print("READ-ONLY COMPLETE. Deploy requires explicit --deploy argument.")
            return
        if old==SHA:
            print("NOOP: bot already uses pinned release.")
            return
        print("UPDATING SINGLE TELEGRAM POLLER:",old[:12],"->",SHA[:12],flush=True)
        # This routine itself verifies readiness and restores prior service
        # configuration on failure; no second poller is started concurrently.
        bot.deploy(SHA,reviewed_support=True)
        if bot.current_ref()!=SHA or bot.poller_pids()!=[bot.current_pid()]:
            raise RuntimeError("Post-deploy bot singleton verification failed")
        if not bot.recent_polling(bot.current_pid()):
            raise RuntimeError("Post-deploy Telegram polling readiness missing")
        if bg.routing(bg.nginx_path().read_text())!=8081 or not bg.health(8081):
            raise RuntimeError("Public admin changed or became unhealthy")
        print("BOT AND ADMIN ALIGNED:",SHA,flush=True)
        print("NEXT: manually test owner-created staff, Telegram Mini App and reply delivery.")

if __name__=="__main__":
    try:
        main()
    except Exception as exc:
        print("SUPPORT BOT UPDATE STOPPED:",type(exc).__name__,str(exc).splitlines()[0][:320],file=sys.stderr)
        sys.exit(1)
