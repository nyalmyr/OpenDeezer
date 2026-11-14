"""Run against the installed MA models; all HTTP traffic here is simulated."""
import asyncio
import json
import unittest
from types import SimpleNamespace

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Artist
from music_assistant_models.errors import LoginFailed, ProviderUnavailableError, UnplayableMediaError
from music_assistant_models.provider import ProviderManifest
from opendeezer_home import setup

TRACK={"ID":"123","Name":"Test", "DurationMS":181000, "Artists":[{"ID":"1","Name":"Artiste"}],"ArtworkURL":"https://example.org/image.jpg"}

class Response:
    def __init__(self, data=None, status=200):
        self.data,self.status,self.content=data,status,self
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    async def json(self):return self.data
    async def iter_chunked(self,n):
        yield b"mp3data"

class Session:
    def __init__(self,name="Alice"):
        self.calls=[];self.name=name;self.preview=False;self.status=200
    def request(self,method,url,**kwargs):
        self.calls.append((method,url,kwargs))
        path=url.split('/v1/')[-1]
        if path=="account":return Response({"id":self.name.lower(),"label":self.name})
        if path=="playlists":return Response([{"ID":"42","Name":"Chill"}])
        if path in ("favorites","playlists/42/tracks"):return Response([TRACK])
        if path=="tracks/123":return Response(TRACK)
        if path=="search":return Response({"Tracks":[TRACK],"Playlists":[{"ID":"42","Name":"Chill"}]})
        if path=="resolve/123":return Response({"lease":"opaque","preview":self.preview},self.status)
        if path=="audio/opaque":return Response(status=self.status)
        raise AssertionError(f"Unexpected path: {path}")
    def get(self,url,**kwargs):return self.request("GET",url,**kwargs)

async def make(name="Alice"):
    manifest=ProviderManifest.from_dict({"type":"music","domain":"opendeezer_home","name":"OpenDeezer Home","description":"Test","codeowners":[],"multi_instance":True})
    session=Session(name)
    values={"bridge_url":"http://127.0.0.1:8731","profile_token":name*32}
    config=SimpleNamespace(instance_id="opendeezer_home--"+name,domain="opendeezer_home",name=name,get_value=lambda k,*args:values.get(k))
    p=await setup(SimpleNamespace(cache=None,http_session=session,config=SimpleNamespace(get=lambda k:values, decrypt_string=lambda v:v)),manifest,config)
    await p.handle_async_init()
    return p,session,values

class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_models_and_library(self):
        p,s,_=await make(); tracks=[x async for x in p.get_library_tracks()]
        self.assertEqual(tracks[0].duration,181)
        self.assertEqual(tracks[0].artists[0].name,"Artiste")
        self.assertIsInstance(tracks[0].artists[0],Artist)
        self.assertEqual((await p.get_artist("1")).name,"Artiste")
        self.assertEqual(tracks[0].provider,p.instance_id)
        lists=[x async for x in p.get_library_playlists()]
        self.assertEqual([x.name for x in lists],["Alice · Coups de cœur","Alice · Chill"])
        self.assertFalse(p.is_streaming_provider)
    async def test_profiles_never_share_headers_or_mapping(self):
        a,sa,_=await make("Alice");b,sb,_=await make("Bob")
        self.assertNotEqual(a._headers,b._headers)
        self.assertNotEqual(a._mapping("123"),b._mapping("123"))
        self.assertEqual((await a.get_playlist("42")).owner,"Alice")
        self.assertEqual((await b.get_playlist("42")).owner,"Bob")
    async def test_playlist_positions_and_no_duplicate_pages(self):
        p,s,_=await make();items=await p.get_playlist_tracks("42")
        self.assertEqual(items[0].position,1)
        before=len(s.calls);self.assertEqual(await p.get_playlist_tracks("42",1),[])
        self.assertEqual(len(s.calls),before)
    async def test_search_respects_type_and_limit(self):
        p,s,_=await make();r=await p.search("Test",[MediaType.TRACK],1)
        self.assertEqual(len(r.tracks),1);self.assertEqual(len(r.playlists),0)
    async def test_full_stream_uses_headers_and_no_secret_url(self):
        p,s,v=await make();detail=await p.get_stream_details("123",MediaType.TRACK)
        self.assertFalse(detail.can_seek);self.assertNotIn(v["profile_token"],str(detail))
        chunks=[x async for x in p.get_audio_stream(detail)]
        self.assertEqual(chunks,[b"mp3data"])
        for _,url,kwargs in s.calls:
            self.assertNotIn(v["profile_token"],url)
            self.assertEqual(kwargs["headers"],p._headers)
            self.assertFalse(kwargs["allow_redirects"])
    async def test_preview_is_refused(self):
        p,s,_=await make();s.preview=True;detail=await p.get_stream_details("123",MediaType.TRACK)
        with self.assertRaises(UnplayableMediaError):
            _=[x async for x in p.get_audio_stream(detail)]
        self.assertFalse(any('/audio/' in url for _,url,_ in s.calls))
    async def test_gateway_failure_is_not_a_success(self):
        p,s,_=await make();s.status=502;detail=await p.get_stream_details("123",MediaType.TRACK)
        with self.assertRaises(ProviderUnavailableError):
            _=[x async for x in p.get_audio_stream(detail)]
    async def test_config_rejects_url_credentials(self):
        p,s,v=await make();v["bridge_url"]="http://secret@127.0.0.1:8731"
        with self.assertRaises(LoginFailed):await p.handle_async_init()

if __name__=="__main__":unittest.main()
