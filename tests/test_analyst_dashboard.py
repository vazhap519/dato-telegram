"""Regression checks for the isolated read-only analyst workspace."""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def fn(path, name):
    tree = ast.parse(read(path), filename=path)
    nodes = [n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    if not nodes:
        raise AssertionError(f"Missing function {name} in {path}")
    return ast.get_source_segment(read(path), nodes[0])


class AnalystDashboardTests(unittest.TestCase):
    def test_analyst_has_only_dedicated_read_routes(self):
        allowed = fn("db/admin_access.py", "allowed")
        analyst_branch = allowed.split('if role=="analyst":', 1)[1].split("return False", 1)[0]
        self.assertIn('method=="GET"', analyst_branch)
        self.assertIn('path in ("/analyst","/analyst/export")', analyst_branch)
        self.assertNotIn('"/dashboard"', analyst_branch)
        self.assertNotIn('"/support"', analyst_branch)
        self.assertNotIn('"/events"', analyst_branch)
        self.assertNotIn('"/search"', analyst_branch)

    def test_login_redirects_analyst_to_own_panel(self):
        login = fn("admin/main.py", "login")
        self.assertIn('else "/analyst" if user.get("role")=="analyst"', login)

    def test_dedicated_panel_and_aggregate_export_exist(self):
        main = read("admin/main.py")
        self.assertIn('@app.get("/analyst")', main)
        self.assertIn('@app.get("/analyst/export")', main)
        export = fn("admin/main.py", "analyst_export")
        self.assertIn("analytics_report(period)", export)
        self.assertNotIn("export_subscribers()", export)
        self.assertNotIn("export_verified_payments()", export)

    def test_analyst_template_is_standalone_and_read_only(self):
        template = read("admin/templates/analyst_dashboard.html")
        self.assertNotIn('_sidebar.html', template)
        self.assertIn("Панель аналитика", template)
        self.assertIn("/analyst/export?period=", template)
        self.assertIn("7 дней", template)
        self.assertIn("30 дней", template)
        self.assertIn("90 дней", template)
        self.assertIn("180 дней", template)
        self.assertIn("режим просмотра", template)

    def test_role_specific_telegram_menu(self):
        menu = fn("admin/main.py", "update_staff_telegram_menu")
        self.assertIn('role == "analyst"', menu)
        self.assertIn("Панель аналитика", menu)
        self.assertIn("https://bot.arutyun.me/analyst", menu)
        self.assertIn("MenuButtonDefault()", menu)

        change = fn("db/admin_access.py", "staff_change")
        permissions = fn("db/admin_access.py", "support_permissions_change")
        self.assertIn('"role": role', change)
        self.assertIn('account["role"] == "analyst"', permissions)
        self.assertIn('"role": account["role"]', permissions)

    def test_analytics_period_is_validated_and_parameterized(self):
        report = fn("db/admin_analytics.py", "report")
        self.assertIn("(7, 30, 90, 180)", report)
        self.assertIn(":period_days * INTERVAL '1 day'", report)
        self.assertIn("registrations_by_day", report)
        self.assertIn('"period_days":period_days', report)

    def test_analyst_navigation_is_not_admin_navigation(self):
        sidebar = read("admin/templates/_sidebar.html")
        self.assertIn("('/analyst','▥','Панель аналитика')", sidebar)
        self.assertNotIn("('/events','▤','Журнал событий')", sidebar.split("{% else %}")[0])


if __name__ == "__main__":
    unittest.main()
