"""Household features on MA's authenticated API and native audio/party controllers."""
from __future__ import annotations

import asyncio
import copy
import json
import os
import secrets
import time
from collections import deque
from pathlib import Path

from aiohttp import web, ClientTimeout
from music_assistant.models.plugin import PluginProvider
from music_assistant_models.auth import Scope
from music_assistant_models.enums import QueueOption
from music_assistant_models.errors import InvalidDataError
from .mixer import build_mix, normalize

VERSION = "0.4.0"


async def setup(mass, manifest, config):
    return FamilyProvider(mass, manifest, config, set())


async def get_config_entries(mass, instance_id=None, action=None, values=None):
    return ()


class FamilyProvider(PluginProvider):
    async def loaded_in_mass(self):
        self._path = Path(self.mass.storage_path) / "opendeezer-family.json"
        self._lock = asyncio.Lock()
        self._catalogs = {}
        self._catalog_lock = asyncio.Lock()
        self._metrics = deque(maxlen=50)
        self._pairs = {}
        self._flows = {}
        self._pair_attempts = deque(maxlen=40)
        self._state = {"preferences": {}, "mixes": {}, "last_mix": {}}
        if self._path.exists():
            self._state = json.loads(await asyncio.to_thread(self._path.read_text))
        self._state.setdefault("presets", {})
        self._handles = []
        for name, method, scope in [
            ("status", self.status, Scope.PROVIDERS_READ),
            ("compose", self.compose, Scope.LIBRARY_WRITE),
            ("selection", self.selection, Scope.LIBRARY_READ),
            ("play", self.play, Scope.QUEUES_CONTROL),
            ("start", self.start_mix, Scope.QUEUES_CONTROL),
            ("flow", self.flow, Scope.QUEUES_CONTROL),
            ("playlists", self.playlists, Scope.LIBRARY_READ),
            ("playlist_play", self.playlist_play, Scope.QUEUES_CONTROL),
            ("preset_save", self.preset_save, Scope.LIBRARY_WRITE),
            ("preset_delete", self.preset_delete, Scope.LIBRARY_WRITE),
            ("preset_play", self.preset_play, Scope.QUEUES_CONTROL),
            ("control", self.control, Scope.QUEUES_CONTROL),
            ("resume", self.resume_member, Scope.QUEUES_CONTROL),
            ("feedback", self.feedback, Scope.LIBRARY_WRITE),
            ("explain", self.explain, Scope.LIBRARY_READ),
            ("transfer", self.transfer, Scope.QUEUES_CONTROL),
            ("join", self.join, Scope.PLAYERS_CONTROL),
            ("party", self.party, Scope.CONFIG_PROVIDERS_WRITE),
            ("diagnostics", self.diagnostics, Scope.CONFIG_PROVIDERS_READ),
            ("pair_begin", self.pair_begin, Scope.CONFIG_PROVIDERS_WRITE),
            ("pair_finish", self.pair_finish, Scope.CONFIG_PROVIDERS_WRITE),
            ("pair_cancel", self.pair_cancel, Scope.CONFIG_PROVIDERS_WRITE),
            ("reconnect", self.reconnect, Scope.CONFIG_PROVIDERS_WRITE),
            ("remove", self.remove_member, Scope.CONFIG_PROVIDERS_WRITE),
        ]:
            self._handles.append(self.mass.register_api_command("opendeezer/family/" + name, method, required_scope=scope))
        self._handles.append(self.mass.webserver.register_dynamic_route("/opendeezer-family/*", self.web_handler))
        self._warm_task = asyncio.create_task(self._warm())
        self._pair_cleanup_task = asyncio.create_task(self._cleanup_pairs())
        self._flow_task = asyncio.create_task(self._watch_flows())

    async def unload(self, is_removed=False):
        if hasattr(self, "_flow_task"):
            self._flow_task.cancel()
            try: await self._flow_task
            except asyncio.CancelledError: pass
        if hasattr(self, "_pair_cleanup_task"):
            self._pair_cleanup_task.cancel()
            try: await self._pair_cleanup_task
            except asyncio.CancelledError: pass
        if hasattr(self, "_warm_task"):
            self._warm_task.cancel()
            try: await self._warm_task
            except asyncio.CancelledError: pass
        for handle in getattr(self, "_handles", []): handle()
        self._pairs = {}

    async def _cleanup_pairs(self):
        while True:
            await asyncio.sleep(15)
            self._pairs = {k:v for k,v in self._pairs.items() if v["expires"] > time.monotonic()}

    async def _watch_flows(self):
        while True:
            await asyncio.sleep(8)
            for queue_id, session in list(self._flows.items()):
                try: await self._refill_flow(queue_id, session)
                except Exception:
                    self._flows.pop(queue_id, None)
                    self._metrics.append({"operation": "flow_refill", "ok": False})

    def _flow_queue_matches(self, queue_id, session):
        q = self.mass.player_queues.get(queue_id)
        if not q or str(q.state) not in ("playing", "paused"): return False
        items = self.mass.player_queues.items(queue_id, limit=1, offset=max(q.items-1,0))
        return bool(items and items[-1].queue_item_id == session["tail"])

    async def _refill_flow(self, queue_id, session):
        # Queue-item IDs distinguish this exact Flow from a user replacement or append.
        if self._flows.get(queue_id) is not session: return
        if not self._flow_queue_matches(queue_id, session):
            self._flows.pop(queue_id, None); return
        q = self.mass.player_queues.get(queue_id)
        if str(q.state) != "playing" or q.items - (q.current_index or 0) > 3: return
        if time.monotonic() - session["last"] < 30: return
        p = self.member(session["member"])
        raw = await p._api("flow") or []
        forbidden = set(session["seen"]) | {tid for tid,rating in self._state["preferences"].get(p.profile_id,{}).items() if rating == "ban"}
        tracks = list({str(t["ID"]):t for t in raw if str(t["ID"]) not in forbidden}.values())[:100]
        if not tracks: raise InvalidDataError("Flow sans nouveaux titres")
        if self._flows.get(queue_id) is not session or not self._flow_queue_matches(queue_id, session): return
        if str(self.mass.player_queues.get(queue_id).state) != "playing": return
        await self.mass.player_queues.play_media(queue_id,[f"{p.instance_id}://track/{t['ID']}" for t in tracks],option=QueueOption.ADD)
        q = self.mass.player_queues.get(queue_id)
        tail = self.mass.player_queues.items(queue_id,limit=1,offset=q.items-1)[0]
        session.update(tail=tail.queue_item_id,last=time.monotonic(),seen=(session["seen"]+[str(t["ID"]) for t in tracks])[-300:])
        self._metrics.append({"operation":"flow_refill","ok":True,"tracks":len(tracks)})

    def providers(self):
        return [p for p in self.mass.providers if p.domain == "opendeezer_home" and p.available]

    def member(self, value):
        matches = [p for p in self.providers() if normalize(value) in
                   {normalize(p.profile_id), normalize(p.profile_label), normalize(p.instance_id)}]
        if len(matches) != 1:
            raise InvalidDataError("Membre absent, ambigu ou déconnecté : " + str(value)[:60])
        return matches[0]

    def player(self, value):
        players = self.mass.players.all_players(False, False)
        matches = [p for p in players if p.available and normalize(value) in {normalize(p.player_id), normalize(p.display_name), normalize(p.display_name).removeprefix("echo ")}]
        if len(matches) != 1: raise InvalidDataError("Choisissez un lecteur disponible et précis.")
        if matches[0].player_id.startswith("syncgroup_") and not matches[0].group_members:
            raise InvalidDataError("Ce groupe ne contient encore aucune enceinte.")
        return matches[0].player_id

    async def _save(self):
        data = json.dumps(self._state, ensure_ascii=False)
        path = self._path
        def write():
            temporary = path.with_suffix(".tmp")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(data); f.flush(); os.fsync(f.fileno())
            os.replace(temporary, path)
        await asyncio.to_thread(write)

    async def _warm(self):
        await asyncio.sleep(5)
        while True:
            for provider in self.providers():
                try: await self.catalog(provider)
                except Exception: self._metrics.append({"operation": "catalog", "ok": False})
            await asyncio.sleep(600)

    async def catalog(self, provider):
        key = provider.profile_id
        cached = self._catalogs.get(key)
        if cached and time.monotonic() - cached[0] < 900: return cached[1]
        async with self._catalog_lock:
            cached = self._catalogs.get(key)
            if cached and time.monotonic() - cached[0] < 900: return cached[1]
            started = time.monotonic()
            lists = (await provider._api("playlists") or [])[:30]
            requests = [("favorites", "Coups de cœur")] + [(f"playlists/{p['ID']}/tracks", p["Name"]) for p in lists]
            tracks = {}
            for route, title in requests:
                for t in (await provider._api(route) or [])[:500]:
                    key_id = str(t["ID"])
                    if key_id not in tracks:
                        tracks[key_id] = {"id": key_id, "name": t["Name"], "duration": int(t.get("DurationMS", 0) // 1000),
                            "artists": [a["Name"] for a in t.get("Artists", [])], "playlists": [],
                            "uri": f"{provider.instance_id}://track/{key_id}"}
                    tracks[key_id]["playlists"].append(title)
                    if len(tracks) >= 5000: break
                if len(tracks) >= 5000: break
            result = list(tracks.values())
            self._catalogs[key] = (time.monotonic(), result)
            self._metrics.append({"operation": "catalog", "ok": True, "ms": round((time.monotonic()-started)*1000), "tracks": len(result)})
            return result

    async def status(self):
        configs = await self.mass.config.get_provider_configs(provider_domain="opendeezer_home")
        loaded = {p.instance_id: p for p in self.providers()}
        accounts = []
        for c in configs:
            raw = self.mass.config.get("providers/" + c.instance_id)
            values = self.mass.config._decrypt_values(raw.get("setup_data") or {})
            p = loaded.get(c.instance_id)
            accounts.append({"instance_id": c.instance_id, "label": p.profile_label if p else values.get("profile_label") or c.name or "Compte Deezer", "available": p is not None})
        return {"version": VERSION, "members": [{"id": p.profile_id, "label": p.profile_label,
            "instance_id": p.instance_id, "cached_tracks": len(self._catalogs.get(p.profile_id, (0, []))[1]),
            "preferences": len(self._state["preferences"].get(p.profile_id, {}))} for p in self.providers()],
            "accounts": accounts,
            "presets": [{**{k:v for k,v in p.items() if k != "tracks"}, "track_count": len(p["tracks"])} for p in self._state["presets"].values()],
            "players": [{"id": p.player_id, "label": p.display_name} for p in self.mass.players.all_players(False, False) if p.available],
            "mixes": [{k: v for k, v in mix.items() if k != "tracks"} for mix in self._state["mixes"].values()]}

    async def playlists(self, member: str):
        p = self.member(member)
        return {"member": p.profile_id, "playlists": [{"id": str(x["ID"]), "name": x["Name"]}
            for x in (await p._api("playlists") or [])]}

    async def flow(self, member: str, player: str = "", preview: bool = False):
        p = self.member(member)
        queue_id = self.player(player) if not preview else None
        raw = await p._api("flow") or []
        banned = {tid for tid, rating in self._state["preferences"].get(p.profile_id, {}).items() if rating == "ban"}
        tracks = list({str(t["ID"]): t for t in raw if str(t["ID"]) not in banned}.values())[:100]
        if not tracks: raise InvalidDataError("Deezer ne renvoie aucun titre Flow pour ce compte. Aucun autre mélange n'a été lancé.")
        if preview:
            return {"source": "deezer_flow", "member": p.profile_label, "tracks": len(tracks),
                    "titles": [t["Name"] for t in tracks[:5]], "continuous": True}
        await self.mass.player_queues.play_media(queue_id,
            [f"{p.instance_id}://track/{t['ID']}" for t in tracks], option=QueueOption.REPLACE)
        q = self.mass.player_queues.get(queue_id)
        if q:
            tail = self.mass.player_queues.items(queue_id,limit=1,offset=q.items-1)
            if tail: self._flows[queue_id]={"member":p.profile_id,"tail":tail[0].queue_item_id,"last":time.monotonic(),"seen":[str(t["ID"]) for t in tracks]}
        return {"requested": True, "source": "deezer_flow", "tracks": len(tracks), "speech": "", "continuous": queue_id in self._flows}

    async def playlist_play(self, member: str, name: str, player: str = "", preview: bool = False):
        """Resolve within the explicitly named account; never use another instance's results."""
        p = self.member(member)
        queue_id = self.player(player) if not preview else None
        import re
        key = lambda text: re.sub(r"[^\w]+", " ", normalize(text)).strip()
        wanted = key(name)
        if not wanted or len(name) > 160: raise InvalidDataError("Indiquez le nom de la playlist.")
        saved = [x for x in self._state["presets"].values() if x["kind"] == "playlist" and x["members"] == [p.profile_id]
                 and wanted in {key(x["name"]), key(x.get("source_title", ""))}]
        if len(saved) > 1: raise InvalidDataError("Plusieurs raccourcis correspondent à cette demande.")
        matches = [{"ID":saved[0]["playlist_id"],"Name":saved[0].get("source_title") or saved[0]["name"]}] if saved else []
        source = "saved_shortcut" if saved else "personal_library"
        if not matches:
            candidates = await p._api("playlists") or []
            matches = [x for x in candidates if key(x["Name"]) == wanted]
        if not matches:
            search = await p._api("search", params={"q": name})
            candidates = search.get("Playlists") or []
            matches = [x for x in candidates if key(x["Name"]) == wanted]
            source = "deezer_catalog"
        if len(matches) != 1:
            raise InvalidDataError("Playlist introuvable ou ambiguë pour " + p.profile_label + ". Précisez son titre exact ou utilisez un raccourci. Aucun autre compte n'a été utilisé.")
        chosen = matches[0]
        result = {"member": p.profile_label, "provider_instance": p.instance_id, "playlist": chosen["Name"], "playlist_id": str(chosen["ID"]), "source": source}
        if preview: return result
        raw = await p._api(f"playlists/{chosen['ID']}/tracks") or []
        banned = {tid for tid,rating in self._state["preferences"].get(p.profile_id,{}).items() if rating == "ban"}
        uris = [f"{p.instance_id}://track/{t['ID']}" for t in raw[:1000] if str(t["ID"]) not in banned]
        if not uris: raise InvalidDataError("Cette playlist est vide ou indisponible.")
        await self.mass.player_queues.play_media(queue_id, uris, option=QueueOption.REPLACE)
        return {**result, "requested": True, "tracks": len(uris), "speech": ""}

    async def preset_save(self, name: str, kind: str, members: list[str], player: str = "",
                          minutes: int = 60, mood: str = "", end_mood: str = "",
                          weights: dict[str, float] | None = None, playlist_id: str = "", preset_id: str = "",
                          allow_catalog: bool = False, source_title: str = ""):
        name = name.strip()
        if not 1 <= len(name) <= 60: raise InvalidDataError("Donnez un nom de 1 à 60 caractères au raccourci.")
        if kind not in ("mix", "playlist", "flow"): raise InvalidDataError("Source musicale inconnue.")
        if not isinstance(members, list) or not 1 <= len(members) <= 20: raise InvalidDataError("Choisissez au moins une personne.")
        people = [self.member(m) for m in members]
        ids = [p.profile_id for p in people]
        if len(set(ids)) != len(ids): raise InvalidDataError("Un membre apparaît deux fois.")
        if kind != "mix" and len(people) != 1: raise InvalidDataError("Choisissez un seul compte pour une playlist ou un Flow.")
        destination = self.player(player) if player else ""
        if preset_id and preset_id not in self._state["presets"]: raise InvalidDataError("Raccourci introuvable.")
        mix = None
        if kind == "playlist":
            lists = (await self.playlists(members[0]))["playlists"]
            if playlist_id not in {x["id"] for x in lists}:
                if not allow_catalog or not playlist_id.isdecimal() or len(playlist_id)>20:
                    raise InvalidDataError("Choisissez une playlist de ce compte ou un lien Deezer explicite.")
                if not await people[0]._api(f"playlists/{playlist_id}/tracks"):
                    raise InvalidDataError("La playlist indiquée est vide ou inaccessible pour ce compte.")
        if kind == "mix":
            mix = await self.compose(members, weights, minutes, mood, end_mood, name)
        async with self._lock:
            if any(normalize(v["name"]) == normalize(name) and k != preset_id for k,v in self._state["presets"].items()):
                raise InvalidDataError("Ce nom est déjà utilisé. Modifiez le raccourci existant.")
            if not preset_id and len(self._state["presets"]) >= 60: raise InvalidDataError("Maximum 60 raccourcis.")
            pid = preset_id or secrets.token_hex(8)
            item = {"id": pid, "name": name, "kind": kind, "members": ids, "player": destination,
                "minutes": minutes, "mood": mood[:80], "end_mood": end_mood[:80], "weights": weights or {},
                "playlist_id": playlist_id, "tracks": copy.deepcopy(mix["tracks"]) if mix else [],
                "allow_catalog": allow_catalog, "source_title": source_title.strip()[:160],
                "warnings": mix["warnings"] if mix else [], "created": int(time.time())}
            self._state["presets"][pid] = item
            await self._save()
        return {"saved": True, "id": pid, "tracks": len(item["tracks"]), "warnings": item["warnings"]}

    async def preset_delete(self, preset_id: str):
        async with self._lock:
            if preset_id not in self._state["presets"]: raise InvalidDataError("Raccourci introuvable.")
            del self._state["presets"][preset_id]
            await self._save()
        return {"deleted": True}

    async def preset_play(self, preset: str, player: str = ""):
        matches = [p for p in self._state["presets"].values() if normalize(preset) in {normalize(p["id"]), normalize(p["name"])}]
        if len(matches) != 1: raise InvalidDataError("Raccourci absent ou ambigu.")
        p = matches[0]
        people = [self.member(m) for m in p["members"]]
        queue_id = self.player(player or p["player"])
        if p["kind"] == "flow": return await self.flow(people[0].profile_id, queue_id)
        if p["kind"] == "playlist":
            raw = await people[0]._api(f"playlists/{p['playlist_id']}/tracks") or []
            tracks = [{"id": str(t["ID"]), "uri": f"{people[0].instance_id}://track/{t['ID']}"} for t in raw[:1000]]
        else: tracks = p["tracks"]
        banned = {tid for person in people for tid,rating in self._state["preferences"].get(person.profile_id,{}).items() if rating == "ban"}
        uris = [t["uri"] for t in tracks if t["id"] not in banned]
        if not uris: raise InvalidDataError("Cette sélection ne contient aucun titre disponible.")
        await self.mass.player_queues.play_media(queue_id, uris, option=QueueOption.REPLACE)
        return {"requested": True, "tracks": len(uris), "speech": ""}

    async def control(self, player: str, action: str, volume: int = 20):
        pid = self.player(player)
        if action == "stop": self._flows.pop(pid, None)
        if action in ("play", "pause", "stop", "next", "previous"):
            await getattr(self.mass.player_queues, action)(pid)
        elif action == "volume":
            if not 0 <= volume <= 100: raise InvalidDataError("Volume : 0 à 100 %.")
            await self.mass.players.cmd_volume_set(pid, volume)
        else: raise InvalidDataError("Commande inconnue.")
        return {"requested": True, "speech": ""}

    async def compose(self, members: list[str], weights: dict[str, float] | None = None,
                      minutes: int = 60, mood: str = "", end_mood: str = "", name: str = "Mix familial"):
        if not isinstance(members, list) or not 1 <= len(members) <= 20: raise InvalidDataError("Choisissez les participants.")
        providers = [self.member(m) for m in members]
        if len({p.profile_id for p in providers}) != len(providers): raise InvalidDataError("Un membre apparaît deux fois.")
        catalogs = {}; mapped_weights = {}; preferences = {}
        for original, p in zip(members, providers):
            catalogs[p.profile_label] = await self.catalog(p)
            mapped_weights[p.profile_label] = (weights or {}).get(original, 1)
            preferences[p.profile_label] = self._state["preferences"].get(p.profile_id, {})
        started = time.monotonic()
        try: mix = build_mix(catalogs, mapped_weights, preferences, minutes, mood[:80], end_mood[:80], secrets.randbits(32))
        except ValueError as e: raise InvalidDataError(str(e)) from None
        mix.update({"id": secrets.token_hex(8), "name": name.strip()[:80] or "Mix familial",
                    "members": [p.profile_id for p in providers], "created": int(time.time()), "mood": mood[:80], "end_mood": end_mood[:80]})
        async with self._lock:
            self._state["mixes"][mix["id"]] = mix
            while len(self._state["mixes"]) > 20: del self._state["mixes"][next(iter(self._state["mixes"]))]
            await self._save()
        self._metrics.append({"operation": "mix", "ok": True, "ms": round((time.monotonic()-started)*1000)})
        return mix

    def _mix(self, mix_id):
        mix = self._state["mixes"].get(mix_id)
        if not mix: raise InvalidDataError("Sélection absente ; recréez-la.")
        return mix

    async def selection(self, mix_id: str):
        return copy.deepcopy(self._mix(mix_id))

    async def play(self, mix_id: str, player: str):
        mix = self._mix(mix_id); queue_id = self.player(player)
        for member in mix["members"]: self.member(member)
        bans = {tid for member in mix["members"] for tid, rating in self._state["preferences"].get(member, {}).items() if rating == "ban"}
        tracks = [t for t in mix["tracks"] if t["id"] not in bans]
        if not tracks: raise InvalidDataError("Tous les morceaux sont exclus par vos préférences.")
        started = time.monotonic()
        await self.mass.player_queues.play_media(queue_id, [t["uri"] for t in tracks], option=QueueOption.REPLACE)
        async with self._lock:
            for member in mix["members"]: self._state["last_mix"][member] = {"mix": mix_id, "queue": queue_id}
            await self._save()
        self._metrics.append({"operation": "play_request", "ok": True, "ms": round((time.monotonic()-started)*1000)})
        return {"requested": True, "queue_id": queue_id, "tracks": len(tracks), "speech": ""}

    async def start_mix(self, members: list[str], player: str, minutes: int = 60, mood: str = "", end_mood: str = "", weights: dict[str, float] | None = None):
        self.player(player)  # reject invalid destinations before composing
        mix = await self.compose(members, weights, minutes, mood, end_mood, " + ".join(members)[:60] + (" · " + mood if mood else ""))
        result = await self.play(mix["id"], player)
        return {**result, "mix_id": mix["id"], "contributions": mix["contributions"], "warnings": mix["warnings"]}

    async def resume_member(self, member: str, player: str):
        p = self.member(member); previous = self._state["last_mix"].get(p.profile_id)
        mix_id = previous.get("mix") if isinstance(previous, dict) else previous
        if isinstance(previous, dict):
            source = self.mass.player_queues.get(previous.get("queue"))
            expected = {t["id"] for t in self._state["mixes"].get(mix_id, {}).get("tracks", [])}
            if source and source.current_item and source.current_item.media_item:
                item = source.current_item.media_item
                identifiers = {str(item.item_id)} | {str(m.item_id) for m in getattr(item, "provider_mappings", []) if m.provider_domain == "opendeezer_home"}
                banned = {tid for tid, rating in self._state["preferences"].get(p.profile_id, {}).items() if rating == "ban"}
                if identifiers & expected and not identifiers & banned:
                    destination = self.player(player)
                    if destination == previous["queue"]:
                        await self.mass.player_queues.resume(destination)
                    else:
                        await self.mass.player_queues.transfer_queue(previous["queue"], destination, auto_play=True)
                        async with self._lock:
                            self._state["last_mix"][p.profile_id]["queue"] = destination
                            await self._save()
                    return {"requested": True, "resumed_queue": True, "speech": ""}
        if not mix_id or mix_id not in self._state["mixes"]:
            mix_id = (await self.compose([member], name="Sélection de " + p.profile_label))["id"]
        return await self.play(mix_id, player)

    def current_track(self, player):
        q = self.mass.player_queues.get(self.player(player))
        item = q.current_item if q else None
        media = item.media_item if item else None
        if not media: raise InvalidDataError("Aucun morceau en cours.")
        if getattr(media, "provider", "") == "library":
            mappings = [m for m in getattr(media, "provider_mappings", []) if m.provider_domain == "opendeezer_home"]
            if not mappings: raise InvalidDataError("Le morceau ne vient pas de Deezer.")
            return mappings[0].item_id
        if not str(getattr(media, "provider", "")).startswith("opendeezer_home"):
            raise InvalidDataError("Le morceau ne vient pas de Deezer.")
        return media.item_id

    async def feedback(self, member: str, rating: str, player: str = "", track_id: str = ""):
        p = self.member(member)
        if rating not in ("like", "dislike", "ban", "neutral"): raise InvalidDataError("Avis non reconnu.")
        tid = track_id or self.current_track(player)
        if not str(tid).isdecimal(): raise InvalidDataError("Identifiant de morceau invalide.")
        async with self._lock:
            prefs = self._state["preferences"].setdefault(p.profile_id, {})
            if rating == "neutral": prefs.pop(str(tid), None)
            else: prefs[str(tid)] = rating
            await self._save()
        return {"saved": True, "rating": rating, "speech": ""}

    async def explain(self, mix_id: str = "", track_id: str = "", player: str = ""):
        tid = track_id or self.current_track(player)
        mixes = [self._mix(mix_id)] if mix_id else reversed(list(self._state["mixes"].values()))
        for mix in mixes:
            for track in mix["tracks"]:
                if track["id"] == tid: return {"track": track["name"], "reason": track["reason"], "mix": mix["name"]}
        return {"reason": "Ce morceau n'a pas été choisi par le mélange familial."}

    async def transfer(self, source: str, destination: str):
        a, b = self.player(source), self.player(destination)
        if a == b: return {"requested": False, "speech": ""}
        await self.mass.player_queues.transfer_queue(a, b, auto_play=True)
        return {"requested": True, "speech": "", "note": "Le morceau peut reprendre au début si le lecteur ou le flux ne permet pas le déplacement."}

    async def join(self, player: str, leader: str):
        await self.mass.players.cmd_group(self.player(player), self.player(leader))
        return {"requested": True, "speech": ""}

    async def party(self, enabled: bool, player: str = "", hours: int = 4):
        if not 1 <= hours <= 24: raise InvalidDataError("Durée de soirée : 1 à 24 heures.")
        values = {"enabled": True, "enable_guest_access": enabled}
        if enabled:
            values.update({"player": self.player(player), "mode": "venue", "party_duration": hours,
                "enable_rate_limiting": True, "add_queue_limit": 2, "add_queue_refill_minutes": 5,
                "enable_boost": True, "boost_limit": 1, "boost_refill_minutes": 5,
                "enable_skip_song": False, "prevent_duplicate_tracks": True, "party_name": "Soirée maison"})
        await self.mass.config.save_provider_config("party", values, "party")
        provider = self.mass.get_provider("party")
        return {"enabled": enabled, "url": await provider.get_party_url() if enabled and provider else None}

    async def diagnostics(self):
        return {"version": VERSION, "members_count": len(self.providers()), "mixes_count": len(self._state["mixes"]),
            "catalog_tracks": sorted(len(c[1]) for c in self._catalogs.values()), "timings": list(self._metrics),
            "paid_ai_calls": 0, "family_plugin_downloads_audio": False,
            "limitations": ["Durées play_request = traitement logiciel, pas mesure acoustique", "Ambiances fondées sur métadonnées, pas analyse audio"]}

    async def bridge(self, action, payload):
        key = await asyncio.to_thread(Path(os.environ.get("OPENDEEZER_ADMIN_TOKEN_FILE", "/run/secrets/opendeezer-admin")).read_text)
        base = os.environ.get("OPENDEEZER_ENROLL_URL", "http://127.0.0.1:8731").rstrip("/")
        async with self.mass.http_session.post(base + "/admin/" + action, json=payload,
            headers={"Authorization": "Bearer " + key.strip()}, timeout=ClientTimeout(total=45), allow_redirects=False) as r:
            data = await r.json()
            if r.status not in (200, 201): raise InvalidDataError("Action de compte refusée : " + str(data.get("error", r.status)))
            return data

    async def reconnect(self, member: str, arl: str):
        # Configured but unavailable providers are included for expired sessions.
        configs = await self.mass.config.get_provider_configs()
        configs = [c for c in configs if c.domain == "opendeezer_home" and c.instance_id == member]
        if len(configs) != 1: raise InvalidDataError("Choisissez l'instance du compte à reconnecter.")
        raw = self.mass.config.get("providers/" + member)
        values = self.mass.config._decrypt_values(raw.get("setup_data") or {})
        await self.bridge("reconnect", {"profile_token": values["profile_token"], "arl": arl})
        await self.mass.load_provider_config(configs[0])
        self._catalogs.clear()
        return {"reconnected": True}

    async def remove_member(self, member: str, confirm: bool = False):
        if not confirm: raise InvalidDataError("Confirmez le retrait de ce compte.")
        configs = await self.mass.config.get_provider_configs(provider_domain="opendeezer_home")
        if not any(c.instance_id == member for c in configs): raise InvalidDataError("Compte absent.")
        p = next((p for p in self.providers() if p.instance_id == member), None)
        pid = p.profile_id if p else None
        raw = self.mass.config.get("providers/" + member)
        values = self.mass.config._decrypt_values(raw.get("setup_data") or {})
        result = await self.bridge("revoke", {"profile_token": values["profile_token"]})
        pid = pid or result.get("profile_id")
        await self.mass.config.remove_provider_config(member)
        async with self._lock:
            self._state["preferences"].pop(pid, None); self._state["last_mix"].pop(pid, None)
            self._state["mixes"] = {k:v for k,v in self._state["mixes"].items() if pid not in v["members"]}
            self._state["presets"] = {k:v for k,v in self._state["presets"].items() if pid not in v["members"]}
            await self._save()
        self._catalogs.pop(pid, None)
        return {"removed": True}

    async def pair_begin(self, label: str):
        label = label.strip()
        if not 1 <= len(label) <= 40: raise InvalidDataError("Indiquez le prénom du membre.")
        self._pairs = {k:v for k,v in self._pairs.items() if v["expires"] > time.monotonic()}
        if len(self._pairs) >= 8: raise InvalidDataError("Trop de connexions en attente. Réessayez dans dix minutes.")
        ticket = secrets.token_urlsafe(32)
        self._pairs[ticket] = {"label": label, "expires": time.monotonic()+600, "arl": None}
        return {"ticket": ticket, "label": label, "expires_in": 600}

    async def pair_cancel(self, ticket: str):
        self._pairs.pop(ticket, None)
        return {"cancelled": True}

    async def pair_finish(self, ticket: str):
        pair = self._pairs.get(ticket)
        if not pair or pair["expires"] <= time.monotonic():
            self._pairs.pop(ticket, None); raise InvalidDataError("Connexion temporaire expirée.")
        if not pair["arl"]: return {"waiting": True}
        self._pairs.pop(ticket)
        step = await self.mass.config.setup_provider("opendeezer_home")
        step = await self.mass.config.submit_setup_flow(step.flow_id, {"profile_label": pair["label"], "deezer_arl": pair["arl"]})
        pair["arl"] = None
        for _ in range(120):
            if step.type.value not in ("progress",): break
            await asyncio.sleep(0.25); step = await self.mass.config.get_setup_flow(step.flow_id)
        if step.type.value != "finish":
            raise InvalidDataError("Compte non ajouté. Consultez le formulaire OpenDeezer : compte déjà présent ou connexion refusée.")
        return {"added": True}

    async def web_handler(self, request):
        suffix = request.path.removeprefix("/opendeezer-family/")
        if suffix == "pair":
            origin = request.headers.get("Origin", "")
            cors = {"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST, OPTIONS", "Vary": "Origin"} if origin.startswith(("chrome-extension://", "moz-extension://")) else {}
            if request.method == "OPTIONS": return web.Response(status=204, headers=cors)
            if request.method != "POST" or request.content_length is None or request.content_length > 4096: return web.Response(status=400, headers=cors)
            now = time.monotonic()
            if len(self._pair_attempts) == 40 and now - self._pair_attempts[0] < 60: return web.Response(status=429, headers=cors)
            self._pair_attempts.append(now)
            try: data = await request.json()
            except Exception: return web.Response(status=400, headers=cors)
            if not isinstance(data, dict) or not isinstance(data.get("ticket"), str): return web.Response(status=400, headers=cors)
            pair = self._pairs.get(data["ticket"])
            arl = data.get("arl", "")
            if not pair or pair["expires"] <= now or pair["arl"] is not None: return web.Response(status=403, headers=cors)
            if not isinstance(arl,str) or not 32 <= len(arl) <= 1024 or any(c.isspace() for c in arl): return web.Response(status=400, headers=cors)
            pair["arl"] = arl
            return web.json_response({"received": True}, headers={**cors,"Cache-Control":"no-store"})
        if request.method != "GET" or suffix not in ("", "index.html", "app.js", "style.css", "family-card.js"):
            return web.Response(status=404)
        name = suffix or "index.html"
        text = await asyncio.to_thread((Path(__file__).parent / "web" / name).read_text)
        return web.Response(text=text, content_type={"html":"text/html","js":"text/javascript","css":"text/css"}[name.rsplit('.',1)[1]],
            headers={"Cache-Control":"no-store","Referrer-Policy":"no-referrer","X-Content-Type-Options":"nosniff", "Content-Security-Policy":"default-src 'self'; connect-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'self'"})
