"""Analytics invariants: no real DB, credentials or production writes needed."""
import ast
from pathlib import Path
import re
import unittest

ROOT=Path(__file__).resolve().parents[1]
def source(name):return (ROOT/name).read_text(encoding="utf-8")

class MiniAppAnalyticsTests(unittest.TestCase):
    def test_modules_parse(self):
        for path in ("db/miniapp_analytics.py","admin/members_miniapp.py","admin/main.py"):
            with self.subTest(path=path):ast.parse(source(path),filename=path)

    def test_api_is_signed_and_staff_role_gated(self):
        tree=ast.parse(source("admin/members_miniapp.py"))
        routes={}
        for fn in tree.body:
            if isinstance(fn,ast.AsyncFunctionDef):
                for d in fn.decorator_list:
                    if isinstance(d,ast.Call) and d.args and isinstance(d.args[0],ast.Constant):
                        routes[d.args[0].value]=fn
        self.assertIn("/api/analytics",routes)
        fn=routes["/api/analytics"]
        self.assertEqual(fn.name,"analytics_data")
        deps=list(fn.args.defaults)+[d for d in fn.args.kw_defaults if d is not None]
        self.assertTrue(any(isinstance(d,ast.Call) and isinstance(d.func,ast.Name)
                            and d.func.id=="Depends"
                            and isinstance(d.args[0],ast.Name)
                            and d.args[0].id=="members_staff" for d in deps))
        members=source("admin/members_miniapp.py")
        self.assertIn('staff["role"] not in ("owner","admin")',members)
        self.assertIn("Depends(require_staff)",members)
        self.assertIn('period not in ("30d","all")',members)

    def test_analytics_has_no_mutations(self):
        content=source("db/miniapp_analytics.py")
        for disallowed in ("INSERT INTO","UPDATE ","DELETE FROM","CREATE TABLE","ALTER TABLE","DROP TABLE"):
            self.assertNotIn(disallowed,content)
        self.assertIn("SET TRANSACTION READ ONLY",content)

    def test_effective_rights_exclude_revoked_and_expired(self):
        sql=source("db/miniapp_analytics.py")
        for marker in ("revoked_at IS NULL","starts_at<=NOW()",
                       "ends_at IS NULL OR ends_at>NOW()",
                       "status='active' AND paid_until>NOW()",
                       "BOOL_OR(ends_at IS NULL) AS lifetime",
                       "GREATEST(g.grant_expiry,s.paid_expiry)",
                       "NOT lifetime AND expires_at>NOW()"):
            self.assertIn(marker,sql)
        self.assertIn("COUNT(*) FILTER(WHERE active)",sql)
        self.assertIn("COUNT(*) FILTER(WHERE lifetime)",sql)

    def test_paid_subscription_is_distinct_from_verified_receipts(self):
        sql=source("db/miniapp_analytics.py")
        self.assertIn("LEFT JOIN paid_users p",sql)
        self.assertIn("subscriber_with_verified_payment",sql)
        self.assertIn("COUNT(DISTINCT telegram_id)",sql)
        self.assertIn("payment_status='paid'",sql)
        self.assertIn("payment_status IN ('refunded','chargeback')",sql)
        self.assertIn("GROUP BY COALESCE(currency,'Не указана')",sql)
        self.assertNotIn("currency_conversion",sql)

    def test_ui_is_auth_gated_and_has_navigation(self):
        dash=source("admin/templates/analytics_miniapp.html")
        for value in ('X-Telegram-Init-Data',"/api/me",'/api/analytics?period=',
                      "Подписчики с оплатой","Истекает за 7 дней","Подтверждённые платежи",
                      "/tg-support/members?member=", "Asia/Tbilisi"):
            self.assertIn(value,dash)
        self.assertNotIn("innerHTML",dash)
        self.assertIn('id="analytics-link" class="hidden"',source("admin/templates/support_miniapp.html"))
        self.assertIn('me.role==="owner"||me.role==="admin"',source("admin/templates/support_miniapp.html"))
        self.assertIn('id="go-analytics"',source("admin/templates/members_miniapp.html"))

    def test_returns_no_payment_identifiers(self):
        sql=source("db/miniapp_analytics.py")
        for private in ('provider_payment_id','BOT_TOKEN','payment_webhook_events','user["password"]'):
            self.assertNotIn(private,sql)

if __name__=="__main__":unittest.main()
