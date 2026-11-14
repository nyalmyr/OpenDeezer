import unittest
from unittest.mock import patch
from unittest.mock import AsyncMock
from types import SimpleNamespace
from opendeezer_home.setup_flow import run_setup, enroll, EnrollmentError
from test_provider import Response

class Session:
    def __init__(self, status=201, data=None):
        self.context=SimpleNamespace(setup_data={},instance_id=None)
        self.mass=SimpleNamespace(providers=[],http_session=self)
        self.status=status;self.data=data or {"id":"member1","label":"Alice","profile_token":"p"*64}
        self.forms=[];self.saved=None;self.requests=[]
    def post(self,url,**kw):
        self.requests.append((url,kw));return Response(self.data,self.status)
    async def form(self,entries,**kw):
        self.forms.append(entries)
        return {"profile_label":"Alice","deezer_arl":"a"*64}
    async def finish(self,values):self.saved=values

class SetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_existing_account_not_duplicated(self):
        s=Session()
        s.mass.config=SimpleNamespace(get_provider_configs=AsyncMock(return_value=[SimpleNamespace(instance_id="disabled")]),
            get=lambda key:{"setup_data":{"profile_token":"p"*64}},_decrypt_values=lambda values:values)
        with patch("pathlib.Path.read_text",return_value="admin-secret"):
            with self.assertRaisesRegex(EnrollmentError,"déjà configuré"):
                await enroll(s,"Alice","a"*64)
    async def test_add_private_form_and_minimal_persistence(self):
        s=Session()
        with patch("pathlib.Path.read_text",return_value="admin-secret"):
            await run_setup(s)
        self.assertEqual(s.saved["profile_label"],"Alice")
        self.assertNotIn("deezer_arl",s.saved)
        self.assertNotIn("admin-secret",str(s.saved))
        self.assertEqual(s.forms[0][-1].type.value,"secure_string")
        self.assertFalse(s.requests[0][1]["allow_redirects"])
    async def test_existing_account_not_duplicated(self):
        s=Session();s.mass.providers=[SimpleNamespace(domain="opendeezer_home",profile_id="member1",instance_id="existing")]
        with patch("pathlib.Path.read_text",return_value="admin-secret"):
            with self.assertRaisesRegex(EnrollmentError,"déjà connecté"):
                await enroll(s,"Alice","a"*64)
        self.assertIsNone(s.saved)
    async def test_expired_cookie_message_safe(self):
        s=Session(422,{"error":"invalid_session","unsafe":"should-not-appear"})
        with patch("pathlib.Path.read_text",return_value="admin-secret"):
            with self.assertRaisesRegex(EnrollmentError,"expirée") as cm:
                await enroll(s,"Alice","a"*64)
        self.assertNotIn("should-not-appear",str(cm.exception))
    async def test_reconfigure_retains_existing_secret(self):
        s=Session();s.context.setup_data={"profile_token":"old-secret","bridge_url":"http://127.0.0.1:8731"}
        async def form(*args,**kwargs):return {"profile_token":"","bridge_url":"http://127.0.0.1:8731"}
        s.form=form;await run_setup(s)
        self.assertEqual(s.saved["profile_token"],"old-secret");self.assertFalse(s.requests)

if __name__=="__main__":unittest.main()
