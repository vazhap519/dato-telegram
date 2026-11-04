#!/usr/bin/env python3
"""Offline syntax and Jinja preflight for the admin extensions.

Does not connect to production DB or send Telegram messages.
Run with the app virtualenv after pulling the GitHub update.
"""
from pathlib import Path
import compileall
import sys

root=Path(__file__).resolve().parent.parent
ok=all(compileall.compile_dir(str(root / folder),quiet=1,force=False)
       for folder in ("admin","bot","db","scripts"))
if not ok:
    raise SystemExit("Python compilation failed")

try:
    from jinja2 import Environment, FileSystemLoader
    env=Environment(loader=FileSystemLoader(str(root/"admin"/"templates")))
    for name in (
        "_sidebar.html","index.html","subscription_user.html",
        "subscriptions.html","broadcast.html","support_ticket.html",
        "crm_tasks.html","admin_search.html","admin_issues.html",
        "admin_operations.html","admin_analytics.html",
        "admin_staff.html","admin_automations.html"
    ):
        env.get_template(name)
        print("TEMPLATE OK:",name)
except Exception as exc:
    print("Template verification failed:",type(exc).__name__,str(exc))
    raise
print("PRE-FLIGHT PASSED: Python files and Jinja syntax")
