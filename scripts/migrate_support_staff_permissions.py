#!/usr/bin/env python3
"""One-time, guarded, additive support permissions migration.

Run as root with the existing admin venv:
  git show FETCH_HEAD:scripts/migrate_support_staff_permissions.py | /opt/dato-telegram/.venv/bin/python -

No application services are restarted. Before changing the production schema
the script creates a fresh PostgreSQL archive and restores it into a separate
temporary database using the project's reliability tool.
"""
import os
import sys
from pathlib import Path

if os.geteuid() != 0:
    raise SystemExit("ERROR: Run as root for isolated database restore test.")

ROOT = Path("/opt/dato-telegram")
sys.path.insert(0, str(ROOT / "scripts"))
import reliability
import psycopg2

COLUMNS = (
    "support_access", "support_can_reply", "support_can_change_status",
    "support_can_close", "support_can_delete",
)

def connection_arguments():
    reliability.load_env()
    env = reliability.postgres_environment()
    result = {
        "host": env["PGHOST"],
        "port": env["PGPORT"],
        "user": env["PGUSER"],
        "dbname": env["PGDATABASE"],
        "connect_timeout": 8,
        "application_name": "dato_support_permissions_migration",
    }
    if "PGPASSWORD" in env:
        result["password"] = env["PGPASSWORD"]
    if "PGSSLMODE" in env:
        result["sslmode"] = env["PGSSLMODE"]
    if "PGSSLROOTCERT" in env:
        result["sslrootcert"] = env["PGSSLROOTCERT"]
    return result

def column_info(cur):
    cur.execute("""
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema=current_schema() AND table_name='admins'
          AND column_name = ANY(%s)
    """, (list(COLUMNS),))
    return {name: (data_type, nullable, default) for name, data_type, nullable, default in cur.fetchall()}

def assert_existing_columns_compatible(info):
    for name, (data_type, nullable, default) in info.items():
        if data_type != "boolean" or nullable != "NO":
            raise RuntimeError(f"Incompatible existing column {name}: {data_type}, nullable={nullable}")

def main():
    params = connection_arguments()
    print("=== SUPPORT STAFF SCHEMA MIGRATION ===", flush=True)
    with psycopg2.connect(**params) as conn:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('admins') IS NOT NULL")
            if not cur.fetchone()[0]:
                raise RuntimeError("Required admins table is missing")
            current = column_info(cur)
            assert_existing_columns_compatible(current)
            needed = [c for c in COLUMNS if c not in current]
    if not needed:
        print("NOOP: all permission columns already exist. No backup/migration needed.")
        return
    print("Pending additive columns:", ", ".join(needed), flush=True)
    print("Creating fresh production backup using reliability.py...", flush=True)
    archive = reliability.backup()
    print("Testing FULL restore into isolated temporary PostgreSQL database...", flush=True)
    reliability.restore_test(str(archive))
    print("Backup and test restore PASSED. Starting short transactional migration...", flush=True)
    with psycopg2.connect(**params) as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = '5s'")
            cur.execute("SET LOCAL statement_timeout = '30s'")
            # Column names are constant strings defined in this source,
            # not externally supplied identifiers.
            for name in COLUMNS:
                cur.execute(
                    f"ALTER TABLE admins ADD COLUMN IF NOT EXISTS {name} BOOLEAN NOT NULL DEFAULT TRUE"
                )
            updated = column_info(cur)
            assert_existing_columns_compatible(updated)
            if len(updated) != len(COLUMNS):
                raise RuntimeError("Incomplete support permission schema")
        # Commit once, after all checks; any exception rolls everything back.
    with psycopg2.connect(**params) as conn:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            final = column_info(cur)
            assert_existing_columns_compatible(final)
            if set(final) != set(COLUMNS):
                raise RuntimeError("Post-commit verification failed")
    print("SUCCESS: all 5 permission columns are present, boolean and NOT NULL.")
    print("Existing admins retained. Application services were not restarted.")
    print("Next: deploy code only through separately reviewed coordinated release.")
    print("NOTE: DEFAULT TRUE preserves previous behavior for existing operators;")
    print("newly created staff are denied access explicitly by updated application code.")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("MIGRATION STOPPED:", type(exc).__name__, str(exc).splitlines()[0][:240], file=sys.stderr)
        sys.exit(1)
