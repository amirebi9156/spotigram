"""
Spotify playlists -> Telegram forum topics.
Lightweight: one process, one download at a time, SQLite state.

Usage:
    python bot.py auth   # one-time Spotify login
    python bot.py        # run the sync loop
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import aiohttp
import spotipy
import yt_dlp
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import BufferedInputFile, FSInputFile
from spotipy.oauth2 import SpotifyOAuth


def load_env(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


load_env()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = int(os.environ.get("CHAT_ID", "0"))  # supergroup id, like -100123456789
SYNC_INTERVAL = int(os.environ.get("SYNC_INTERVAL", "600"))  # seconds
ONLY_PLAYLISTS = {x.strip() for x in os.environ.get("ONLY_PLAYLISTS", "").split(",") if x.strip()}
YTDLP_COOKIES = os.environ.get("YTDLP_COOKIES", "")  # optional cookies.txt path
SEND_DELAY = float(os.environ.get("SEND_DELAY", "3.5"))  # seconds between sends
DURATION_TOLERANCE = 20  # seconds allowed between Spotify and YouTube duration
MAX_ATTEMPTS = 3
DB_PATH = os.environ.get("DB_PATH", "state.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sp2tg")

# ---------------------------------------------------------------- database
db = sqlite3.connect(DB_PATH)
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS playlists (
        playlist_id TEXT PRIMARY KEY, topic_id INTEGER, snapshot_id TEXT, name TEXT);
    CREATE TABLE IF NOT EXISTS tracks (
        playlist_id TEXT, track_id TEXT, message_id INTEGER,
        PRIMARY KEY (playlist_id, track_id));
    CREATE TABLE IF NOT EXISTS files (track_id TEXT PRIMARY KEY, file_id TEXT);
    CREATE TABLE IF NOT EXISTS failed (
        playlist_id TEXT, track_id TEXT, attempts INTEGER DEFAULT 0,
        PRIMARY KEY (playlist_id, track_id));
    """
)
db.commit()


# ----------------------------------------------------------------- spotify
def get_sp() -> spotipy.Spotify:
    auth = SpotifyOAuth(
        client_id=os.environ["SPOTIPY_CLIENT_ID"],
        client_secret=os.environ["SPOTIPY_CLIENT_SECRET"],
        redirect_uri=os.environ.get("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8888/callback"),
        scope="playlist-read-private playlist-read-collaborative",
        cache_path=".spotify_cache",
        open_browser=False,
    )
    return spotipy.Spotify(auth_manager=auth, requests_timeout=20, retries=3)


def fetch_playlists(sp: spotipy.Spotify) -> list[dict]:
    out, res = [], sp.current_user_playlists(limit=50)
    while res:
        out.extend(p for p in res["items"] if p)
        res = sp.next(res) if res.get("next") else None
    return out


def fetch_tracks(sp: spotipy.Spotify, playlist_id: str) -> list[dict]:
    out, res = [], sp.playlist_items(playlist_id, limit=100, additional_types=("track",))
    while res:
        for it in res["items"]:
            t = it.get("track") or it.get("item")  # API field name differs between versions
            if not t or t.get("is_local") or not t.get("id"):
                continue
            images = (t.get("album") or {}).get("images") or []
            cover = None
            if images:
                cover = min(images, key=lambda i: abs((i.get("width") or 300) - 300))["url"]
            out.append(
                {
                    "id": t["id"],
                    "title": t["name"],
                    "artists": [a["name"] for a in t.get("artists", [])] or ["Unknown"],
                    "duration": (t.get("duration_ms") or 0) // 1000,
                    "cover": cover,
                }
            )
        res = sp.next(res) if res.get("next") else None
    return out


# ---------------------------------------------------------------- download
def download_audio(track: dict, outdir: str) -> str:
    """Search YouTube, pick the closest duration match, download m4a (no ffmpeg needed)."""
    base = {"quiet": True, "no_warnings": True, "noplaylist": True}
    if YTDLP_COOKIES:
        base["cookiefile"] = YTDLP_COOKIES

    query = f"{track['artists'][0]} {track['title']}"
    with yt_dlp.YoutubeDL({**base, "extract_flat": True}) as ydl:
        entries = ydl.extract_info(f"ytsearch5:{query}", download=False).get("entries") or []

    def diff(e: dict) -> float:
        return abs(e["duration"] - track["duration"]) if e.get("duration") else 999

    entries = [e for e in entries if e and e.get("id")]
    if not entries:
        raise RuntimeError("no search results")
    best = min(entries, key=diff)
    if track["duration"] and diff(best) > DURATION_TOLERANCE:
        raise RuntimeError(f"no close duration match (best diff {diff(best):.0f}s)")

    opts = {
        **base,
        "format": "bestaudio[ext=m4a]/bestaudio",
        "outtmpl": os.path.join(outdir, "%(id)s.%(ext)s"),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={best['id']}", download=True)
        return ydl.prepare_filename(info)


async def fetch_cover(session: aiohttp.ClientSession, url: str | None) -> bytes | None:
    if not url:
        return None
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
            return await r.read() if r.status == 200 else None
    except Exception:
        return None


# ---------------------------------------------------------------- telegram
async def tg_call(coro_factory):
    """Run a Telegram call, sleeping through flood-wait errors."""
    while True:
        try:
            return await coro_factory()
        except TelegramRetryAfter as e:
            log.warning("flood wait %ss", e.retry_after)
            await asyncio.sleep(e.retry_after + 1)


async def send_track(bot: Bot, topic_id: int, track: dict, session: aiohttp.ClientSession) -> int:
    performer = ", ".join(track["artists"])
    cached = db.execute("SELECT file_id FROM files WHERE track_id=?", (track["id"],)).fetchone()

    if cached:
        msg = await tg_call(
            lambda: bot.send_audio(CHAT_ID, cached[0], message_thread_id=topic_id)
        )
        return msg.message_id

    tmp = tempfile.mkdtemp(prefix="sp2tg_")
    try:
        path = await asyncio.to_thread(download_audio, track, tmp)
        cover = await fetch_cover(session, track["cover"])
        thumb = BufferedInputFile(cover, "cover.jpg") if cover else None
        msg = await tg_call(
            lambda: bot.send_audio(
                CHAT_ID,
                FSInputFile(path),
                message_thread_id=topic_id,
                title=track["title"],
                performer=performer,
                duration=track["duration"] or None,
                thumbnail=thumb,
            )
        )
        db.execute(
            "INSERT OR REPLACE INTO files VALUES (?, ?)", (track["id"], msg.audio.file_id)
        )
        db.commit()
        return msg.message_id
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def ensure_topic(bot: Bot, pl: dict) -> int:
    row = db.execute("SELECT topic_id FROM playlists WHERE playlist_id=?", (pl["id"],)).fetchone()
    if row and row[0]:
        return row[0]
    topic = await tg_call(lambda: bot.create_forum_topic(CHAT_ID, pl["name"][:128] or "Playlist"))
    db.execute(
        "INSERT OR REPLACE INTO playlists (playlist_id, topic_id, snapshot_id, name) VALUES (?,?,?,?)",
        (pl["id"], topic.message_thread_id, None, pl["name"]),
    )
    db.commit()
    await asyncio.sleep(SEND_DELAY)
    return topic.message_thread_id


# -------------------------------------------------------------------- sync
async def sync_playlist(bot, sp, pl, session) -> None:
    row = db.execute(
        "SELECT snapshot_id FROM playlists WHERE playlist_id=?", (pl["id"],)
    ).fetchone()
    if row and row[0] == pl.get("snapshot_id"):
        return  # unchanged

    log.info("syncing playlist: %s", pl["name"])
    topic_id = await ensure_topic(bot, pl)
    tracks = await asyncio.to_thread(fetch_tracks, sp, pl["id"])
    done = {r[0] for r in db.execute("SELECT track_id FROM tracks WHERE playlist_id=?", (pl["id"],))}
    complete = True

    for t in tracks:
        if t["id"] in done:
            continue
        fr = db.execute(
            "SELECT attempts FROM failed WHERE playlist_id=? AND track_id=?", (pl["id"], t["id"])
        ).fetchone()
        if fr and fr[0] >= MAX_ATTEMPTS:
            continue
        try:
            mid = await send_track(bot, topic_id, t, session)
            db.execute("INSERT OR REPLACE INTO tracks VALUES (?,?,?)", (pl["id"], t["id"], mid))
            db.execute("DELETE FROM failed WHERE playlist_id=? AND track_id=?", (pl["id"], t["id"]))
            db.commit()
            log.info("sent: %s - %s", t["artists"][0], t["title"])
        except TelegramBadRequest as e:
            if "thread" in str(e).lower():
                # topic was deleted in Telegram: forget it, recreate next cycle
                log.error("topic missing for %s, will recreate", pl["name"])
                db.execute("UPDATE playlists SET topic_id=NULL WHERE playlist_id=?", (pl["id"],))
                db.commit()
                return
            raise
        except Exception as e:
            log.warning("failed: %s - %s (%s)", t["artists"][0], t["title"], e)
            attempts = (fr[0] if fr else 0) + 1
            db.execute(
                "INSERT OR REPLACE INTO failed VALUES (?,?,?)", (pl["id"], t["id"], attempts)
            )
            db.commit()
            if attempts < MAX_ATTEMPTS:
                complete = False
        await asyncio.sleep(SEND_DELAY)

    if complete:  # only remember the snapshot when nothing is left to retry
        db.execute(
            "UPDATE playlists SET snapshot_id=?, name=? WHERE playlist_id=?",
            (pl.get("snapshot_id"), pl["name"], pl["id"]),
        )
        db.commit()


async def sync_all(bot: Bot, sp: spotipy.Spotify) -> None:
    playlists = await asyncio.to_thread(fetch_playlists, sp)
    async with aiohttp.ClientSession() as session:
        for pl in playlists:
            if ONLY_PLAYLISTS and pl["id"] not in ONLY_PLAYLISTS:
                continue
            try:
                await sync_playlist(bot, sp, pl, session)
            except Exception:
                log.exception("playlist failed: %s", pl.get("name"))


async def main() -> None:
    sp = get_sp()
    bot = Bot(BOT_TOKEN)
    try:
        while True:
            try:
                await sync_all(bot, sp)
            except Exception:
                log.exception("sync cycle failed")
            await asyncio.sleep(SYNC_INTERVAL)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        me = get_sp().current_user()
        print("OK, logged in as:", me.get("display_name") or me["id"])
    else:
        asyncio.run(main())
