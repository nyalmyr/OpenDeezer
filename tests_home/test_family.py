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
    async def test_saved_mix_survives_preview_eviction_and_applies_new_ban(self):
        preset=await self.p.preset_save("Morning","mix",["Alice"],player="Salon",minutes=12)
        original=self.p._state["presets"][preset["id"]]["tracks"]
        self.p._state["mixes"].clear()
        await self.p.feedback("Alice","ban",track_id=original[0]["id"])
        await self.p.preset_play("Morning")
        args=self.p.mass.player_queues.play_media.call_args.args
        self.assertEqual(len(args[1]),len(original)-1)
        self.assertNotIn(original[0]["uri"],args[1])
        with self.assertRaises(Exception):await self.p.preset_save("morning","mix",["Alice"])
    async def test_playlist_is_owned_by_selected_account(self):
        self.p.providers()[0]._api=AsyncMock(return_value=[{"ID":"42","Name":"Chill"}])
        with self.assertRaises(Exception):await self.p.preset_save("Bad","playlist",["Alice"],playlist_id="7")
        await self.p.preset_save("Good","playlist",["Alice"],playlist_id="42")
        with self.assertRaises(Exception):await self.p.preset_save("Two","flow",["Alice","Bob"])
    async def test_flow_preview_does_not_play_and_empty_does_not_fallback(self):
        self.p.providers()[0]._api=AsyncMock(return_value=[{"ID":"88","Name":"Flow track"}])
        result=await self.p.flow("Alice",preview=True)
        self.assertEqual(result["source"],"deezer_flow")
        self.assertEqual(self.p.mass.player_queues.play_media.await_count,0)
        await self.p.flow("Alice","Salon")
        self.assertEqual(self.p.mass.player_queues.play_media.call_args.args[1],["opendeezer_home--a://track/88"])
        self.p.providers()[0]._api=AsyncMock(return_value=[])
        with self.assertRaises(Exception):await self.p.flow("Alice","Salon")
        self.assertEqual(self.p.mass.player_queues.play_media.await_count,1)
    async def test_delete_preset_does_not_touch_music_library(self):
        p=await self.p.preset_save("Morning","mix",["Alice"])
        await self.p.preset_delete(p["id"])
        self.assertEqual(self.p._state["presets"],{})
        self.assertEqual(self.p.mass.player_queues.play_media.await_count,0)
    async def test_named_playlist_never_uses_other_account(self):
        a,b=self.p.providers()
        async def api(route,**kw):
            if route=='playlists':return []
            if route=='search':return {'Playlists':[{'ID':'42','Name':'Pop Rock 2010'}]}
            return [{'ID':'88','Name':'Track'}]
        a._api=AsyncMock(side_effect=api);b._api=AsyncMock(side_effect=AssertionError('Wrong account'))
        result=await self.p.playlist_play('Alice','Pop Rock 2010',preview=True)
        self.assertEqual(result['member'],'Alice')
        self.assertEqual(self.p.mass.player_queues.play_media.await_count,0)
        await self.p.playlist_play('Alice','Pop Rock 2010','Salon')
        self.assertEqual(self.p.mass.player_queues.play_media.call_args.args[1],['opendeezer_home--a://track/88'])
        b._api.assert_not_called()
        a._api=AsyncMock(return_value=[])
        with self.assertRaises(Exception):await self.p.playlist_play('Alice','Absent','Salon')
        self.assertEqual(self.p.mass.player_queues.play_media.await_count,1)
    async def test_flow_does_not_refill_replaced_queue(self):
        q=NS(state='playing',items=2,current_index=0)
        self.p.mass.player_queues.get=lambda _:q
        self.p.mass.player_queues.items=lambda *a,**k:[NS(queue_item_id='new-user-queue')]
        session={'member':'a','tail':'old-flow-tail','last':0,'seen':[]}
        self.p._flows['salon']=session
        await self.p._refill_flow('salon',session)
        self.assertNotIn('salon',self.p._flows)
        self.p.mass.player_queues.play_media.assert_not_called()
    async def test_catalog_shortcut_alias_keeps_account_and_exact_id(self):
        a,b=self.p.providers()
        async def api(route,**kw):
            if route=='playlists':return []
            if route=='playlists/8074581462/tracks':return [{'ID':'88','Name':'Track'}]
            raise AssertionError('Unexpected search or playlist: '+route)
        a._api=AsyncMock(side_effect=api)
        b._api=AsyncMock(return_value=[])
        await self.p.preset_save('Pop Rock 2010','playlist',['Alice'],playlist_id='8074581462',allow_catalog=True,source_title='2010s Pop Rock')
        for name in ['Pop Rock 2010','2010s Pop Rock']:
            result=await self.p.playlist_play('Alice',name,preview=True)
            self.assertEqual(result['playlist_id'],'8074581462')
            self.assertEqual(result['provider_instance'],'opendeezer_home--a')
            self.assertEqual(result['source'],'saved_shortcut')
        async def missing(route,**kw):return [] if route=='playlists' else {'Playlists':[]}
        b._api=AsyncMock(side_effect=missing)
        with self.assertRaises(Exception):await self.p.playlist_play('Bob','Pop Rock 2010',preview=True)
        self.p.mass.player_queues.play_media.assert_not_called()
        await self.p.playlist_play('Alice','Pop Rock 2010','Salon')
        self.assertEqual(self.p.mass.player_queues.play_media.call_args.args[1],['opendeezer_home--a://track/88'])
    async def test_catalog_shortcut_rejects_invalid_or_empty_target(self):
        a=self.p.providers()[0];a._api=AsyncMock(return_value=[])
        for ident in ['../profiles','123']:
            with self.assertRaises(Exception):
                await self.p.preset_save('Invalid','playlist',['Alice'],playlist_id=ident,allow_catalog=True)
        self.assertEqual(self.p._state['presets'],{})
    async def test_flow_refill_uses_same_account_and_preserves_queue(self):
        q=NS(state='playing',items=2,current_index=0)
        self.p.mass.player_queues.get=lambda _:q
        self.p.mass.player_queues.items=lambda *a,**k:[NS(queue_item_id='flow-tail')]
        a,b=self.p.providers();a._api=AsyncMock(return_value=[{'ID':'1'},{'ID':'2'},{'ID':'3'}]);b._api=AsyncMock()
        self.p._state['preferences']['a']={'2':'ban'}
        session={'member':'a','tail':'flow-tail','last':0,'seen':['1']};self.p._flows['salon']=session
        await self.p._refill_flow('salon',session)
        call=self.p.mass.player_queues.play_media.call_args
        self.assertEqual(call.args[1],['opendeezer_home--a://track/3'])
        self.assertEqual(call.kwargs['option'].value,'add');b._api.assert_not_called()
