"""Regression checks for Telegram support operator access boundaries.

These checks require only the Python standard library and operate on the
repository source, so they run without database credentials or bot tokens.
End-to-end checks are still required before production rollout.
"""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def source(path):
    return (ROOT / path).read_text(encoding="utf-8")


def function_node(path, name):
    module = ast.parse(source(path), filename=path)
    matches = [n for n in ast.walk(module)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    if not matches:
        raise AssertionError(f"{path} does not define {name}")
    return matches[0]


def function_text(path, name):
    return ast.get_source_segment(source(path), function_node(path, name))


class SupportPermissionsTests(unittest.TestCase):
    def test_python_files_parse(self):
        for folder in ("admin", "bot", "db"):
            for path in (ROOT / folder).rglob("*.py"):
                with self.subTest(file=path.relative_to(ROOT)):
                    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def test_new_staff_denied_until_owner_approval(self):
        creation = function_text("db/admin_access.py", "staff_create")
        for required in ("support_access", "support_can_reply",
                         "support_can_change_status", "support_can_close",
                         "support_can_delete", "FALSE,FALSE,FALSE,FALSE,FALSE"):
            self.assertIn(required, creation)

    def test_owner_only_staff_grants(self):
        handler = function_text("admin/main.py", "change_staff_support_permissions")
        self.assertIn('user["role"]!="owner"', handler)
        self.assertIn("verify_csrf", handler)
        self.assertIn("support_permissions_change(", handler)
        self.assertIn("CHANGE_SUPPORT", handler)

    def test_telegram_revocation_is_checked_on_each_request(self):
        identity = function_text("db/support_telegram.py", "telegram_staff")
        self.assertIn("a.enabled=TRUE", identity)
        self.assertIn("a.support_access=TRUE", identity)
        auth = function_text("admin/support_miniapp.py", "require_staff")
        self.assertIn("telegram_staff(user_id)", auth)
        self.assertIn("hmac.compare_digest", auth)

    def test_no_notification_to_revoked_operators(self):
        notifications = function_text("db/support_telegram.py", "pending_notifications")
        self.assertIn("a.support_access=TRUE", notifications)
        self.assertIn("l.notifications_enabled=TRUE", notifications)

    def test_action_authorization_in_both_interfaces(self):
        mini_reply = function_text("admin/support_miniapp.py", "reply")
        mini_status = function_text("admin/support_miniapp.py", "change_status")
        web_status = function_text("admin/main.py", "support_change_status")
        web_auth = function_text("admin/main.py", "get_current_user")
        native_reply = function_text("bot/support_admin.py", "support_admin_reply")
        native_callback = function_text("bot/support_admin.py", "support_admin_callback")
        self.assertIn('staff["can_reply"]', mini_reply)
        self.assertIn('staff["can_close"]', mini_status)
        self.assertIn('staff["can_change_status"]', mini_status)
        self.assertIn('user["support_can_close"]', web_status)
        self.assertIn('user["support_can_change_status"]', web_status)
        self.assertIn("support_can_delete", web_auth)
        self.assertIn('staff["can_reply"]', native_reply)
        self.assertIn('staff["can_close"]', native_callback)

    def test_both_interfaces_share_ticket_history(self):
        mini = source("admin/support_miniapp.py")
        admin = source("admin/main.py")
        self.assertIn("ticket_detail(ticket_id)", mini)
        self.assertIn("ticket_detail(ticket_id)", admin)
        self.assertIn("admin_reply(ticket_id", mini)
        self.assertIn("admin_reply(ticket_id", admin)

    def test_owner_controls_are_present(self):
        markup = source("admin/templates/admin_staff.html")
        self.assertIn('action="/staff/support"', markup)
        for permission in ("support_access", "support_can_reply", "support_can_change_status",
                           "support_can_close", "support_can_delete", "support_notify_telegram"):
            self.assertIn(f'name="{permission}"', markup)


if __name__ == "__main__":
    unittest.main()
