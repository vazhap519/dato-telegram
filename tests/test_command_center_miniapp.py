"""Static route/role and data-boundary regressions for the Mini App hub."""
import ast
from pathlib import Path
import re
import unittest

ROOT=Path(__file__).resolve().parents[1]
def src(path):return (ROOT/path).read_text(encoding="utf-8")

class CommandCenterTests(unittest.TestCase):
    def test_python_parses(self):
        for path in ("db/miniapp_command_center.py","admin/support_miniapp.py",
                     "admin/members_miniapp.py"):
            with self.subTest(path=path):ast.parse(src(path),filename=path)

    def test_api_is_telegram_signed_and_role_scoped(self):
        tree=ast.parse(src("admin/support_miniapp.py"))
        found={}
        for fn in tree.body:
            if not isinstance(fn,ast.AsyncFunctionDef):continue
            for deco in fn.decorator_list:
                if isinstance(deco,ast.Call) and deco.args and isinstance(deco.args[0],ast.Constant):
                    found[deco.args[0].value]=fn
        self.assertIn("/api/home",found)
        fn=found["/api/home"]
        self.assertEqual(fn.name,"command_centre_data")
        deps=fn.args.defaults+list(filter(None,fn.args.kw_defaults))
        self.assertTrue(any(isinstance(d,ast.Call) and getattr(d.func,"id",None)=="Depends"
                            and d.args and getattr(d.args[0],"id",None)=="require_staff"
                            for d in deps))
        self.assertIn('staff["role"] in ("owner","admin")',ast.unparse(fn))
        self.assertIn("return await command_overview",ast.unparse(fn))

    def test_queries_read_only_and_no_payments_as_subscriptions(self):
        code=src("db/miniapp_command_center.py")
        self.assertIn("SET TRANSACTION READ ONLY",code)
        self.assertIn("TOTALS_SQL",code)
        self.assertIn("payment_status='paid'",code)
        self.assertIn("COUNT(*) AS count",code)
        self.assertIn("if include_members:",code)
        for bad in ("UPDATE ","INSERT INTO ","DELETE FROM ","CREATE TABLE","DROP TABLE","BOT_TOKEN"):
            self.assertNotIn(bad,code)

    def test_ui_contains_accessible_routes_and_deep_links(self):
        ui=src("admin/templates/command_center_miniapp.html")
        for marker in ('X-Telegram-Init-Data','api("/me")','api("/home")',
                       "/tg-support?view=tickets","/tg-support/members","/tg-support/analytics",
                       'id="tickets"','id="active"','id="payments"',
                       "/tg-support?ticket=",'me.role==="owner"||me.role==="admin"'):
            self.assertIn(marker,ui)
        self.assertNotIn("innerHTML",ui)

    def test_default_bot_miniapp_opens_hub_but_tickets_remain_accessible(self):
        support=src("admin/templates/support_miniapp.html")
        self.assertIn('window.location.replace("/tg-support/home"+window.location.hash)',support)
        self.assertIn('!params.has("view")&&!params.has("ticket")',support)
        self.assertIn('await openTicket(Number(ticket))',support)
        self.assertIn('id="home-link"',support)
        self.assertIn('"/tg-support?view=tickets"',src("admin/templates/members_miniapp.html"))
        for path in ("admin/templates/members_miniapp.html","admin/templates/analytics_miniapp.html"):
            self.assertIn('"/tg-support/home"',src(path))

if __name__=="__main__":unittest.main()
