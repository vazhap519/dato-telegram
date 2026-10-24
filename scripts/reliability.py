#!/usr/bin/env python3
"""Daily PostgreSQL backup, restore verification, and read-only health monitoring.

No automatic database restore, data deletion outside the private backup folder,
or automated Telegram group actions.
"""
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

APP_DIR = Path(os.getenv("DATO_APP_DIR", "/opt/dato-telegram"))
BACKUP_DIR = Path(os.getenv("DATO_BACKUP_DIR", "/var/backups/dato-telegram"))
STATE_DIR = Path(os.getenv("DATO_RELIABILITY_STATE_DIR", "/var/lib/dato-reliability"))
RETENTION = max(3, min(int(os.getenv("DATO_BACKUP_RETENTION", "14")), 90))
MAX_AGE_HOURS = max(1, int(os.getenv("DATO_BACKUP_MAX_AGE_HOURS", "36")))


def load_env():
    # Administrative configuration is sourced by the systemd service EnvironmentFile.
    # Fallback: read only the DATABASE_URL and BOT_TOKEN for manual invocations.
    path = Path(os.getenv("DATO_ENV_FILE", str(APP_DIR / ".env")))
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        if key not in {"DATABASE_URL", "BOT_TOKEN"} or key in os.environ:
            continue
        val = value.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        os.environ[key] = val


def postgres_environment():
    url = os.getenv("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL is not configured")
    parsed = urllib.parse.urlsplit(url)
    if not parsed.scheme.startswith("postgresql") or not parsed.hostname:
        raise RuntimeError("DATABASE_URL is not a valid PostgreSQL connection string")
    env = os.environ.copy()
    env["PGHOST"] = parsed.hostname
    env["PGPORT"] = str(parsed.port or 5432)
    env["PGUSER"] = urllib.parse.unquote(parsed.username or "")
    env["PGDATABASE"] = urllib.parse.unquote(parsed.path.lstrip("/"))
    if parsed.password is not None:
        env["PGPASSWORD"] = urllib.parse.unquote(parsed.password)
    params = urllib.parse.parse_qs(parsed.query)
    for key, target in (("sslmode", "PGSSLMODE"), ("sslrootcert", "PGSSLROOTCERT")):
        if key in params:
            env[target] = params[key][0]
    if not env["PGDATABASE"] or not env["PGUSER"]:
        raise RuntimeError("DATABASE_URL must include a database and username")
    return env


def run(cmd, **kwargs):
    return subprocess.run(cmd, check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, **kwargs)


def ensure_private(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def latest_backup():
    backups = list(BACKUP_DIR.glob("dato-*.dump")) if BACKUP_DIR.exists() else []
    return max(backups, key=lambda p: p.stat().st_mtime) if backups else None


def verify(path):
    if not path.is_file() or path.stat().st_size < 100:
        raise RuntimeError("Backup archive is missing or empty")
    run(["pg_restore", "--list", str(path)])
    return True


def backup():
    ensure_private(BACKUP_DIR)
    ensure_private(STATE_DIR)
    with (STATE_DIR / "backup.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        when = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        final = BACKUP_DIR / ("dato-" + when + ".dump")
        with tempfile.NamedTemporaryFile(prefix=".incomplete-", suffix=".dump",
                                         dir=BACKUP_DIR, delete=False) as temp:
            partial = Path(temp.name)
        try:
            os.chmod(partial, 0o600)
            run(["pg_dump", "--format=custom", "--no-owner",
                 "--no-privileges", "--file", str(partial)], env=postgres_environment())
            verify(partial)
            os.replace(partial, final)
            backups = sorted(BACKUP_DIR.glob("dato-*.dump"),
                             key=lambda p: p.stat().st_mtime, reverse=True)
            for obsolete in backups[RETENTION:]:
                obsolete.unlink()
            print("Backup created and archive verified:", final,
                  "bytes:", final.stat().st_size)
            return final
        finally:
            partial.unlink(missing_ok=True)



def restore_test(archive=None):
    """Restore into a new temporary sibling database, NEVER into production."""
    import secrets
    file = Path(archive) if archive else latest_backup()
    if file is None:
        raise RuntimeError("No archive for restore test")
    verify(file)
    env = postgres_environment()
    dbname = "dato_restore_test_" + secrets.token_hex(5)
    if not dbname.startswith("dato_restore_test_"):
        raise RuntimeError("Unsafe test database name")
    # The DB role must have CREATEDB to perform this optional test.
    created = False
    try:
        run(["createdb", "--template=template0", dbname], env=env, timeout=40)
        created = True
        restored_env = env.copy()
        restored_env["PGDATABASE"] = dbname
        run(["pg_restore", "--exit-on-error", "--no-owner", "--no-privileges",
             "--dbname", dbname, str(file)], env=restored_env, timeout=300)
        check = run(["psql", "-X", "-A", "-t", "-c",
                     "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'"],
                    env=restored_env, timeout=30)
        tables = int(check.stdout.strip() or b"0")
        if tables < 1:
            raise RuntimeError("Restored database contains no application tables")
        print("Restore test PASSED into temporary DB:", dbname, "tables:", tables)
    finally:
        if created:
            run(["dropdb", "--if-exists", dbname], env=env, timeout=40)
            print("Temporary restore database removed:", dbname)

def health():
    issues = []
    try:
        run(["pg_isready"], env=postgres_environment(), timeout=12)
    except Exception:
        issues.append("PostgreSQL is not ready")
    latest = latest_backup()
    if not latest:
        issues.append("No PostgreSQL backup exists")
    else:
        age = (dt.datetime.now(dt.timezone.utc).timestamp() -
               latest.stat().st_mtime) / 3600
        if age > MAX_AGE_HOURS:
            issues.append("Last backup is older than %d hours" % MAX_AGE_HOURS)
        else:
            try:
                verify(latest)
            except Exception:
                issues.append("Last backup archive cannot be read")
    url = os.getenv("DATO_HEALTH_URL", "https://bot.arutyun.me/login")
    try:
        request = urllib.request.Request(url, headers={"User-Agent":"dato-health/1.0"})
        with urllib.request.urlopen(request, timeout=12) as response:
            if not (200 <= response.status < 400):
                issues.append("Admin HTTP status: " + str(response.status))
    except Exception:
        issues.append("Admin HTTPS endpoint is unavailable")
    for service in os.getenv("DATO_MONITOR_SERVICES", "").split(","):
        service = service.strip()
        if service:
            import re
            if not re.fullmatch(r"[A-Za-z0-9@_.-]{1,100}\.service",service):
                issues.append("Invalid configured systemd service name")
                continue
            try:
                out = run(["systemctl", "is-active", service], timeout=10)
                if out.stdout.strip() != b"active":
                    issues.append("Service not active: " + service)
                    continue
            except Exception:
                issues.append("Service not active: " + service)
                continue
            # Only report presence of errors, not their potentially private content.
            try:
                logs = run(["journalctl", "-u", service, "--since", "20 minutes ago",
                            "--priority=err", "--no-pager", "--output=cat"], timeout=12)
                if logs.stdout.strip() and logs.stdout.strip() != b"-- No entries --":
                    issues.append("Recent error logs for service: " + service)
            except Exception:
                pass
    return issues


def notify(message):
    token = os.getenv("BOT_TOKEN", "")
    chat = os.getenv("DATO_ALERT_CHAT_ID", "")
    if not token or not chat:
        print("Telegram alert not configured (set DATO_ALERT_CHAT_ID)", file=sys.stderr)
        return False
    encoded = urllib.parse.urlencode({"chat_id":chat, "text":message}).encode()
    req = urllib.request.Request("https://api.telegram.org/bot" + token +
                                 "/sendMessage", data=encoded, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=12) as res:
            return res.status == 200
    except Exception:
        print("Telegram alert delivery failed", file=sys.stderr)
        return False


def monitor():
    ensure_private(STATE_DIR)
    issues = health()
    state_file = STATE_DIR / "last_alert.json"
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    fingerprint = json.dumps(issues, ensure_ascii=False)
    try:
        previous = json.loads(state_file.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        previous = {}
    if issues:
        print("HEALTH FAILURE:", "; ".join(issues), file=sys.stderr)
        if previous.get("fingerprint") != fingerprint or now - previous.get("at", 0) > 21600:
            if notify("🚨 Круг Души — проверка сервера:\n" + "\n".join("• " + v for v in issues)):
                state_file.write_text(json.dumps({"fingerprint":fingerprint,"at":now}))
                os.chmod(state_file, 0o600)
        return 1
    print("HEALTH OK: PostgreSQL, backup and HTTP endpoint checked")
    if previous.get("fingerprint"):
        notify("✅ Круг Души — PostgreSQL, резервные копии и сайт снова доступны.")
        state_file.write_text("{}")
        os.chmod(state_file, 0o600)
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("backup","verify","restore-test","monitor"))
    parser.add_argument("archive", nargs="?")
    args = parser.parse_args()
    load_env()
    if args.command == "backup":
        backup()
    elif args.command == "verify":
        path = Path(args.archive) if args.archive else latest_backup()
        if not path:
            raise RuntimeError("No backup archive found")
        verify(path)
        print("Archive manifest is readable:", path)
        print("Note: verifying an archive does not replace a test restore to a separate database.")
    elif args.command == "restore-test":
        restore_test(args.archive)
    else:
        return monitor()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("Reliability operation failed:", type(exc).__name__, str(exc)[:250],
              file=sys.stderr)
        if len(sys.argv)>1 and sys.argv[1]=="backup":
            notify("🚨 Круг Души: ежедневная резервная копия PostgreSQL не создана. Проверьте сервер.")
        sys.exit(1)
