"""Lyrics lookup via LRCLIB (https://lrclib.net), a free community lyrics database (no API key)."""
from __future__ import annotations

import re
import unicodedata

import aiohttp
from aiogram import Bot
from aiogram.types import BufferedInputFile, Message, ReplyParameters

from core import db, log, tg_call

API = "https://lrclib.net/api"
UA = {"User-Agent": "Spotigram/1.0 (https://github.com/amirebi9156/spotigram)"}
MSG_LIMIT = 4000  # Telegram allows 4096 characters per message
MAX_MESSAGES = 3  # longer lyrics are sent as a .txt file instead


def first_artist(artist: str) -> str:
    return (artist or "").split(",")[0].strip()


def clean_title(title: str) -> str:
    """Drop '(feat. X)', '- From "Album"', '- Remastered 2011' style noise that breaks matching."""
    t = re.sub(r"\s*[\(\[]\s*(?:feat|ft|with|prod)\b[^\)\]]*[\)\]]", "", title or "", flags=re.I)
    t = re.sub(r"\s+-\s+(?:from|remaster|\d{4}\b|live|single|radio edit|version).*$", "", t, flags=re.I)
    return t.strip()


def _norm(s: str) -> str:
    return re.sub(r"\W+", "", unicodedata.normalize("NFKC", s or "").lower())


async def _get(session: aiohttp.ClientSession, path: str, params: dict):
    try:
        async with session.get(
            f"{API}/{path}", params=params, headers=UA, timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            if r.status != 200:
                return None
            return await r.json(content_type=None)
    except Exception as e:
        log.warning("lrclib %s failed: %s", path, e)
        return None


def _usable(d) -> bool:
    return isinstance(d, dict) and bool(d.get("plainLyrics") or d.get("syncedLyrics") or d.get("instrumental"))


async def find_lyrics(session: aiohttp.ClientSession, artist: str, title: str, duration: int = 0):
    a = first_artist(artist)
    titles = list(dict.fromkeys([title, clean_title(title)]))
    if a:
        for t in titles:
            params = {"artist_name": a, "track_name": t}
            if duration:
                params["duration"] = int(duration)
            d = await _get(session, "get", params)
            if _usable(d):
                return d
    searches = [{"track_name": titles[-1], "artist_name": a}] if a else []
    searches.append({"q": f"{a} {titles[-1]}".strip()})
    for params in searches:
        res = await _get(session, "search", params)
        if isinstance(res, list):
            res = [x for x in res if _usable(x)]
            if res:
                if duration:
                    res.sort(key=lambda x: abs((x.get("duration") or 0) - duration))
                return res[0]
    return None


def lyrics_text(d: dict) -> str | None:
    """Plain lyrics (timestamps stripped from synced ones); None for instrumentals."""
    t = d.get("plainLyrics")
    if not t and d.get("syncedLyrics"):
        t = re.sub(r"^\[[^\]]*\]\s?", "", d["syncedLyrics"], flags=re.M)
    return (t or "").strip() or None


def chunk(text: str, limit: int = MSG_LIMIT) -> list[str]:
    out: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > limit:  # absurdly long single line
            if cur:
                out.append(cur.rstrip("\n"))
                cur = ""
            out.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            out.append(cur.rstrip("\n"))
            cur = ""
        cur += line + "\n"
    if cur.strip():
        out.append(cur.rstrip("\n"))
    return out


async def search_candidates(query: str, limit: int = 6) -> list[dict]:
    """Songs on LRCLIB matching free text (for the 'search lyrics' menu)."""
    async with aiohttp.ClientSession() as session:
        res = await _get(session, "search", {"q": query})
    out, seen = [], set()
    for x in res if isinstance(res, list) else []:
        if not _usable(x):
            continue
        k = (_norm(x.get("artistName")), _norm(x.get("trackName")))
        if k in seen or not x.get("trackName"):
            continue
        seen.add(k)
        out.append({"artist": x.get("artistName") or "", "title": x["trackName"], "duration": int(x.get("duration") or 0)})
        if len(out) >= limit:
            break
    return out


async def deliver(bot: Bot, anchor: Message | None, artist: str, title: str, duration: int = 0,
                  dest: int | None = None) -> None:
    """Find lyrics and send them. With `dest` (a user id) they go to that private chat and the group
    stays clean; otherwise they are posted as a reply to `anchor` in its topic."""
    if dest is not None or anchor is None:
        chat_id, thread, reply = dest, None, None
    else:
        chat_id = anchor.chat.id
        thread = anchor.message_thread_id if anchor.is_topic_message else None
        reply = ReplyParameters(message_id=anchor.message_id, allow_sending_without_reply=True)

    async def say(text: str, as_reply: bool = True) -> None:
        await tg_call(
            lambda: bot.send_message(
                chat_id, text, message_thread_id=thread, reply_parameters=reply if as_reply else None
            )
        )

    shown_artist, shown_title = first_artist(artist), clean_title(title) or title
    head = f"🎵 {shown_artist} — {shown_title}".strip(" —")
    key = _norm(shown_artist) + "|" + _norm(shown_title)

    row = db.execute("SELECT text FROM lyrics_cache WHERE key=?", (key,)).fetchone()
    text = row[0] if row else None
    if text is None:
        async with aiohttp.ClientSession() as session:
            d = await find_lyrics(session, artist, title, duration)
        if d is None:
            await say(f"❌ No lyrics found for {head[2:]}.\nTry: /lyrics Artist - Title")
            return
        text = lyrics_text(d)
        if text is None:
            await say(f"🎼 {head[2:]} is instrumental.")
            return
        db.execute("INSERT OR REPLACE INTO lyrics_cache VALUES (?, ?)", (key, text))
        db.commit()

    parts = chunk(f"{head}\n\n{text}")
    if len(parts) > MAX_MESSAGES:
        doc = BufferedInputFile(f"{head}\n\n{text}".encode("utf-8"), f"{shown_artist} - {shown_title}.txt")
        await tg_call(
            lambda: bot.send_document(
                chat_id, doc, message_thread_id=thread, reply_parameters=reply, caption=head
            )
        )
        return
    for i, part in enumerate(parts):
        await say(part, as_reply=(i == 0))
