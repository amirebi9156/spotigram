"""
Spotify public playlists -> Telegram forum topics.
No Spotify API / Premium needed: playlists are read from Spotify's public embed page,
so every playlist must be set to Public.
"""
from __future__ import annotations

import asyncio
import colorsys
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import aiohttp
import yt_dlp
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, FSInputFile

from core import (
    AUDIO_FMT_YT, CHAT_ID, HEADERS, PLAYLISTS_FILE, SEND_DELAY, SYNC_INTERVAL,
    db, dl_lock, fetch_image, log, lyrics_kb, tg_call, ytdlp_opts,
)

DURATION_TOLERANCE = 20  # seconds allowed between Spotify and YouTube duration
MAX_ATTEMPTS = 3

# ----------------------------------------------------------------- spotify
_ID_RE = re.compile(r"playlist[/:]([A-Za-z0-9]{22})")
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def parse_playlist_id(s: str) -> str | None:
    """Accepts a playlist URL, a spotify:playlist: URI or a bare 22-char id."""
    s = s.strip()
    m = _ID_RE.search(s)
    if m:
        return m.group(1)
    return s if re.fullmatch(r"[A-Za-z0-9]{22}", s) else None


def read_playlist_ids() -> list[str]:
    p = Path(PLAYLISTS_FILE)
    if not p.exists():
        return []
    out: list[str] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        pid = parse_playlist_id(line.split("#", 1)[0])
        if pid and pid not in out:
            out.append(pid)
    return out


def parse_embed(html: str) -> dict:
    m = _NEXT_DATA_RE.search(html)
    if not m:
        raise RuntimeError("no data on embed page (private playlist, wrong id, or blocked)")
    try:
        ent = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    except (KeyError, TypeError, ValueError) as e:
        raise RuntimeError(f"unexpected embed page format ({e!r})") from e

    tracks = []
    for t in ent.get("trackList") or []:
        uri = t.get("uri") or ""
        if not uri.startswith("spotify:track:"):
            continue  # skip podcasts/episodes
        performer = (t.get("subtitle") or "Unknown").replace("\xa0", " ").strip()
        tracks.append(
            {
                "id": uri.split(":")[-1],
                "title": (t.get("title") or "").strip(),
                "performer": performer,
                "artist": performer.split(",")[0].strip() or performer,
                "duration": int(t.get("duration") or 0) // 1000,
            }
        )
    name = (ent.get("title") or ent.get("name") or "Playlist").strip()

    # playlist cover: biggest image up to 640px, from coverArt or visualIdentity
    images: list[dict] = []
    for src in ((ent.get("coverArt") or {}).get("sources"), (ent.get("visualIdentity") or {}).get("image")):
        if src:
            images = src
            break
    images = [i for i in images if i.get("url")]
    best = max(images, key=lambda i: min(i.get("maxWidth") or i.get("width") or 0, 640), default=None)
    cover_url = best["url"] if best else None

    bg = (ent.get("visualIdentity") or {}).get("backgroundBase") or {}
    color = (bg["red"], bg["green"], bg["blue"]) if {"red", "green", "blue"} <= bg.keys() else None
    return {"name": name, "tracks": tracks, "cover_url": cover_url, "color": color}


async def fetch_playlist(session: aiohttp.ClientSession, pid: str) -> dict:
    url = f"https://open.spotify.com/embed/playlist/{pid}"
    async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30)) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status} (is the playlist Public?)")
        html = await r.text()
    return parse_embed(html)


async def fetch_track_cover(session: aiohttp.ClientSession, track_id: str) -> bytes | None:
    """Cover art via Spotify's public oEmbed endpoint. Optional: failures are ignored."""
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with session.get(
            "https://open.spotify.com/oembed",
            params={"url": f"spotify:track:{track_id}"},
            headers=HEADERS,
            timeout=timeout,
        ) as r:
            if r.status != 200:
                return None
            thumb = (await r.json(content_type=None)).get("thumbnail_url")
        if not thumb:
            return None
        return await fetch_image(session, thumb, max_bytes=200_000)  # Telegram thumbnail limit
    except Exception:
        return None


# ---------------------------------------------------------------- download
def download_audio(track: dict, outdir: str) -> str:
    """Search YouTube, pick the closest duration match, download m4a (no ffmpeg needed)."""
    query = f"{track['artist']} {track['title']}"
    with yt_dlp.YoutubeDL(ytdlp_opts(noplaylist=True, extract_flat=True)) as ydl:
        entries = ydl.extract_info(f"ytsearch5:{query}", download=False).get("entries") or []

    def diff(e: dict) -> float:
        return abs(e["duration"] - track["duration"]) if e.get("duration") else 999

    entries = [e for e in entries if e and e.get("id")]
    if not entries:
        raise RuntimeError("no search results")
    best = min(entries, key=diff)
    if track["duration"] and diff(best) > DURATION_TOLERANCE:
        raise RuntimeError(f"no close duration match (best diff {diff(best):.0f}s)")

    opts = ytdlp_opts(
        noplaylist=True, format=AUDIO_FMT_YT, outtmpl=os.path.join(outdir, "%(id)s.%(ext)s")
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={best['id']}", download=True)
        return ydl.prepare_filename(info)


# ---------------------------------------------------------------- telegram
async def send_track(bot: Bot, topic_id: int, track: dict, session: aiohttp.ClientSession) -> int:
    cached = db.execute("SELECT file_id FROM files WHERE track_id=?", (track["id"],)).fetchone()

    if cached:  # already uploaded once (e.g. same track in another playlist)
        msg = await tg_call(
            lambda: bot.send_audio(
                CHAT_ID, cached[0], message_thread_id=topic_id, reply_markup=lyrics_kb()
            )
        )
        return msg.message_id

    tmp = tempfile.mkdtemp(prefix="spotigram_")
    try:
        async with dl_lock():
            path = await asyncio.to_thread(download_audio, track, tmp)
        cover = await fetch_track_cover(session, track["id"])
        thumb = BufferedInputFile(cover, "cover.jpg") if cover else None
        msg = await tg_call(
            lambda: bot.send_audio(
                CHAT_ID,
                FSInputFile(path),
                message_thread_id=topic_id,
                title=track["title"],
                performer=track["performer"],
                duration=track["duration"] or None,
                thumbnail=thumb,
                reply_markup=lyrics_kb(),
            )
        )
        db.execute(
            "INSERT OR REPLACE INTO files VALUES (?, ?)", (track["id"], msg.audio.file_id)
        )
        db.commit()
        return msg.message_id
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# Telegram does not allow custom images as topic icons, only one of these 6 colors
# (or a custom emoji). We pick the color closest in hue to the playlist cover.
TOPIC_COLORS = [0x6FB9F0, 0xFFD67E, 0xCB86DB, 0x8EEE98, 0xFF93B2, 0xFB6F5F]


def pick_topic_color(rgb: tuple | None) -> int | None:
    if not rgb:
        return None
    h, s, _ = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
    if s < 0.15:  # greyish cover: keep the default blue
        return TOPIC_COLORS[0]

    def hue(c: int) -> float:
        return colorsys.rgb_to_hsv((c >> 16 & 255) / 255, (c >> 8 & 255) / 255, (c & 255) / 255)[0]

    def dist(c: int) -> float:
        d = abs(hue(c) - h)
        return min(d, 1 - d)

    return min(TOPIC_COLORS, key=dist)


async def send_cover(bot: Bot, session: aiohttp.ClientSession, pid: str, topic_id: int, data: dict) -> None:
    """Post the playlist cover as the first (pinned) message of its topic."""
    img = await fetch_image(session, data["cover_url"]) if data.get("cover_url") else None
    caption = f"{data['name']}\nhttps://open.spotify.com/playlist/{pid}"
    try:
        if img:
            msg = await tg_call(
                lambda: bot.send_photo(
                    CHAT_ID, BufferedInputFile(img, "cover.jpg"), message_thread_id=topic_id, caption=caption
                )
            )
        else:
            msg = await tg_call(lambda: bot.send_message(CHAT_ID, caption, message_thread_id=topic_id))
    except TelegramBadRequest as e:
        log.warning("cover not sent for %s: %s", data["name"], e)
        return
    try:
        await tg_call(lambda: bot.pin_chat_message(CHAT_ID, msg.message_id, disable_notification=True))
    except Exception as e:  # pinning is optional (needs the Pin Messages admin right)
        log.warning("could not pin cover: %s", e)
    db.execute("UPDATE playlists SET cover_sent=1 WHERE playlist_id=?", (pid,))
    db.commit()


async def ensure_topic(bot: Bot, pid: str, data: dict, session: aiohttp.ClientSession) -> int:
    name = data["name"]
    row = db.execute(
        "SELECT topic_id, name, cover_sent FROM playlists WHERE playlist_id=?", (pid,)
    ).fetchone()
    if row and row[0]:
        topic_id = row[0]
        if row[1] != name:  # playlist was renamed on Spotify
            try:
                await tg_call(lambda: bot.edit_forum_topic(CHAT_ID, topic_id, name=name[:128]))
            except TelegramBadRequest as e:
                log.warning("topic rename failed: %s", e)
            db.execute("UPDATE playlists SET name=? WHERE playlist_id=?", (name, pid))
            db.commit()
        if not row[2]:  # topics created before covers existed
            await send_cover(bot, session, pid, topic_id, data)
            await asyncio.sleep(SEND_DELAY)
        return topic_id

    kwargs = {}
    color = pick_topic_color(data.get("color"))
    if color:
        kwargs["icon_color"] = color
    topic = await tg_call(lambda: bot.create_forum_topic(CHAT_ID, name[:128] or "Playlist", **kwargs))
    db.execute(
        "INSERT OR REPLACE INTO playlists (playlist_id, topic_id, name, cover_sent) VALUES (?,?,?,0)",
        (pid, topic.message_thread_id, name),
    )
    db.commit()
    await asyncio.sleep(SEND_DELAY)
    await send_cover(bot, session, pid, topic.message_thread_id, data)
    await asyncio.sleep(SEND_DELAY)
    return topic.message_thread_id


# -------------------------------------------------------------------- sync
_pl_locks: dict[str, asyncio.Lock] = {}


async def sync_playlist(bot: Bot, pid: str, session: aiohttp.ClientSession) -> None:
    """One sync per playlist at a time (the loop and the Telegram menu may both trigger it)."""
    lock = _pl_locks.setdefault(pid, asyncio.Lock())
    async with lock:
        await _sync_playlist(bot, pid, session)


def add_playlist_id(pid: str, name: str = "") -> None:
    p = Path(PLAYLISTS_FILE)
    old = p.read_text(encoding="utf-8") if p.exists() else ""
    if pid in read_playlist_ids():
        return
    if old and not old.endswith("\n"):
        old += "\n"
    p.write_text(old + f"https://open.spotify.com/playlist/{pid}" + (f"  # {name}" if name else "") + "\n", encoding="utf-8")


def remove_playlist_id(pid: str) -> None:
    p = Path(PLAYLISTS_FILE)
    if not p.exists():
        return
    keep = [ln for ln in p.read_text(encoding="utf-8").splitlines() if parse_playlist_id(ln.split("#", 1)[0]) != pid]
    p.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")


def playlist_overview() -> list[dict]:
    out = []
    for pid in read_playlist_ids():
        row = db.execute("SELECT name, topic_id FROM playlists WHERE playlist_id=?", (pid,)).fetchone()
        n = db.execute("SELECT COUNT(*) FROM tracks WHERE playlist_id=?", (pid,)).fetchone()[0]
        out.append({"id": pid, "name": (row[0] if row and row[0] else pid), "topic_id": row[1] if row else None, "tracks": n})
    return out


async def _sync_playlist(bot: Bot, pid: str, session: aiohttp.ClientSession) -> None:
    data = await fetch_playlist(session, pid)
    name, tracks = data["name"], data["tracks"]
    topic_id = await ensure_topic(bot, pid, data, session)

    done = {r[0] for r in db.execute("SELECT track_id FROM tracks WHERE playlist_id=?", (pid,))}
    todo = [t for t in tracks if t["id"] not in done]
    if not todo:
        return
    log.info("%s: %d new track(s)", name, len(todo))

    for t in todo:
        fr = db.execute(
            "SELECT attempts FROM failed WHERE playlist_id=? AND track_id=?", (pid, t["id"])
        ).fetchone()
        if fr and fr[0] >= MAX_ATTEMPTS:
            continue
        try:
            mid = await send_track(bot, topic_id, t, session)
            db.execute("INSERT OR REPLACE INTO tracks VALUES (?,?,?)", (pid, t["id"], mid))
            db.execute("DELETE FROM failed WHERE playlist_id=? AND track_id=?", (pid, t["id"]))
            db.commit()
            log.info("sent: %s - %s", t["artist"], t["title"])
        except TelegramBadRequest as e:
            if "thread" in str(e).lower():
                # topic was deleted in Telegram: forget it and resend everything next cycle
                log.error("topic missing for %s, will recreate", name)
                db.execute("UPDATE playlists SET topic_id=NULL WHERE playlist_id=?", (pid,))
                db.execute("DELETE FROM tracks WHERE playlist_id=?", (pid,))
                db.commit()
                return
            raise
        except Exception as e:
            log.warning("failed: %s - %s (%s)", t["artist"], t["title"], e)
            attempts = (fr[0] if fr else 0) + 1
            db.execute("INSERT OR REPLACE INTO failed VALUES (?,?,?)", (pid, t["id"], attempts))
            db.commit()
        await asyncio.sleep(SEND_DELAY)


async def sync_all(bot: Bot) -> None:
    ids = read_playlist_ids()
    if not ids:
        log.warning("no playlists configured (add links with: spotigram -> Manage playlists)")
        return
    async with aiohttp.ClientSession() as session:
        for pid in ids:
            try:
                await sync_playlist(bot, pid, session)
            except Exception as e:
                log.error("playlist %s failed: %s", pid, e)
            await asyncio.sleep(1)


async def sync_loop(bot: Bot) -> None:
    while True:
        try:
            await sync_all(bot)
        except Exception:
            log.exception("playlist sync cycle failed")
        await asyncio.sleep(SYNC_INTERVAL)


async def info(arg: str) -> int:
    """CLI helper for install.sh: print '<id>\\t<name>\\t<track count>'."""
    pid = parse_playlist_id(arg)
    if not pid:
        print("that does not look like a Spotify playlist link", file=sys.stderr)
        return 1
    async with aiohttp.ClientSession() as session:
        try:
            data = await fetch_playlist(session, pid)
        except Exception as e:
            print(f"could not read playlist: {e}", file=sys.stderr)
            return 1
    print(f"{pid}\t{data['name']}\t{len(data['tracks'])}")
    return 0
