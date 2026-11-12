#!/usr/bin/env python3
"""Read-only inventory for blue/green migration planning.

Does not edit config, restart services, or read credential values.
Only executes fixed, local inspection commands.
"""
import os
from pathlib import Path
import re
import subprocess
import sys

APP = Path("/opt/dato-telegram")
UPDATE = Path("/usr/local/bin/update-dato-telegram")
NGINX = Path("/etc/nginx/sites-enabled/dato-telegram")
SERVICES = ("dato-telegram-admin.service", "dato-telegram-bot.service")
SENSITIVE = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|PRIVATE_KEY|DATABASE_URL)",re.I)


def run(*args):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=12)
        return (p.stdout or "").strip() or f"(exit={p.returncode})"
    except Exception as exc:
        return f"({type(exc).__name__})"


def safe(value):
    value = re.sub(r"(?i)(https?://)[^/\\s@]+@",r"\\1***@",value)
    value = re.sub(r"(?i)(-p|--password|--token|--secret)\\s+[^\\s]+",r"\\1 [REDACTED]",value)
    return value[:350]


def main():
    print("=== REPOSITORY ===")
    print("Commit:",run("git","-C",str(APP),"rev-parse","--short","HEAD"))
    print("Working tree:",run("git","-C",str(APP),"status","--porcelain") or "clean")
    print("=== ADMIN/BOT SERVICES ===")
    for unit in SERVICES:
        print("Service:",unit)
        print("  Active:",run("systemctl","is-active",unit))
        print("  Load:",run("systemctl","show",unit,"-p","LoadState","--value"))
        for key in ("ExecStart","WorkingDirectory","User","EnvironmentFiles","FragmentPath"):
            value=run("systemctl","show",unit,"-p",key,"--value")
            if key=="ExecStart":
                value=re.sub(r"(?i)(BOT_TOKEN|SECRET_KEY|DATABASE_URL)=[^ ;}]+",r"\\1=[REDACTED]",value)
            print(" ",key+":",safe(value) if value else "-")
    print("=== UPDATER ===")
    if not UPDATE.is_file():
        print("not found")
    else:
        print("Present, bytes:", UPDATE.stat().st_size)
        print("SHA256:",run("sha256sum",str(UPDATE)).split()[0])
        print("Relevant non-secret operations (line numbers):")
        for number,line in enumerate(UPDATE.read_text(errors="replace").splitlines(),1):
            stripped=line.strip()
            if SENSITIVE.search(stripped):
                continue
            if re.search(r"\\b(git|rsync|systemctl|nginx|uvicorn|pip|venv|cp|mv|ln|backup|sleep|curl|flock|cd)\\b",stripped,re.I):
                print(f" {number}: {safe(stripped)}")
    print("=== NGINX ROUTING ===")
    if NGINX.is_file():
        print("Config present:",NGINX)
        for number,line in enumerate(NGINX.read_text(errors="replace").splitlines(),1):
            if re.search(r"server_name|location|proxy_pass|listen|upstream",line):
                if not SENSITIVE.search(line):
                    print(f" {number}: {safe(line.strip())}")
    print("=== APPLICATION FILES ===")
    for name in (".venv/bin/uvicorn",".env","requirements.txt","admin/main.py"):
        p=APP/name
        print(name, "present" if p.exists() else "missing")
    print("=== RESOURCES ===")
    print(run("df","-h",str(APP)).splitlines()[-1])
    print(run("free","-h").splitlines()[1])
    print("No changes were made.")


if __name__=="__main__":
    main()
