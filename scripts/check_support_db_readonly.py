#!/usr/bin/env python3
"""Read-only production DB schema preflight for multiple support operators.

Reads metadata and aggregate counts only. Does not expose staff identities,
Telegram IDs, usernames, bot credentials, or application secrets.
"""
import sys
from pathlib import Path

APP = Path("/opt/dato-telegram")
sys.path.insert(0, str(APP / "scripts"))

import reliability
import psycopg2

EXPECTED = {
    "admins": {
        "id", "username", "role", "enabled", "session_version",
        "support_access", "support_can_reply", "support_can_change_status",
        "support_can_close", "support_can_delete"
    },
    "support_telegram_links": {
        "admin_id", "telegram_id", "notifications_enabled", "notifications_since",
        "reply_ticket_id", "updated_at"
    },
    "support_tickets": {"id", "telegram_id", "status", "updated_at"},
    "support_messages": {
        "id", "ticket_id", "actor", "body", "delivery_status", "media_type",
        "file_id", "created_at"
    },
    "admin_operation_audit": {"actor", "action", "target", "details"},
}

def main():
    reliability.load_env()
    pg = reliability.postgres_environment()
    args = {
        "host": pg["PGHOST"], "port": pg["PGPORT"], "user": pg["PGUSER"],
        "dbname": pg["PGDATABASE"], "connect_timeout": 8,
        "application_name": "dato_support_readonly_preflight",
    }
    if "PGPASSWORD" in pg:
        args["password"] = pg["PGPASSWORD"]
    if "PGSSLMODE" in pg:
        args["sslmode"] = pg["PGSSLMODE"]
    if "PGSSLROOTCERT" in pg:
        args["sslrootcert"] = pg["PGSSLROOTCERT"]
    with psycopg2.connect(**args) as conn:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT table_name,column_name FROM information_schema.columns
                WHERE table_schema=current_schema() AND table_name=ANY(%s)
            """, (list(EXPECTED),))
            found = {}
            for table, column in cur.fetchall():
                found.setdefault(table, set()).add(column)
            print("=== SUPPORT DATABASE COMPATIBILITY (READ ONLY) ===")
            missing_total = 0
            for table, needed in EXPECTED.items():
                missing = sorted(needed - found.get(table, set()))
                missing_total += len(missing)
                if missing:
                    print(f"{table}: MISSING ({', '.join(missing)})")
                else:
                    print(f"{table}: OK")
            if "admins" in found:
                cur.execute("SELECT COUNT(*) FROM admins")
                print("Staff accounts (aggregate):", cur.fetchone()[0])
            if "support_telegram_links" in found:
                cur.execute("SELECT COUNT(*) FROM support_telegram_links")
                print("Telegram staff bindings (aggregate):", cur.fetchone()[0])
            if "support_tickets" in found:
                cur.execute("SELECT COUNT(*) FROM support_tickets")
                print("Support tickets (aggregate):", cur.fetchone()[0])
            print("Missing required schema fields:", missing_total)
            print("No database writes were performed.")
            if missing_total:
                print("RESULT: MIGRATION REVIEW REQUIRED before any deployment")
                return 2
            print("RESULT: SCHEMA PREREQUISITES PRESENT")
            return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("PREFLIGHT FAILED:", type(exc).__name__, str(exc).splitlines()[0][:160], file=sys.stderr)
        sys.exit(1)
