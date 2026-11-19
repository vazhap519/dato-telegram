"""Regression checks for the isolated read-only analyst workspace."""
import ast
from io import BytesIO
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
        self.assertIn("analyst_miniapp_router", main)
        miniapp = read("admin/analyst_miniapp.py")
        self.assertIn('prefix="/tg-support/analyst"', miniapp)
        self.assertIn("require_analyst", miniapp)
        self.assertIn("a.role='analyst'", miniapp)
        self.assertIn("a.enabled=TRUE", miniapp)
        self.assertIn('"/api/export"', miniapp)
        self.assertIn('"/api/report"', miniapp)
        self.assertIn('"/api/export-link"', miniapp)
        self.assertIn('"/api/export-file/{token}"', miniapp)
        self.assertIn('_sign_analyst_export_payload', miniapp)
        self.assertIn('_read_analyst_export_token', miniapp)
        self.assertIn('Access-Control-Allow-Origin', miniapp)
        self.assertIn('_build_analyst_xlsx', miniapp)
        self.assertIn('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', miniapp)
        self.assertIn('payload.get("format","csv")', miniapp)

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
        miniapp_template = read("admin/templates/analyst_miniapp.html")
        self.assertIn("/api/export-link?period=", miniapp_template)
        self.assertIn("tg.downloadFile", miniapp_template)
        self.assertIn("file_name:filename", miniapp_template)
        self.assertIn('data-export-format="xlsx"', miniapp_template)
        self.assertIn('data-export-format="csv"', miniapp_template)
        self.assertIn('format="+format', miniapp_template)

    def test_role_specific_telegram_menu(self):
        menu = fn("admin/main.py", "update_staff_telegram_menu")
        self.assertIn('role == "analyst"', menu)
        self.assertIn("Панель аналитика", menu)
        self.assertIn("https://bot.arutyun.me/tg-support/analyst", menu)
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

    def test_xlsx_export_has_separate_formatted_sheets_and_numeric_values(self):
        tree = ast.parse(read("admin/analyst_miniapp.py"))
        node = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == "_build_analyst_xlsx")
        namespace = {}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "analyst_miniapp.py", "exec"), namespace)
        sample = {
            "totals": {"total_users": 4, "registered_period": 3, "buyers": 1,
                       "paid_events": 1, "payments_period": 1, "repeat_buyers": 0, "reversals": 0},
            "registrations_by_day": [{"day": "2026-11-03", "count": 3}],
            "by_currency": [{"period": "2026-11-01", "provider": "Tribute",
                             "currency": "USD", "payments": 1, "gross_amount": 1.0, "reversals": 0}],
            "funnel": {"started": 4, "accepted": 2, "clicked": 1, "buyers": 1},
            "retention": {"repeat_30d": 0, "eligible_30d": 1, "repeat_90d": 0,
                          "eligible_90d": 0, "repeat_180d": 0, "eligible_180d": 0},
        }
        raw, filename = namespace["_build_analyst_xlsx"](sample, 30)
        from openpyxl import load_workbook
        wb = load_workbook(BytesIO(raw), data_only=True)
        self.assertEqual(filename.split(".")[-1], "xlsx")
        self.assertEqual(wb.sheetnames, ["Сводка", "Регистрации", "Оплаты", "Воронка и удержание"])
        self.assertEqual(wb["Сводка"]["B5"].value, 4)
        self.assertIsInstance(wb["Сводка"]["B5"].value, int)
        self.assertEqual(wb["Регистрации"]["A5"].value.strftime("%Y-%m-%d"), "2026-11-03")
        self.assertEqual(wb["Оплаты"]["E5"].value, 1)
        self.assertEqual(wb["Сводка"].freeze_panes, "A5")
        self.assertTrue(wb["Оплаты"].auto_filter.ref.startswith("A4:"))


if __name__ == "__main__":
    unittest.main()

