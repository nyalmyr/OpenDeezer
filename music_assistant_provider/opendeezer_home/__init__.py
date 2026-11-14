"""OpenDeezer Home: one personal account per Music Assistant instance.

Deezer sessions and audio resolution stay in the upstream Go engine. This
playback adapter uses the household gateway token only. Setup forwards the ARL
once to the private gateway for enrollment.
Queues, decoding, volume and synchronized playback remain owned by MA.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from collections import OrderedDict, deque
import copy
import time
from urllib.parse import quote, urlsplit

from aiohttp import ClientError, ClientTimeout
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType, ContentType, ImageType, MediaType, ProviderFeature, StreamType,
)
from music_assistant_models.errors import (
    LoginFailed, MediaNotFoundError, ProviderUnavailableError, UnplayableMediaError,
)
from music_assistant_models.media_items import (
    Artist, AudioFormat, MediaItemImage, Playlist, ProviderMapping,
    SearchResults, Track, UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails
from music_assistant.models.music_provider import MusicProvider

__version__ = "0.3.0"
FEATURES = {
    ProviderFeature.SEARCH, ProviderFeature.BROWSE,
    ProviderFeature.LIBRARY_TRACKS, ProviderFeature.LIBRARY_PLAYLISTS,
}


def config_entries() -> tuple[ConfigEntry, ...]:
    return (
        ConfigEntry(key="bridge_url", type=ConfigEntryType.STRING,
                    label="Adresse du service OpenDeezer",
                    default_value="http://127.0.0.1:8731", required=False),
        ConfigEntry(key="profile_token", type=ConfigEntryType.SECURE_STRING,
                    label="Clé du profil familial (pas le cookie Deezer)",
                    default_value="", required=False),
    )


async def setup(mass, manifest, config):
    return OpenDeezerHomeProvider(mass, manifest, config, FEATURES)


async def get_config_entries(mass, instance_id=None, action=None, values=None):
    return config_entries()


class OpenDeezerHomeProvider(MusicProvider):
    @property
    def instance_name_postfix(self):
        return getattr(self, "profile_label", None)

    @property
    def is_streaming_provider(self) -> bool:
        # Personal libraries must be queried separately, never collapsed into
        # a single Deezer-domain instance by Music Assistant.
        return False

    @property
    def max_concurrent_streams(self) -> int:
        return 2  # permits prebuffering; gateway also enforces this per account

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        return ()  # credentials are edited via the native Reconfigure flow

    async def handle_async_init(self) -> None:
        self._base = str(self.get_setup_value("bridge_url", "http://127.0.0.1:8731") or "").rstrip("/")
        parts = urlsplit(self._base)
        if (parts.scheme not in ("http", "https") or not parts.hostname or
                parts.username or parts.password or parts.query or parts.fragment or parts.path):
            raise LoginFailed("Adresse du service OpenDeezer invalide")
        token = str(self.get_setup_value("profile_token") or "")
        if len(token) < 32:
            raise LoginFailed("Renseigner la clé dédiée à ce profil familial")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._playlists: dict[str, dict] = {}
        self._artists: dict[str, Artist] = {}
        self._metadata_cache = OrderedDict()
        self.request_timings = deque(maxlen=30)
        account = await self._api("account")
        self.profile_id = account["id"]
        self.profile_label = account["label"]

    async def _api(self, path: str, method: str = "GET", params=None):
        cacheable = method == "GET" and (path in ("favorites", "playlists") or path.startswith(("tracks/", "playlists/")))
        cached = self._metadata_cache.get(path) if cacheable else None
        if cached and time.monotonic() < cached[0]:
            self._metadata_cache.move_to_end(path)
            return copy.deepcopy(cached[1])
        started = time.monotonic()
        try:
            result = await self._request(path, method, params)
            self.request_timings.append({"operation": path.split('/')[0], "ok": True, "ms": round((time.monotonic()-started)*1000)})
        except Exception:
            self.request_timings.append({"operation": path.split('/')[0], "ok": False, "ms": round((time.monotonic()-started)*1000)})
            raise
        if cacheable:
            self._metadata_cache[path] = (time.monotonic()+120, copy.deepcopy(result))
            while len(self._metadata_cache)>256: self._metadata_cache.popitem(last=False)
        return result

    async def _request(self, path: str, method: str = "GET", params=None):
        try:
            async with self.mass.http_session.request(
                method, f"{self._base}/v1/{path}", headers=self._headers,
                params=params, timeout=ClientTimeout(total=15), allow_redirects=False,
            ) as resp:
                if resp.status in (401, 403):
                    raise LoginFailed("Clé du profil OpenDeezer refusée")
                if resp.status == 422:
                    raise UnplayableMediaError("Morceau complet indisponible pour ce compte Deezer")
                if resp.status != 200:
                    raise ProviderUnavailableError(f"OpenDeezer indisponible (HTTP {resp.status})")
                return await resp.json()
        except (ClientError, TimeoutError, ValueError):
            raise ProviderUnavailableError("Service OpenDeezer injoignable ou réponse invalide") from None

    def _mapping(self, item_id: str) -> set[ProviderMapping]:
        return {ProviderMapping(
            item_id=item_id, provider_domain=self.domain,
            provider_instance=self.instance_id, is_unique=True,
            audio_format=AudioFormat(content_type=ContentType.MP3),
        )}

    def _track(self, data: dict, position: int | None = None) -> Track:
        item = Track(item_id=str(data["ID"]), provider=self.instance_id,
                     name=data["Name"], duration=int(data.get("DurationMS", 0) // 1000),
                     provider_mappings=self._mapping(str(data["ID"])), position=position)
        item.artists = UniqueList([
            Artist(item_id=str(a.get("ID") or a["Name"]),
                   provider=self.instance_id, name=a["Name"],
                   provider_mappings=self._mapping(str(a.get("ID") or a["Name"])))
            for a in (data.get("Artists") or [])
        ])
        for artist in item.artists:
            self._artists[artist.item_id] = artist
        if data.get("ArtworkURL"):
            item.metadata.images = UniqueList([MediaItemImage(
                type=ImageType.THUMB, path=data["ArtworkURL"], provider=self.instance_id,
                remotely_accessible=True,
            )])
        item.metadata.explicit = bool(data.get("Explicit", False))
        return item

    def _playlist(self, data: dict) -> Playlist:
        self._playlists[str(data["ID"])] = data
        item = Playlist(item_id=str(data["ID"]), provider=self.instance_id,
                        name=f"{self.profile_label} · {data['Name']}",
                        owner=self.profile_label, is_editable=False,
                        provider_mappings=self._mapping(str(data["ID"])))
        if data.get("ArtworkURL"):
            item.metadata.images = UniqueList([MediaItemImage(
                type=ImageType.THUMB, path=data["ArtworkURL"], provider=self.instance_id,
                remotely_accessible=True,
            )])
        return item

    async def get_library_tracks(self) -> AsyncGenerator[Track]:
        for item in await self._api("favorites") or []:
            yield self._track(item)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist]:
        yield self._playlist({"ID": "favorites", "Name": "Coups de cœur"})
        for item in await self._api("playlists") or []:
            yield self._playlist(item)

    async def get_track(self, prov_track_id: str) -> Track:
        return self._track(await self._api(f"tracks/{quote(prov_track_id, safe='')}"))

    async def get_artist(self, prov_artist_id: str) -> Artist:
        if prov_artist_id in self._artists:
            return self._artists[prov_artist_id]
        raise MediaNotFoundError("Artiste absent des métadonnées déjà chargées")

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        if prov_playlist_id == "favorites":
            return self._playlist({"ID": "favorites", "Name": "Coups de cœur"})
        if prov_playlist_id not in self._playlists:
            for item in await self._api("playlists") or []:
                self._playlists[str(item["ID"])] = item
        if prov_playlist_id in self._playlists:
            return self._playlist(self._playlists[prov_playlist_id])
        # Public playlist IDs may come directly from a voice request or URL.
        if not prov_playlist_id.isdecimal():
            raise MediaNotFoundError("Identifiant de playlist Deezer invalide")
        return self._playlist({"ID": prov_playlist_id, "Name": f"Playlist Deezer {prov_playlist_id}"})

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        if page > 0:
            return []  # upstream engine already paginates the complete list
        path = "favorites" if prov_playlist_id == "favorites" else f"playlists/{quote(prov_playlist_id, safe='')}/tracks"
        return [self._track(item, pos) for pos, item in enumerate(await self._api(path) or [], 1)]

    async def search(self, search_query: str, media_types: list[MediaType], limit: int = 5) -> SearchResults:
        data = await self._api("search", params={"q": search_query})
        return SearchResults(
            tracks=[self._track(i) for i in (data.get("Tracks") or [])[:limit]] if MediaType.TRACK in media_types else [],
            playlists=[self._playlist(i) for i in (data.get("Playlists") or [])[:limit]] if MediaType.PLAYLIST in media_types else [],
        )

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        if media_type != MediaType.TRACK:
            raise UnplayableMediaError("Seuls les morceaux Deezer sont pris en charge")
        track = await self.get_track(item_id)
        return StreamDetails(
            item_id=item_id, provider=self.instance_id,
            audio_format=AudioFormat(content_type=ContentType.MP3),
            stream_type=StreamType.CUSTOM, duration=track.duration,
            can_seek=False, allow_seek=False,
        )

    async def get_audio_stream(self, streamdetails: StreamDetails, seek_position: int = 0) -> AsyncGenerator[bytes]:
        if seek_position:
            raise UnplayableMediaError("Avance dans le morceau non prise en charge par cette version")
        # Resolve just in time. No signed CDN URL or session credential is put
        # into StreamDetails, logs, entity attributes or browser responses.
        plan = await self._api(f"resolve/{quote(streamdetails.item_id, safe='')}", method="POST")
        if plan.get("preview"):
            raise UnplayableMediaError("Extrait de 30 secondes refusé")
        try:
            async with self.mass.http_session.get(
                f"{self._base}/v1/audio/{plan['lease']}", headers=self._headers,
                timeout=ClientTimeout(total=None, sock_connect=5, sock_read=25),
                allow_redirects=False,
            ) as resp:
                if resp.status != 200:
                    raise UnplayableMediaError(f"Lecture Deezer indisponible (HTTP {resp.status})")
                async for chunk in resp.content.iter_chunked(32768):
                    yield chunk
        except (ClientError, TimeoutError):
            raise UnplayableMediaError("Flux Deezer interrompu") from None
