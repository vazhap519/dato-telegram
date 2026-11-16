"""Source-level regression checks for read-only members Mini App.

No DB or Telegram credentials required. Live authorization and data integrity
must still be verified against a staged release.
"""
import ast
from datetime import datetime, timezone, timedelta
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]

def file(path):
    return (ROOT/path).read_text(encoding="utf-8")

class MembersMiniAppTests(unittest.TestCase):
    def test_all_changed_python_parses(self):
        for path in ("admin/main.py","admin/members_miniapp.py",
                     "admin/support_miniapp.py","db/subscriptions.py"):
            with self.subTest(path=path):
                ast.parse(file(path),filename=path)

    def test_owner_admin_only_and_signed_telegram_required(self):
        source=file("admin/members_miniapp.py")
        self.assertIn('staff["role"] not in ("owner","admin")',source)
        self.assertIn("Depends(require_staff)",source)
        self.assertIn("HTTPException(403",source)
        self.assertIn("require_members_access(staff)",source)

    def test_all_data_routes_require_members_permission(self):
        module=ast.parse(file("admin/members_miniapp.py"))
        api_routes=[]
        for node in module.body:
            if not isinstance(node,ast.AsyncFunctionDef):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec,ast.Call) or not isinstance(dec.func,ast.Attribute):
                    continue
                if dec.func.attr not in ("get","post","delete","patch","put"):
                    continue
                route=(dec.args[0].value if dec.args and isinstance(dec.args[0],ast.Constant) else "")
                if route.startswith("/api/members"):
                    api_routes.append((dec.func.attr,route,node))
        self.assertEqual(len(api_routes),6)
        expected_write={
            "/api/members/{telegram_id}/grant",
            "/api/members/{telegram_id}/extend",
            "/api/members/{telegram_id}/grants/{grant_id}/expiry",
            "/api/members/{telegram_id}/grants/{grant_id}/revoke",
        }
        for verb,route,node in api_routes:
            with self.subTest(route=route):
                self.assertEqual(verb,"post" if route in expected_write else "get")
                # staff=Depends(members_staff) is a positional-or-keyword
                # parameter, so its default is stored in args.defaults.
                # kw_defaults only contains keyword-only parameters.
                defaults=list(node.args.defaults)+[
                    arg for arg in node.args.kw_defaults if arg is not None
                ]
                self.assertTrue(any(
                    isinstance(arg,ast.Call)
                    and isinstance(arg.func,ast.Name)
                    and arg.func.id=="Depends"
                    and arg.args
                    and isinstance(arg.args[0],ast.Name)
                    and arg.args[0].id==("members_owner" if route in expected_write else "members_staff")
                    for arg in defaults
                ))

    def test_uses_verified_payment_ledger_and_existing_subscription_registry(self):
        source=file("admin/members_miniapp.py")
        self.assertIn("FROM verified_payment_receipts",source)
        self.assertIn("await people(",source)
        self.assertIn("await person(",source)
        self.assertIn("verified_payment_count",source)
        self.assertIn("payment_status",source)

    def test_no_sensitive_raw_user_objects_exposed(self):
        source=file("admin/members_miniapp.py")
        self.assertNotIn('"user":user',source)
        self.assertNotIn('"audit":audit',source)
        self.assertNotIn("provider_payment_id",source)

    def test_support_interface_link_is_role_gated(self):
        ui=file("admin/templates/support_miniapp.html")
        self.assertIn('id="members-link" class="hidden"',ui)
        self.assertIn('me.role==="owner"||me.role==="admin"',ui)
        self.assertIn('/tg-support/members',ui)

    def test_owner_can_manage_only_grants_not_payment_records(self):
        source=file("admin/members_miniapp.py")
        self.assertIn('staff["role"]!="owner"',source)
        self.assertIn("Depends(members_owner)",source)
        self.assertIn("await grant_access(",source)
        self.assertIn("await revoke_grant(",source)
        self.assertIn("await change_grant_expiry(",source)
        self.assertIn("await extend_community_access(",source)
        self.assertIn("require_confirm(payload.confirm",source)
        self.assertIn("send_group_invitation",source)
        self.assertIn("remove_after_revoke",source)
        self.assertNotIn("INSERT INTO verified_payment_receipts",source)
        self.assertNotIn("UPDATE subscriptions",source)

    def test_human_readable_access_cards(self):
        ui=file("admin/templates/members_miniapp.html")
        for phrase in ("Текущий доступ", "История доступа",
                       "Временный доступ", "Бессрочный доступ",
                       "Подтверждённых платежей", "Подтвердить продление",
                       "Отозвать доступ", "Выдать новый доступ"):
            self.assertIn(phrase,ui)
        self.assertNotIn('type="password"',ui)
        self.assertIn('id="manage"',ui)
        self.assertIn('data.can_manage',ui)

    def test_access_summary_behavior_with_real_dates(self):
        # Compile only the pure calculation helpers: no external DB or Bot.
        source=ast.parse(file("admin/members_miniapp.py"))
        wanted={"stamp","aware","grant_state","describe_access"}
        helpers=[node for node in source.body
                 if isinstance(node,ast.FunctionDef) and node.name in wanted]
        self.assertEqual({node.name for node in helpers},wanted)
        scope={"datetime":datetime,"timezone":timezone,"timedelta":timedelta}
        exec(compile(ast.Module(body=helpers,type_ignores=[]),
                     "<membership-helpers>","exec"),scope)
        now=datetime(2026,11,16,12,tzinfo=timezone.utc)
        start=now-timedelta(days=1)
        future=now+timedelta(days=20)
        def grant(kind="temporary",end=future,revoked=None):
            return {"grant_type":kind,"starts_at":start,
                    "ends_at":end,"revoked_at":revoked}
        describe=scope["describe_access"]
        self.assertEqual(describe([grant("lifetime",None)],[],now)["status"],"lifetime")
        self.assertEqual(describe([grant()],[],now)["basis"],"Ручной временный доступ")
        self.assertEqual(describe([grant(revoked=now)],[],now)["status"],"none")
        self.assertEqual(describe([],[{"status":"active","paid_until":future}],now)["basis"],"Подписка")
        combined=describe([grant()],[{"status":"active","paid_until":future}],now)
        self.assertEqual(combined["manual_count"],1)
        self.assertEqual(combined["paid_subscription_count"],1)
        self.assertEqual(describe([grant(end=now-timedelta(seconds=1))],[],now)["status"],"expired")

    def test_access_calculation_ignores_revoked_grants(self):
        source=file("admin/members_miniapp.py")
        self.assertIn('if grant["revoked_at"] is not None:return "revoked"',source)
        self.assertIn('if start>now:return "scheduled"',source)
        self.assertIn('if end is not None and end<=now:return "expired"',source)
        self.assertIn('active=[g for g in grants if grant_state(g,now)=="active"]',source)

if __name__=="__main__":
    unittest.main()
