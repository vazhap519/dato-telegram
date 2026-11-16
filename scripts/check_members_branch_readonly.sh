#!/usr/bin/env bash
# Read-only check of the members Mini App candidate. No checkout, DB writes,
# service restart, Nginx change, or Telegram API call.
set -Eeuo pipefail
cd /opt/dato-telegram
branch="feature/members-cards-actions-20261116"
temp="$(mktemp -d /tmp/dato-members-check.XXXXXXXX)"
trap 'rm -rf "$temp"' EXIT
git fetch --no-tags origin "refs/heads/${branch}:refs/remotes/origin/${branch}"
sha="$(git rev-parse --verify "refs/remotes/origin/${branch}")"
echo "MEMBERS CANDIDATE: $sha"
echo "== Source snapshot =="
git archive "$sha" | tar -xf - -C "$temp"
echo "== Compile =="
python3 -m compileall -q "$temp/admin" "$temp/db" "$temp/tests/test_members_miniapp.py"
echo "== Members security regression =="
python3 -m unittest discover -s "$temp/tests" -p 'test_members_miniapp.py' -v
echo "== Existing support regression =="
python3 -m unittest discover -s "$temp/tests" -p 'test_support_permissions.py' -v
echo "== Mini App HTML syntax =="
DATO_HTML="$temp/admin/templates/members_miniapp.html" /opt/dato-telegram/.venv/bin/python - <<'PY'
import os
from pathlib import Path
from html.parser import HTMLParser
class Parser(HTMLParser):
    pass
source=Path(os.environ["DATO_HTML"]).read_text(encoding="utf-8")
Parser().feed(source)
assert "/tg-support/api" in source
assert "Управление доступом" in source
print("HTML parsed. Member management controls present.")
PY
echo "== Mini App JavaScript syntax =="
if command -v node >/dev/null 2>&1; then
  DATO_HTML="$temp/admin/templates/members_miniapp.html" DATO_JS="$temp/members-inline.js" python3 - <<'PY'
import os,re
from pathlib import Path
html=Path(os.environ["DATO_HTML"]).read_text(encoding="utf-8")
scripts=re.findall(r'<script>(.*?)</script>',html,re.S)
scripts=[s for s in scripts if s.strip()]
assert len(scripts)==1,"Expected one inline application script"
Path(os.environ["DATO_JS"]).write_text(scripts[0],encoding="utf-8")
PY
  node --check "$temp/members-inline.js"
else
  echo "Node.js is unavailable: JavaScript syntax check skipped"
fi
echo "== Existing DB schema (read only) =="
/opt/dato-telegram/.venv/bin/python - <<'PY'
import sys
sys.path.insert(0,"/opt/dato-telegram/scripts")
import reliability
import psycopg2
reliability.load_env()
pg=reliability.postgres_environment()
params={"host":pg["PGHOST"],"port":pg["PGPORT"],"user":pg["PGUSER"],
        "dbname":pg["PGDATABASE"],"connect_timeout":8}
if "PGPASSWORD" in pg:params["password"]=pg["PGPASSWORD"]
with psycopg2.connect(**params) as conn:
    conn.set_session(readonly=True)
    with conn.cursor() as cur:
        names=("users","subscriptions","subscription_plans","access_grants",
               "verified_payment_receipts","subscription_plan_selections")
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema() AND table_name = ANY(%s)",(list(names),))
        found={v[0] for v in cur.fetchall()}
        missing=set(names)-found
        if missing:raise RuntimeError("Missing tables: "+", ".join(sorted(missing)))
        cur.execute("""SELECT table_name,column_name FROM information_schema.columns
           WHERE table_schema=current_schema() AND table_name = ANY(%s)""",(list(names),))
        columns={}
        for table,col in cur.fetchall():columns.setdefault(table,set()).add(col)
        required={"verified_payment_receipts":{"telegram_id","provider","amount","currency","payment_status","verified_at"},
                  "subscriptions":{"telegram_id","status","paid_until","provider"},
                  "users":{"telegram_id","username","first_name","last_name"}}
        for name,needed in required.items():
            missing=needed-columns.get(name,set())
            if missing:raise RuntimeError("Missing fields in "+name+": "+", ".join(sorted(missing)))
        print("Existing membership and verified payment tables: OK")
PY
echo "== Server services (unchanged) =="
systemctl is-active dato-telegram-bot.service dato-telegram-admin-blue.service dato-telegram-admin-standby.service
echo "MEMBER CARDS AND ACTIONS PREFLIGHT PASSED. No deployment performed."
