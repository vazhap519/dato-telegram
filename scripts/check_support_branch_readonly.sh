#!/usr/bin/env bash
# Read-only preflight for support-permissions changes.
# Does not checkout source into the live tree, touch PostgreSQL, switch Nginx,
# or restart Telegram/admin services. Only git remote refs/temp files change.
set -Eeuo pipefail
cd /opt/dato-telegram
target="feature/support-staff-permissions-20261113"
work="$(mktemp -d /tmp/dato-support-verify.XXXXXXXX)"
trap 'rm -rf "$work"' EXIT
echo "== Fetch candidate (no checkout) =="
git fetch --no-tags origin "refs/heads/${target}:refs/remotes/origin/${target}"
echo "== Candidate commit =="
git rev-parse "refs/remotes/origin/${target}"
echo "== Export code into isolated temp directory =="
git archive "refs/remotes/origin/${target}" | tar -x -C "$work"
echo "== Python syntax =="
python3 -m compileall -q "$work/admin" "$work/bot" "$work/db" "$work/scripts/update_system_safe.py" "$work/scripts/migrate_support_staff_permissions.py" "$work/scripts/check_support_db_readonly.py"
echo "== Support permissions regression tests =="
python3 -m unittest discover -s "$work/tests" -p 'test_support_permissions.py' -v
echo "== Jinja template syntax (from existing admin venv) =="
if [[ -x /opt/dato-telegram/.venv/bin/python ]]; then
  TEMPLATE_ROOT="$work/admin/templates" /opt/dato-telegram/.venv/bin/python - <<'PY'
import os
from pathlib import Path
from jinja2 import Environment
root = Path(os.environ["TEMPLATE_ROOT"])
for file in (root / "admin_staff.html", root / "support_ticket.html",
             root / "support_miniapp.html"):
    Environment().parse(file.read_text(encoding="utf-8"))
    print("OK", file.name)
PY
else
  echo "WARNING: admin .venv not found; Jinja check skipped"
fi
echo "== Candidate-specific deployment gate (read-only) =="
CANDIDATE_SOURCE="$work/scripts" CANDIDATE_SHA="$(git rev-parse "refs/remotes/origin/${target}")" \
  /opt/dato-telegram/.venv/bin/python - <<'PY'
import os, sys
sys.path.insert(0, os.environ["CANDIDATE_SOURCE"])
import update_system_safe as updater
import deploy_admin_bluegreen as bg
import update_bot_safe as bot
candidate = os.environ["CANDIDATE_SHA"]
bg.target_commit = lambda: candidate
bot.origin = lambda: candidate
plan = updater.validate(fetch=False, require_resources=False, reviewed_support=True)
if plan["target"] != candidate or not plan["reviewed_support"]:
    raise RuntimeError("Candidate support review gate did not verify")
updater.print_plan(plan)
print("CANDIDATE REVIEW GATE PASSED (no release operations performed)")
PY
echo "== Existing deployment safety gates (read-only) =="
if command -v update-dato-system >/dev/null 2>&1; then
  update-dato-system check
else
  echo "WARNING: update-dato-system not installed"
fi
echo "== Running services remain untouched =="
for unit in dato-telegram-bot.service dato-telegram-admin-blue.service dato-telegram-admin-standby.service; do
  systemctl show "$unit" -p ActiveState -p MainPID --no-pager 2>/dev/null || true
done
echo "PRECHECK FINISHED. No deploy or server process restart was performed."
