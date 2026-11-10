"""
Music metadata (search artists/songs, top songs, albums) from Deezer's public API.
No key or login needed. Only metadata is used here; audio still comes from YouTube/SoundCloud.
"""
from __future__ import annotations

import time

import aiohttp

from core import HEADERS, log

API = "https://api.deezer.com"
_TTL = 600  # seconds to keep artist/album lists in memory
_cache: dict[str, tuple[float, object]] = {}


async def _get(path: str, params: dict | None = None) -> dict | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{API}{path}", params=params, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                data = await r.json(content_type=None)
    except Exception as e:
        log.warning("deezer %s failed: %s", path, e)
        return None
    if not isinstance(data, dict) or "error" in data:
        return None
    return data


async def _cached(key: str, path: str, params: dict | None = None) -> dict | None:
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]  # type: ignore[return-value]
    data = await _get(path, params)
    if data is not None:
        if len(_cache) > 300:
            _cache.clear()
        _cache[key] = (time.time(), data)
    return data


def _track(t: dict, album: dict | None = None) -> dict | None:
    if not t or not t.get("id") or not t.get("title"):
        return None
    alb = t.get("album") or album or {}
    art = t.get("artist") or {}
    return {
        "id": int(t["id"]),
        "title": t["title"],
        "artist": art.get("name") or "",
        "artist_id": art.get("id"),
        "duration": int(t.get("duration") or 0),
        "album": alb.get("title") or "",
        "cover": alb.get("cover_medium") or alb.get("cover"),
    }


def _tracks(items, album: dict | None = None) -> list[dict]:
    out = [_track(t, album) for t in items or []]
    return [t for t in out if t]


async def search_artists(q: str, limit: int = 5) -> list[dict]:
    d = await _get("/search/artist", {"q": q, "limit": limit})
    return [
        {"id": int(a["id"]), "name": a["name"], "fans": a.get("nb_fan") or 0, "albums": a.get("nb_album") or 0}
        for a in (d or {}).get("data", []) if a.get("id") and a.get("name")
    ]


async def search_tracks(q: str, limit: int = 8) -> list[dict]:
    d = await _get("/search", {"q": q, "limit": limit})
    return _tracks((d or {}).get("data"))


async def artist(artist_id: int) -> dict | None:
    d = await _cached(f"a{artist_id}", f"/artist/{artist_id}")
    if not d or not d.get("name"):
        return None
    return {"id": int(d["id"]), "name": d["name"], "fans": d.get("nb_fan") or 0, "albums": d.get("nb_album") or 0}


async def artist_top(artist_id: int, limit: int = 50) -> list[dict]:
    d = await _cached(f"t{artist_id}", f"/artist/{artist_id}/top", {"limit": limit})
    return _tracks((d or {}).get("data"))


async def artist_albums(artist_id: int, limit: int = 100) -> list[dict]:
    d = await _cached(f"b{artist_id}", f"/artist/{artist_id}/albums", {"limit": limit})
    albums = [
        {
            "id": int(a["id"]), "title": a.get("title") or "?", "year": (a.get("release_date") or "")[:4],
            "type": a.get("record_type") or "album", "date": a.get("release_date") or "",
        }
        for a in (d or {}).get("data", []) if a.get("id")
    ]
    return sorted(albums, key=lambda a: a["date"], reverse=True)


async def album(album_id: int) -> dict | None:
    d = await _cached(f"l{album_id}", f"/album/{album_id}")
    if not d or not d.get("title"):
        return None
    art = d.get("artist") or {}
    tracks = _tracks((d.get("tracks") or {}).get("data"), album=d)
    for t in tracks:
        t["album"] = t["album"] or d["title"]
        t["cover"] = t["cover"] or d.get("cover_medium")
    return {
        "id": int(d["id"]), "title": d["title"], "year": (d.get("release_date") or "")[:4],
        "artist": art.get("name") or "", "artist_id": art.get("id"), "tracks": tracks,
    }


async def track(track_id: int) -> dict | None:
    d = await _get(f"/track/{track_id}")
    return _track(d) if d else None
