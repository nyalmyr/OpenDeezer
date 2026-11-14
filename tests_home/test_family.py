import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from aiohttp.test_utils import make_mocked_request
from opendeezer_family import FamilyProvider
from opendeezer_family.mixer import build_mix

def tracks(prefix,n=60,artist=None):
    return [{"id":str(prefix+i),"name":f"Titre {i}","artists":[artist or f"Artiste {i}"],"duration":180,"uri":f"test://track/{prefix+i}","playlists":["Chill"]} for i in range(n)]

class MixerTests(unittest.TestCase):
    def test_weighted_fairness_and_no_duplicates(self):
        result=build_mix({"A":tracks(100),"B":tracks(200)}, {"A":2,"B":1}, {},minutes=90)
        self.assertEqual(result["contributions"],{"A":20,"B":10})
        self.assertEqual(len({t["id"] for t in result["tracks"]}),30)
    def test_ban_is_household_veto(self):
        result=build_mix({"A":tracks(100),"B":tracks(100)}, {}, {"B":{"100":"ban"}},minutes=30)
        self.assertNotIn("100",[t["id"] for t in result["tracks"]])
    def test_shared_artist_explained(self):
        result=build_mix({"A":tracks(100,artist="Commun"),"B":tracks(200,artist="Commun")},{},{},minutes=5)
        self.assertIn("plusieurs",result["tracks"][0]["reason"])
    def test_insufficient_catalog_does_not_repeat(self):
        result=build_mix({"A":tracks(100,1),"B":tracks(100,1)},{},{},minutes=60)
        self.assertEqual(len(result["tracks"]),1)
        self.assertTrue(result["warnings"])
    def test_invalid_weights_and_empty(self):
        for weight in [0,-1,11,float('inf'),float('nan')]:
            with self.assertRaises(ValueError): build_mix({"A":tracks(1)},{"A":weight},{})
        with self.assertRaises(ValueError):build_mix({"A":[]},{},{})
    def test_deterministic_preview_without_mutating_catalog(self):
        catalog={"A":tracks(1)};before=json.dumps(catalog)
        self.assertEqual(build_mix(catalog,{},{},seed=7),build_mix(catalog,{},{},seed=7))
        self.assertEqual(before,json.dumps(catalog))
    def test_end_mood_uses_second_half(self):
        catalog=tracks(1,10)
        for i,t in enumerate(catalog):t["artists"]=[str(i)];t["playlists"]=["chill" if i<5 else "rock"]
        result=build_mix({"A":catalog},{},{},minutes=12,mood="chill",end_mood="rock")
        self.assertEqual([t["playlists"][0] for t in result["tracks"]],["chill","chill","rock","rock"])

class FamilyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        from music_assistant_models.provider import ProviderManifest
        manifest=ProviderManifest.from_dict({"domain":"opendeezer_family","type":"plugin","name":"Family","description":"Test","codeowners":[],"multi_instance":False})
        config=NS(instance_id="opendeezer_family",domain="opendeezer_family",name="Family",get_value=lambda *a:None)
        p1=NS(domain="opendeezer_home",profile_id="a",profile_label="Alice",instance_id="opendeezer_home--a",available=True)
        p2=NS(domain="opendeezer_home",profile_id="b",profile_label="Bob",instance_id="opendeezer_home--b",available=True)
        self.commands={}
        def register(name,handler,**kw):self.commands[name]=kw;return lambda:self.commands.pop(name,None)
        mass=NS(storage_path=self.tmp.name,cache=None,providers=[p1,p2],register_api_command=register,webserver=NS(register_dynamic_route=lambda *a:lambda:None),
            players=NS(all_players=lambda *a:[NS(player_id="salon",display_name="Salon",available=True)]),
            player_queues=NS(play_media=AsyncMock(),get=lambda q:None))
        self.p=FamilyProvider(mass,manifest,config,set());await self.p.loaded_in_mass()
        async def catalog(provider):return tracks(100 if provider.profile_id=="a" else 200)
        self.p.catalog=catalog
    async def asyncTearDown(self):await self.p.unload()
    async def test_api_scopes_no_anonymous_admin(self):
        for name,kw in self.commands.items():self.assertIsNotNone(kw["required_scope"])
        self.assertEqual(self.commands["opendeezer/family/pair_begin"]["required_scope"].value,"config.providers.write")
    async def test_compose_persist_feedback_play(self):
        await self.p.feedback("Alice","ban",track_id="100")
        mix=await self.p.compose(["Alice","Bob"],minutes=30)
        self.assertNotIn("100",[t["id"] for t in mix["tracks"]])
        result=await self.p.play(mix["id"],"Salon")
        self.assertEqual(result["speech"],"")
        saved=json.loads((Path(self.tmp.name)/"opendeezer-family.json").read_text())
        self.assertEqual(saved["last_mix"]["a"]["mix"],mix["id"])
        self.assertEqual(self.p.mass.player_queues.play_media.await_count,1)
    async def test_missing_member_fails_without_playback(self):
        with self.assertRaises(Exception):await self.p.compose(["Alice","Unknown"])
        self.assertEqual(self.p.mass.player_queues.play_media.await_count,0)
    async def test_pair_cancellation_and_expiry(self):
        pair=await self.p.pair_begin("Alice")
        self.assertEqual(await self.p.pair_finish(pair["ticket"]),{"waiting":True})
        await self.p.pair_cancel(pair["ticket"])
        with self.assertRaises(Exception):await self.p.pair_finish(pair["ticket"])
    async def test_pair_rejects_arbitrary_json(self):
        req=make_mocked_request("POST","/opendeezer-family/pair",headers={"Content-Length":"2"})
        req.json=AsyncMock(return_value=[])
        self.assertEqual((await self.p.web_handler(req)).status,400)
    async def test_pair_single_use(self):
        pair=await self.p.pair_begin("Alice")
        req=make_mocked_request("POST","/opendeezer-family/pair",headers={"Content-Length":"200","Origin":"chrome-extension://test"})
        req.json=AsyncMock(return_value={"ticket":pair["ticket"],"arl":"x"*64})
        self.assertEqual((await self.p.web_handler(req)).status,200)
        self.assertEqual((await self.p.web_handler(req)).status,403)
    async def test_diagnostics_no_identifiers(self):
        d=json.dumps(await self.p.diagnostics())
        self.assertNotIn("Alice",d);self.assertNotIn("profile_token",d)
