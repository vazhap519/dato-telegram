"""Source-level regression checks for read-only members Mini App.

No DB or Telegram credentials required. Live authorization and data integrity
must still be verified against a staged release.
"""
import ast
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
        self.assertEqual(len(api_routes),2)
        for verb,route,node in api_routes:
            with self.subTest(route=route):
                self.assertEqual(verb,"get")
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
                    and arg.args[0].id=="members_staff"
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

    def test_markup_does_not_offer_mutation_of_subscriptions(self):
        ui=file("admin/templates/members_miniapp.html")
        self.assertIn("Только просмотр",ui)
        self.assertNotIn('method:"POST"',ui)
        self.assertNotIn('type="password"',ui)

if __name__=="__main__":
    unittest.main()
