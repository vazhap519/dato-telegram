"""Source checks for Mini App existing-automation control centre."""
import ast
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
def get(path):return (ROOT/path).read_text(encoding="utf-8")

class AutomationCentreTests(unittest.TestCase):
    def test_parse(self):
        for path in ("db/miniapp_automation_center.py","admin/members_miniapp.py"):
            ast.parse(get(path),filename=path)
    def test_api_signed_and_role_scoped(self):
        tree=ast.parse(get("admin/members_miniapp.py"))
        routes={}
        for node in tree.body:
            if not isinstance(node,ast.AsyncFunctionDef):continue
            for d in node.decorator_list:
                if isinstance(d,ast.Call) and d.args and isinstance(d.args[0],ast.Constant):
                    routes[(getattr(d.func,"attr",""),d.args[0].value)]=node
        expected={
            ("get","/api/automations"):"members_staff",
            ("post","/api/automations/reminders/{trigger}"):"members_owner",
            ("post","/api/automations/expiry"):"members_owner",
        }
        for key,dependency in expected.items():
            with self.subTest(route=key):
                fn=routes[key]
                self.assertIn("Depends("+dependency+")",ast.unparse(fn))
                if key[0]=="post":self.assertIn("payload.confirm",ast.unparse(fn))
    def test_no_new_sender_or_secondary_ledger(self):
        src=get("db/miniapp_automation_center.py")
        for required in ("automation_overview","reminder_delivery_attention",
                         "automation_settings","save_automation_settings",
                         "TRIGGERS","set_rule","record_audit","SET TRANSACTION READ ONLY"):
            self.assertIn(required,src)
        for banned in ("CREATE TABLE","INSERT INTO broadcasts","enqueue_reminders",
                       "Bot(","send_message(","claim_expiry_alerts"):
            self.assertNotIn(banned,src)
        self.assertIn("settings[\"removal_enabled\"]",src)
    def test_staging_checks_post_route_using_post_not_get(self):
        script=get("scripts/rollout_automation_center.py")
        self.assertIn('def req(url,method="GET"):',script)
        self.assertIn('method="POST"',script)
        self.assertIn('b\'{"enabled":false,"confirm":"SAVE_EXPIRY_NOTICES"}\'',script)
        self.assertIn('req("http://127.0.0.1:8081/tg-support/api/automations/expiry",method="POST")[0]!=401',script)
        self.assertNotIn('                     "/tg-support/api/automations/expiry",\n',script)

    def test_ui_safe_and_no_false_claims(self):
        ui=get("admin/templates/automation_center_miniapp.html")
        for required in ("id=\"expiry-enabled\"","id=\"rules\"",
                         "id=\"problems\"","id=\"campaigns\"",
                         "X-Telegram-Init-Data","SAVE_REMINDER",
                         "SAVE_EXPIRY_NOTICES","/tg-support/home",
                         "Не реализовано","confirm("):
            self.assertIn(required,ui)
        self.assertNotIn("innerHTML",ui)
        home=get("admin/templates/command_center_miniapp.html")
        self.assertIn('id="automations" class="hidden"',home)
        self.assertIn('me.role==="owner"||me.role==="admin"',home)

if __name__=="__main__":unittest.main()
