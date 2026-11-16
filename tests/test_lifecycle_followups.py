"""Lifecycle sender config, signed owner API, inactive gate, queue safety."""
import ast
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
def source(p):return (ROOT/p).read_text(encoding="utf-8")

class LifecycleTests(unittest.TestCase):
    def test_python_parse(self):
        for p in ("db/lifecycle_followups.py","admin/main.py",
                  "admin/members_miniapp.py","scripts/lifecycle_scheduler.py"):
            with self.subTest(path=p):ast.parse(source(p),filename=p)

    def test_optin_and_reliability(self):
        s=source("db/lifecycle_followups.py")
        for marker in ("enabled BOOLEAN NOT NULL DEFAULT FALSE",
                       "CHECK(enabled=FALSE OR kind <> 'inactive')",
                       "enabled_at IS NOT NULL","a.created_at>=r.enabled_at",
                       "lifecycle_followup_deliveries",
                       "PRIMARY KEY(kind,source_audit_id)",
                       "ON CONFLICT DO NOTHING RETURNING source_audit_id",
                       "broadcast_recipients","broadcast_opt_out",
                       "pg_advisory_xact_lock",
                       "new_subscription","renewed_subscription",
                       "prior.id<a.id"):
            self.assertIn(marker,s)
        self.assertNotIn("bot.send_message",s)
        self.assertNotIn("asyncio.create_task",s)

    def test_no_speculative_inactivity(self):
        s=source("db/lifecycle_followups.py")
        self.assertIn('SUPPORTED={"welcome","renewal"}',s)
        self.assertIn('if enabled and kind not in SUPPORTED',s)
        self.assertIn('"blocked":"no_reliable_activity_signal"',s)
        self.assertNotIn("last_seen",s)
    
    def test_owner_only_update_and_staff_read(self):
        tree=ast.parse(source("admin/members_miniapp.py"))
        found={}
        for fn in tree.body:
            if not isinstance(fn,ast.AsyncFunctionDef):continue
            for dec in fn.decorator_list:
                if isinstance(dec,ast.Call) and dec.args and isinstance(dec.args[0],ast.Constant):
                    found[(getattr(dec.func,"attr",""),dec.args[0].value)]=ast.unparse(fn)
        self.assertIn("Depends(members_staff)",found[("get","/api/automations/lifecycle")])
        self.assertIn("Depends(members_owner)",found[("post","/api/automations/lifecycle/{kind}")])
        self.assertIn("payload.confirm",found[("post","/api/automations/lifecycle/{kind}")])
        self.assertIn("sample_telegram_ids",found[("get","/api/automations/lifecycle")])

    def test_ui_does_not_claim_sender_active(self):
        ui=source("admin/templates/automation_center_miniapp.html")
        for marker in ("id=\"lifecycle-rules\"","loadLifecycle()",
                       "SAVE_LIFECYCLE","Планировщик отправки не подключён",
                       "Не реализовано"):
            self.assertIn(marker,ui)
        self.assertNotIn("innerHTML",ui)

    def test_scheduler_not_running_automatically(self):
        s=source("scripts/lifecycle_scheduler.py")
        self.assertIn('parser.add_argument("--enqueue",action="store_true")',s)
        self.assertIn('dry_run_only=not execute',s)
        self.assertNotIn("start_polling",s)
        self.assertNotIn("send_message",s)

if __name__=="__main__":unittest.main()
