"""Offline regressions for Telegram owner alert opt-in and deduplication."""
import ast
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
def read(path):return (ROOT/path).read_text(encoding="utf-8")

class OwnerAlertTests(unittest.TestCase):
    def test_python_compiles(self):
        for path in ("db/owner_alerts.py","admin/members_miniapp.py",
                     "scripts/owner_alert_worker.py","db/miniapp_analytics.py"):
            with self.subTest(path=path):
                ast.parse(read(path),filename=path)

    def test_settings_are_opt_in_and_owner_link_only(self):
        src=read("db/owner_alerts.py")
        self.assertIn("enabled BOOLEAN NOT NULL DEFAULT FALSE",src)
        self.assertIn("a.role='owner'",src)
        self.assertIn("a.enabled=TRUE",src)
        self.assertIn("support_telegram_links",src)
        self.assertIn("payment_after_id",src)
        self.assertIn("MAX(id)",src)
        self.assertIn("enabled_at",src)
        self.assertIn("ON CONFLICT(admin_id)",src)
        self.assertNotIn("INSERT INTO verified_payment_receipts",src)
        self.assertNotIn("UPDATE subscriptions",src)

    def test_repeats_cannot_spam(self):
        src=read("db/owner_alerts.py")
        self.assertIn("UNIQUE(admin_id,event_key)",src)
        self.assertIn("ON CONFLICT (admin_id,event_key)",src)
        self.assertIn("attempts<3",src)
        self.assertIn('"status":"sent" if success else "failed"',src)
        self.assertIn('if claimed is None:continue',src)
        self.assertIn('if dry_run:continue',src)

    def test_expiry_uses_effective_access_and_filters_old(self):
        src=read("db/owner_alerts.py")
        self.assertIn("ACCESS_CTE",src)
        self.assertIn("WHERE active AND NOT lifetime",src)
        self.assertIn("expires_at<=NOW()+INTERVAL '3 days'",src)
        self.assertIn("revoked_at IS NULL",src)
        self.assertIn("a.ended>=:start",src)
        self.assertIn("recipient[\"enabled_at\"]",src)
        self.assertIn("NOT EXISTS (",src)
        self.assertIn("TZ=ZoneInfo(\"Asia/Tbilisi\")",src)

    def test_payments_are_verified_events_only(self):
        src=read("db/owner_alerts.py")
        self.assertIn("v.payment_status='paid'",src)
        self.assertIn("v.id>:cursor",src)
        self.assertIn("v.verified_at>=:start",src)
        self.assertIn("payment_status IN ('refunded','chargeback')",src)

    def test_owner_only_api(self):
        src=read("admin/members_miniapp.py")
        for route in ('"/api/owner-alerts"','Depends(members_owner)',
                      "configure_owner_alerts","owner_settings"):
            self.assertIn(route,src)
        ui=read("admin/templates/analytics_miniapp.html")
        for marker in ('id="owner-alerts"','id="alert-enabled"',
                       'id="alert-save"','if(me.role==="owner")await loadAlerts()',
                       '"X-Telegram-Init-Data"'):
            self.assertIn(marker,ui)

    def test_deployment_scripts_parse_and_are_explicit_opt_in(self):
        for name in ("prepare_owner_alerts.py","stage_owner_alerts_8081.py",
                     "cutover_owner_alerts_8081.py","install_owner_alert_timer.py"):
            path="scripts/"+name
            with self.subTest(path=path):
                ast.parse(read(path),filename=path)
        stage=read("scripts/stage_owner_alerts_8081.py")
        self.assertIn("OWNER ALERT ADDITIVE TABLES READY",stage)
        self.assertIn("RESTORING PREVIOUS STANDBY",stage)
        self.assertNotIn("start_polling",stage)
        cutover=read("scripts/cutover_owner_alerts_8081.py")
        self.assertIn('add_argument("--cutover",action="store_true")',cutover)
        self.assertIn("wait_route(False)",cutover)
        timer=read("scripts/install_owner_alert_timer.py")
        self.assertIn('add_argument("--install",action="store_true"',timer)
        self.assertIn('scripts.owner_alert_worker --run',timer)
        self.assertIn("No Telegram polling bot started",timer)

    def test_no_second_polling_worker(self):
        src=read("scripts/owner_alert_worker.py")
        self.assertIn("--dry-run",src)
        self.assertIn("--run",src)
        self.assertNotIn("start_polling",src)
        self.assertNotIn("Dispatcher",src)
        self.assertNotIn("run_polling",src)

if __name__=="__main__": unittest.main()
