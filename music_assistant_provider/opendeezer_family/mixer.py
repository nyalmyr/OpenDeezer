"""Explainable, weighted household mixes. No network, paid model or audio inference."""
import math
import random
import unicodedata


def normalize(value):
    return "".join(c for c in unicodedata.normalize("NFKD", str(value).lower()) if not unicodedata.combining(c))


MOODS = {
    "chill": {"chill", "calme", "acoustic", "acoustique", "jazz", "lounge", "relax", "diner", "repas"},
    "festif": {"party", "dance", "fete", "festif", "hits", "workout", "pop", "running"},
    "disney": {"disney", "vaiana", "encanto", "reine des neiges"},
    "rock": {"rock", "metal", "indie"},
    "decouverte": set(),
}


def build_mix(catalogs, weights, preferences, minutes=60, mood="", end_mood="", seed=0):
    """Fairly interleave members, favour shared artists, and honour every member's bans."""
    if not catalogs or len(catalogs) > 20:
        raise ValueError("Choisissez entre un et vingt membres connectés.")
    if not 5 <= minutes <= 480:
        raise ValueError("La durée doit être comprise entre 5 et 480 minutes.")
    normalized_weights = {}
    for member in catalogs:
        w = float(weights.get(member, 1))
        if not math.isfinite(w) or not 0 < w <= 10:
            raise ValueError("Les poids doivent être compris entre 0 et 10, zéro exclu.")
        normalized_weights[member] = w
    artist_members = {}
    for member, tracks in catalogs.items():
        for track in tracks:
            for artist in track.get("artists", []):
                artist_members.setdefault(normalize(artist), set()).add(member)
    veto = {track_id for member in catalogs for track_id, rating in preferences.get(member, {}).items() if rating == "ban"}
    candidates = {m: [t for t in ts if t["id"] not in veto] for m, ts in catalogs.items()}
    empty = [m for m, ts in candidates.items() if not ts]
    if empty:
        raise ValueError("Aucun morceau utilisable pour : " + ", ".join(empty))
    rng = random.Random(seed)
    for tracks in candidates.values():
        rng.shuffle(tracks)
    selected, seen, counts, elapsed = [], set(), {m: 0 for m in catalogs}, 0
    mood_matches = 0
    while elapsed < minutes * 60 and len(selected) < 200:
        available = [m for m, ts in candidates.items() if any(t["id"] not in seen for t in ts)]
        if not available:
            break
        member = min(available, key=lambda m: counts[m] / normalized_weights[m])
        desired = end_mood if end_mood and elapsed >= minutes * 30 else mood
        words = MOODS.get(normalize(desired), {normalize(desired)} if desired else set())
        recent_artists = {normalize(a) for t in selected[-2:] for a in t.get("artists", [])}

        def score(track):
            common = max((len(artist_members.get(normalize(a), set())) for a in track.get("artists", [])), default=1)
            ratings = [preferences.get(m, {}).get(track["id"]) for m in catalogs]
            affinity = ratings.count("like") * 5 - ratings.count("dislike") * 8
            text = normalize(" ".join(track.get("playlists", [])))
            matches = bool(words and any(word in text for word in words))
            repeat = bool(recent_artists.intersection(normalize(a) for a in track.get("artists", [])))
            return (common - 1) * 4 + affinity + int(matches) * 20 - int(repeat) * 12, common, matches

        track = max((t for t in candidates[member] if t["id"] not in seen), key=lambda t: score(t)[0])
        _, common, matches = score(track)
        reasons = ["sélection de " + member]
        if common > 1:
            reasons.append("artiste présent chez plusieurs participants")
        if matches:
            reasons.append("ambiance repérée dans les noms des playlists")
            mood_matches += 1
        if any(preferences.get(m, {}).get(track["id"]) == "like" for m in catalogs):
            reasons.append("apprécié explicitement")
        selected.append({**track, "selected_for": member, "reason": "; ".join(reasons)})
        seen.add(track["id"]); counts[member] += 1
        elapsed += max(30, min(3600, int(track.get("duration") or 210)))
    warnings = []
    if elapsed < minutes * 60:
        warnings.append("La bibliothèque disponible ne suffit pas à atteindre la durée demandée sans répétition.")
    if mood and not mood_matches:
        warnings.append("Ambiance non retrouvée dans les métadonnées disponibles ; sélection fondée sur les goûts uniquement.")
    if mood and normalize(mood) != "decouverte":
        words = MOODS.get(normalize(mood), {normalize(mood)})
        for member, ts in catalogs.items():
            if not any(any(w in normalize(" ".join(t.get("playlists", []))) for w in words) for t in ts):
                warnings.append("Aucune playlist correspondant à cette ambiance chez " + member + " ; ses goûts généraux sont utilisés.")
    return {"tracks": selected, "contributions": counts, "duration_seconds": elapsed,
            "warnings": warnings, "method": "pondération des participants, artistes partagés, préférences explicites et noms de playlists"}
