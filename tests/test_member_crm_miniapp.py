"""CRM member card uses existing admin profile and unified audit; no parallel registry."""
import ast
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[1]
def read(path):return (ROOT/path).read_text(encoding="utf-8")

class MemberCrmTests(unittest.TestCase):
    def test_modules_parse(self):
        for path in ("admin/members_miniapp.py","db/subscriber_crm.py","db/subscriber_profile.py"):
            with self.subTest(path=path):ast.parse(read(path),filename=path)

    def test_read_is_signed_and_role_restricted(self):
        tree=ast.parse(read("admin/members_miniapp.py"))
        routes={}
        for fn in tree.body:
            if not isinstance(fn,ast.AsyncFunctionDef):continue
            for d in fn.decorator_list:
                if isinstance(d,ast.Call) and d.args and isinstance(d.args[0],ast.Constant):
                    routes[(d.func.attr,d.args[0].value)]=fn
        for name,method,dep in (("member_crm_detail","get","members_staff"),
                                ("member_crm_update","post","members_owner")):
            fn=routes[(method,"/api/members/{telegram_id}/crm")]
            self.assertEqual(fn.name,name)
            self.assertIn("Depends("+dep+")",ast.unparse(fn))
        self.assertIn("await person(telegram_id)",ast.unparse(routes[("get","/api/members/{telegram_id}/crm")]))
        self.assertIn("staff['role'] == 'owner'",ast.unparse(routes[("get","/api/members/{telegram_id}/crm")]))

    def test_existing_crm_reused(self):
        s=read("admin/members_miniapp.py")
        for marker in ("get_subscriber_profile","save_subscriber_profile","crm_timeline",
                       "FROM support_tickets","ORDER BY updated_at DESC,id DESC LIMIT 20",
                       '"limits":{"tickets":20,"timeline":60}'):
            self.assertIn(marker,s)
        self.assertNotIn("CREATE TABLE",s)
        self.assertNotIn("initialize_subscriber_profiles",s)
        self.assertIn("subscriber_profile_updated",read("db/subscriber_profile.py"))

    def test_ui_has_private_editor_and_read_only_admin_view(self):
        s=read("admin/templates/members_miniapp.html")
        for marker in ('id="crm-form"','id="crm-read"','id="crm-feedback"',
                       'id="crm-tickets"','id="crm-timeline"','"/members/"+uid+"/crm"',
                       'state.canEditCrm','await loadCrm(uid)',
                       '/tg-support?ticket=','window.location.hash'):
            self.assertIn(marker,s)
        self.assertNotIn("innerHTML",s)
        self.assertIn('renderCrmTickets(data.tickets)',s)
        self.assertIn('renderCrmTimeline(data.timeline)',s)

if __name__=="__main__":unittest.main()
