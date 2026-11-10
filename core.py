"""Shared config, database, Telegram and yt-dlp helpers for Spotigram."""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from pathlib import Path

import aiohttp
import yt_dlp
from aiogram import F, Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


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


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


BOT_TOKEN = _env("BOT_TOKEN")
CHAT_ID = int(_env("CHAT_ID", "0") or 0)  # supergroup id, like -100123456789
SYNC_INTERVAL = int(_env("SYNC_INTERVAL", "600"))  # playlist check interval, seconds
YTDLP_COOKIES = _env("YTDLP_COOKIES")  # optional cookies.txt path
SEND_DELAY = float(_env("SEND_DELAY", "3.5"))  # seconds between playlist sends
PLAYLISTS_FILE = _env("PLAYLISTS_FILE", "playlists.txt")
ARTISTS_FILE = _env("ARTISTS_FILE", "artists.txt")
DB_PATH = _env("DB_PATH", "state.db")

# new-release monitor
RELEASE_INTERVAL = int(_env("RELEASE_INTERVAL", "180"))  # seconds between checks
RELEASES_TOPIC_NAME = _env("RELEASES_TOPIC_NAME", "🆕 New Releases")
RELEASE_MAX_AGE_HOURS = int(_env("RELEASE_MAX_AGE_HOURS", "72"))  # ignore older uploads
RELEASE_MIN_SECONDS = int(_env("RELEASE_MIN_SECONDS", "60"))
RELEASE_MAX_SECONDS = int(_env("RELEASE_MAX_SECONDS", "720"))
PICKS_TOPIC_NAME = _env("PICKS_TOPIC_NAME", "🎧 Picks")  # songs added from the search menu
RELEASE_MAX_ATTEMPTS = int(_env("RELEASE_MAX_ATTEMPTS", "20"))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# no ffmpeg needed: progressive m4a (YouTube) / mp3 (SoundCloud) formats
AUDIO_FMT_YT = "bestaudio[ext=m4a]/bestaudio"
AUDIO_FMT_SC = "http_mp3_128/bestaudio[ext=mp3]/bestaudio[ext=m4a]/bestaudio"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("spotigram")

# ---------------------------------------------------------------- database
db = sqlite3.connect(DB_PATH)
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS playlists (
        playlist_id TEXT PRIMARY KEY, topic_id INTEGER, name TEXT, cover_sent INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS tracks (
        playlist_id TEXT, track_id TEXT, message_id INTEGER,
        PRIMARY KEY (playlist_id, track_id));
    CREATE TABLE IF NOT EXISTS files (track_id TEXT PRIMARY KEY, file_id TEXT);
    CREATE TABLE IF NOT EXISTS failed (
        playlist_id TEXT, track_id TEXT, attempts INTEGER DEFAULT 0,
        PRIMARY KEY (playlist_id, track_id));
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS releases (
        key TEXT PRIMARY KEY, artist TEXT, title TEXT, platform TEXT, url TEXT,
        sent_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS seen (item TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS source_state (src TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS attempts (item TEXT PRIMARY KEY, n INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS lyrics_cache (key TEXT PRIMARY KEY, text TEXT);
    CREATE TABLE IF NOT EXISTS picks (
        key TEXT PRIMARY KEY, dz_id INTEGER, artist TEXT, title TEXT, added_at TEXT DEFAULT CURRENT_TIMESTAMP);
    """
)
try:  # databases created by older versions have no cover_sent column
    db.execute("ALTER TABLE playlists ADD COLUMN cover_sent INTEGER DEFAULT 0")
except sqlite3.OperationalError:
    pass
db.commit()

# --------------------------------------------------------------- telegram
_dl_lock: asyncio.Lock | None = None


def dl_lock() -> asyncio.Lock:
    """One download at a time (keeps CPU/RAM low). Created lazily inside the running loop."""
    global _dl_lock
    if _dl_lock is None:
        _dl_lock = asyncio.Lock()
    return _dl_lock


def lyrics_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📝 Lyrics", callback_data="lyr")]]
    )


async def tg_call(coro_factory):
    """Run a Telegram call, sleeping through flood-wait errors."""
    while True:
        try:
            return await coro_factory()
        except TelegramRetryAfter as e:
            log.warning("flood wait %ss", e.retry_after)
            await asyncio.sleep(e.retry_after + 1)


async def topic_id_for(bot: Bot, key: str, name: str, color: int | None = None, force_new: bool = False) -> int:
    """Topic id stored under meta[key]; creates the topic when missing (or when force_new)."""
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if row and not force_new:
        return int(row[0])
    kw = {"icon_color": color} if color else {}
    topic = await tg_call(lambda: bot.create_forum_topic(CHAT_ID, name[:128], **kw))
    db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(topic.message_thread_id)))
    db.commit()
    return topic.message_thread_id


async def send_audio_topic(bot: Bot, key: str, name: str, color: int | None, audio, **kw):
    """Send audio into a named topic; recreate the topic once if someone deleted it."""
    for attempt in (1, 2):
        topic_id = await topic_id_for(bot, key, name, color, force_new=(attempt == 2))
        try:
            return await tg_call(lambda: bot.send_audio(CHAT_ID, audio, message_thread_id=topic_id, **kw))
        except TelegramBadRequest as e:
            if attempt == 1 and "thread" in str(e).lower():
                log.error("topic %s was deleted, creating a new one", key)
                continue
            raise


# ------------------------------------------------------------------- auth
ANON_ADMIN_ID = 1087968824  # Telegram's "GroupAnonymousBot" (admin posting anonymously)


async def _status(bot: Bot, uid: int) -> str:
    try:
        return (await bot.get_chat_member(CHAT_ID, uid)).status
    except Exception:
        return "left"


async def is_admin(bot: Bot, uid: int) -> bool:
    return uid == ANON_ADMIN_ID or await _status(bot, uid) in ("creator", "administrator")


async def can_use(bot: Bot, chat, uid: int) -> bool:
    """Inside the music group: anyone there. In a private chat: only group members."""
    if chat.id == CHAT_ID:
        return True
    return await _status(bot, uid) in ("creator", "administrator", "member", "restricted")


ALLOWED_MSG = (F.chat.id == CHAT_ID) | (F.chat.type == "private")
ALLOWED_CB = (F.message.chat.id == CHAT_ID) | (F.message.chat.type == "private")


async def fetch_image(session: aiohttp.ClientSession, url: str, max_bytes: int = 10_000_000) -> bytes | None:
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                return None
            data = await r.read()
        return data if len(data) <= max_bytes else None
    except Exception:
        return None


# ----------------------------------------------------------------- yt-dlp
def ytdlp_opts(**extra) -> dict:
    opts: dict = {"quiet": True, "no_warnings": True}
    if YTDLP_COOKIES:
        opts["cookiefile"] = YTDLP_COOKIES
    opts.update(extra)
    return opts


def download_url(url: str, outdir: str, fmt: str) -> tuple[str, dict]:
    """Blocking: download one video/track as audio. Returns (file path, info dict)."""
    opts = ytdlp_opts(noplaylist=True, format=fmt, outtmpl=os.path.join(outdir, "%(id)s.%(ext)s"))
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info), info
