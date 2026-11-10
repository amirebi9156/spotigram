"""
Telegram menu for Spotigram: playlists, artists, search, add songs, follow artists.
Works both inside the group (commands/buttons) and in a private chat with the bot
(for members of the group). Management actions need a group admin.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import math
import secrets
import shutil
import tempfile
import time

import aiohttp
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BufferedInputFile, CallbackQuery, ForceReply, FSInputFile, InlineKeyboardButton,
    InlineKeyboardMarkup, Message,
)

import music
import playlists
import releases
from core import (
    ALLOWED_CB, ALLOWED_MSG, CHAT_ID, PICKS_TOPIC_NAME, db, dl_lock, fetch_image, is_admin,
    can_use, log, lyrics_kb, send_audio_topic, tg_call,
)

router = Router()

PER_PAGE = 8
MAX_ADD_ALL = 30
PROMPT_PLAYLIST = "➕ Send a public Spotify playlist link (reply to this message)"
PROMPT_SEARCH = "🔎 Send a song or artist name (reply to this message)"
PROMPTS = {PROMPT_PLAYLIST, PROMPT_SEARCH}

BG: set[asyncio.Task] = set()  # keeps background tasks alive until they finish
_adding: set[str] = set()  # song keys currently being added (avoid double sends)
PENDING: dict[str, dict] = {}  # follow dialogs: token -> state
_bot_id: int | None = None


# ----------------------------------------------------------------- helpers
def esc(s: str) -> str:
    return html.escape(s or "", quote=False)


def cut(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def mmss(sec: int) -> str:
    return f"{sec // 60}:{sec % 60:02d}"


def B(text: str, data: str | None = None, url: str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data, url=url)


def KB(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def topic_link(topic_id: int | None) -> str | None:
    s = str(CHAT_ID)
    return f"https://t.me/c/{s[4:]}/{topic_id}" if topic_id and s.startswith("-100") else None


def _thread(m: Message) -> int | None:
    return m.message_thread_id if m.is_topic_message else None


async def say(bot: Bot, m: Message, text: str, markup=None) -> Message:
    return await tg_call(
        lambda: bot.send_message(
            m.chat.id, text, message_thread_id=_thread(m), reply_markup=markup,
            parse_mode="HTML", disable_web_page_preview=True,
        )
    )


async def edit(cb: CallbackQuery, text: str, markup=None) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            raise


def spawn(coro) -> None:
    t = asyncio.create_task(coro)
    BG.add(t)
    t.add_done_callback(BG.discard)


async def my_id(bot: Bot) -> int:
    global _bot_id
    if _bot_id is None:
        _bot_id = (await bot.me()).id
    return _bot_id


def followed(name: str) -> bool:
    n = releases._norm(name)
    return any(releases._norm(a.name) == n for a in releases.read_artists())


def nav(prefix: str, page: int, pages: int) -> list[InlineKeyboardButton]:
    row = []
    if page > 0:
        row.append(B("◀", f"{prefix}:{page - 1}"))
    row.append(B(f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        row.append(B("▶", f"{prefix}:{page + 1}"))
    return row


# keeps the last tracks we showed so a button press needs no extra API call
TRK: dict[int, dict] = {}


def remember(tracks: list[dict]) -> None:
    if len(TRK) > 3000:
        TRK.clear()
    for t in tracks:
        TRK[t["id"]] = t


# ----------------------------------------------------------------- screens
def main_menu() -> tuple[str, InlineKeyboardMarkup]:
    return (
        "🎧 <b>Spotigram</b>\nWhat do you want to do?",
        KB([
            [B("🎵 Playlists", "m:pl"), B("🎤 Artists", "m:ar")],
            [B("🔎 Search songs / artists", "m:se")],
            [B("📝 Lyrics help", "m:ly"), B("ℹ️ Status", "m:st")],
        ]),
    )


def playlists_screen(admin: bool) -> tuple[str, InlineKeyboardMarkup]:
    rows = playlists.playlist_overview()
    lines = ["🎵 <b>Playlists</b> (each one is a topic in the group)"]
    if not rows:
        lines.append("\nNo playlists yet.")
    for r in rows:
        link = topic_link(r["topic_id"])
        name = esc(r["name"])
        lines.append(f"• {f'<a href=\"{link}\">{name}</a>' if link else name} — {r['tracks']} songs")
    kb_rows = [[B("➕ Add playlist", "pl:add")]]
    if admin:
        for r in rows:
            kb_rows.append([B(f"❌ Remove: {cut(r['name'], 30)}", f"pl:rm:{r['id']}")])
    kb_rows.append([B("↩ Menu", "m:main")])
    return "\n".join(lines), KB(kb_rows)


def artists_screen(admin: bool) -> tuple[str, InlineKeyboardMarkup]:
    arts = releases.read_artists()
    lines = ["🎤 <b>Artists I watch for new releases</b>\n(new songs go to the 🆕 New Releases topic)"]
    if not arts:
        lines.append("\nNo artists yet.")
    for a in arts:
        lines.append(f"• {esc(a.name)} — YouTube {len(a.yt)} · SoundCloud {len(a.sc)}")
    kb_rows = [[B("➕ Find & follow an artist", "m:se")]]
    if admin:
        for a in arts:
            kb_rows.append([B(f"❌ Unfollow: {cut(a.name, 30)}", f"ar:rm:{releases.artist_hash(a.name)}")])
    kb_rows.append([B("↩ Menu", "m:main")])
    return "\n".join(lines), KB(kb_rows)


def help_text() -> str:
    return (
        "📝 <b>Lyrics</b>: tap 📝 under a song, reply to it with /lyrics (or the word متن / lyrics), "
        "or /lyrics Artist - Title\n\n"
        "<b>Commands</b>\n/menu · /search name · /playlists · /addplaylist link · /artists\n"
        "In a private chat with me you can just send an artist or song name, or a Spotify playlist link."
    )


# ------------------------------------------------------------------ search
async def do_search(bot: Bot, m: Message, query: str) -> None:
    arts, trks = await asyncio.gather(music.search_artists(query, 4), music.search_tracks(query, 8))
    if not arts and not trks:
        await say(bot, m, f"Nothing found for <b>{esc(query)}</b>.", KB([[B("↩ Menu", "m:main")]]))
        return
    remember(trks)
    rows = [[B(f"🎤 {cut(a['name'], 34)} · {a['fans']:,} fans", f"a:{a['id']}")] for a in arts]
    rows += [
        [B(f"➕ {cut(t['artist'] + ' — ' + t['title'], 44)} ({mmss(t['duration'])})", f"t:{t['id']}")]
        for t in trks
    ]
    rows.append([B("↩ Menu", "m:main")])
    await say(
        bot, m,
        f"🔎 <b>{esc(query)}</b>\nTap an artist to browse songs / follow, or a song to add it to the group.",
        KB(rows),
    )


def artist_screen(a: dict) -> tuple[str, InlineKeyboardMarkup]:
    text = f"🎤 <b>{esc(a['name'])}</b>\n{a['fans']:,} fans · {a['albums']} albums"
    rows = [
        [B("✅ Following (new releases)" if followed(a["name"]) else "⭐ Follow (auto-send new releases)",
           "noop" if followed(a["name"]) else f"f:{a['id']}")],
        [B("🎵 Top songs", f"at:{a['id']}:0"), B("💿 Albums", f"ab:{a['id']}:0")],
        [B("↩ Menu", "m:main")],
    ]
    return text, KB(rows)


def songs_page(title: str, tracks: list[dict], page: int, prefix: str, kind: str, ident: int,
               back: list[InlineKeyboardButton]) -> tuple[str, InlineKeyboardMarkup]:
    pages = max(1, math.ceil(len(tracks) / PER_PAGE))
    page = min(max(page, 0), pages - 1)
    chunk = tracks[page * PER_PAGE:(page + 1) * PER_PAGE]
    remember(chunk)
    rows = [[B(f"➕ {cut(t['title'], 40)} ({mmss(t['duration'])})", f"t:{t['id']}")] for t in chunk]
    if pages > 1:
        rows.append(nav(prefix, page, pages))
    rows.append([B("➕ All on this page", f"ap:{kind}:{ident}:{page}")])
    if kind == "al":
        rows.append([B(f"➕ Whole album ({len(tracks)})", f"ap:{kind}:{ident}:-1")])
    rows.append(back + [B("↩ Menu", "m:main")])
    return f"{title}\nTap a song to add it to the group.", KB(rows)


# ------------------------------------------------------------ adding songs
async def add_song(bot: Bot, tr: dict) -> tuple[str, str]:
    """Returns ('ok'|'dup'|'fail', detail)."""
    key = releases.release_key(tr["artist"], tr["title"])
    if db.execute("SELECT 1 FROM picks WHERE key=?", (key,)).fetchone():
        return "dup", PICKS_TOPIC_NAME
    if db.execute("SELECT 1 FROM releases WHERE key=?", (key,)).fetchone():
        return "dup", releases.RELEASES_TOPIC_NAME
    if key in _adding:
        return "dup", "already being added"
    _adding.add(key)
    try:
        cached = db.execute("SELECT file_id FROM files WHERE track_id=?", (f"dz:{tr['id']}",)).fetchone()
        if cached:
            msg = await send_audio_topic(bot, "picks_topic", PICKS_TOPIC_NAME, 0xFFD67E, cached[0],
                                         reply_markup=lyrics_kb())
        else:
            tmp = tempfile.mkdtemp(prefix="spotigram_pick_")
            try:
                track = {"artist": tr["artist"], "title": tr["title"], "duration": tr["duration"]}
                async with dl_lock():
                    path = await asyncio.to_thread(playlists.download_audio, track, tmp)
                img = None
                if tr.get("cover"):
                    async with aiohttp.ClientSession() as session:
                        img = await fetch_image(session, tr["cover"], max_bytes=200_000)
                msg = await send_audio_topic(
                    bot, "picks_topic", PICKS_TOPIC_NAME, 0xFFD67E, FSInputFile(path),
                    title=tr["title"], performer=tr["artist"], duration=tr["duration"] or None,
                    thumbnail=BufferedInputFile(img, "cover.jpg") if img else None,
                    reply_markup=lyrics_kb(),
                )
                db.execute("INSERT OR REPLACE INTO files VALUES (?, ?)", (f"dz:{tr['id']}", msg.audio.file_id))
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        db.execute(
            "INSERT OR REPLACE INTO picks (key, dz_id, artist, title) VALUES (?,?,?,?)",
            (key, tr["id"], tr["artist"], tr["title"]),
        )
        db.commit()
        return "ok", ""
    except Exception as e:
        log.warning("add song failed: %s - %s: %s", tr["artist"], tr["title"], e)
        return "fail", cut(str(e), 120)
    finally:
        _adding.discard(key)


async def add_songs_task(bot: Bot, ctx: Message, tracks: list[dict]) -> None:
    ok = dup = fail = 0
    last = ("", "")
    for tr in tracks:
        status, detail = await add_song(bot, tr)
        ok, dup, fail = ok + (status == "ok"), dup + (status == "dup"), fail + (status == "fail")
        last = (status, detail)
        if len(tracks) > 1:
            await asyncio.sleep(1.5)
    where = "the group" if ctx.chat.id != CHAT_ID else "this group"
    if len(tracks) == 1:
        t = tracks[0]
        name = f"<b>{esc(t['artist'])} — {esc(t['title'])}</b>"
        if last[0] == "ok":
            text = f"✅ {name} → {esc(PICKS_TOPIC_NAME)} in {where}"
        elif last[0] == "dup":
            text = f"ℹ️ {name} is already in {esc(last[1])}"
        else:
            text = f"❌ Could not add {name}: {esc(last[1])}"
    else:
        text = f"✅ Added {ok} · already there {dup} · failed {fail} → {esc(PICKS_TOPIC_NAME)} in {where}"
    await say(bot, ctx, text)


# ---------------------------------------------------------------- playlists
async def add_playlist_from_text(bot: Bot, m: Message, text: str) -> None:
    pid = playlists.parse_playlist_id(text)
    if not pid:
        await say(bot, m, "That does not look like a Spotify playlist link.")
        return
    if pid in playlists.read_playlist_ids():
        await say(bot, m, "That playlist is already added.")
        return
    try:
        async with aiohttp.ClientSession() as session:
            data = await playlists.fetch_playlist(session, pid)
    except Exception as e:
        await say(bot, m, f"❌ Could not read it: {esc(str(e))}\nMake sure the playlist is <b>Public</b>.")
        return
    playlists.add_playlist_id(pid, data["name"])
    note = "\n⚠️ Only the first ~100 songs of a playlist can be read." if len(data["tracks"]) >= 100 else ""
    await say(bot, m, f"✅ Added <b>{esc(data['name'])}</b> ({len(data['tracks'])} songs). "
                      f"Sending it to its own topic now…{note}")

    async def run() -> None:
        async with aiohttp.ClientSession() as session:
            try:
                await playlists.sync_playlist(bot, pid, session)
            except Exception as e:
                log.error("sync after add failed for %s: %s", pid, e)
    spawn(run())


# ----------------------------------------------------------------- handlers
async def deny(m: Message, bot: Bot) -> None:
    await say(bot, m, "Only members of the music group can use me. Join the group first.")


async def need_admin(bot: Bot, uid: int) -> bool:
    return await is_admin(bot, uid)


@router.message(ALLOWED_MSG, Command("menu", "start", "help"))
async def cmd_menu(m: Message, bot: Bot) -> None:
    if not await can_use(bot, m.chat, m.from_user.id):
        return await deny(m, bot)
    text, kb = main_menu()
    await say(bot, m, text + "\n\n" + help_text(), kb)


@router.message(ALLOWED_MSG, Command("search", "s", "artist", "follow"))
async def cmd_search(m: Message, command: CommandObject, bot: Bot) -> None:
    if not await can_use(bot, m.chat, m.from_user.id):
        return await deny(m, bot)
    if command.args:
        await do_search(bot, m, command.args.strip())
    else:
        await tg_call(lambda: bot.send_message(m.chat.id, PROMPT_SEARCH, message_thread_id=_thread(m),
                                               reply_markup=ForceReply(selective=True),
                                               reply_parameters=None))


@router.message(ALLOWED_MSG, Command("playlists"))
async def cmd_playlists(m: Message, bot: Bot) -> None:
    if not await can_use(bot, m.chat, m.from_user.id):
        return await deny(m, bot)
    text, kb = playlists_screen(await need_admin(bot, m.from_user.id))
    await say(bot, m, text, kb)


@router.message(ALLOWED_MSG, Command("artists"))
async def cmd_artists(m: Message, bot: Bot) -> None:
    if not await can_use(bot, m.chat, m.from_user.id):
        return await deny(m, bot)
    text, kb = artists_screen(await need_admin(bot, m.from_user.id))
    await say(bot, m, text, kb)


@router.message(ALLOWED_MSG, Command("addplaylist", "add"))
async def cmd_addplaylist(m: Message, command: CommandObject, bot: Bot) -> None:
    if not await can_use(bot, m.chat, m.from_user.id):
        return await deny(m, bot)
    if not await need_admin(bot, m.from_user.id):
        return await say(bot, m, "Only group admins can add playlists.")
    if command.args:
        await add_playlist_from_text(bot, m, command.args)
    else:
        await tg_call(lambda: bot.send_message(m.chat.id, PROMPT_PLAYLIST, message_thread_id=_thread(m),
                                               reply_markup=ForceReply(selective=True)))


async def is_prompt_reply(m: Message, bot: Bot) -> bool:
    r = m.reply_to_message
    return bool(r and r.text in PROMPTS and r.from_user and r.from_user.id == await my_id(bot))


@router.message(ALLOWED_MSG, F.text, is_prompt_reply)
async def on_prompt_reply(m: Message, bot: Bot) -> None:
    if not await can_use(bot, m.chat, m.from_user.id):
        return await deny(m, bot)
    if m.reply_to_message.text == PROMPT_PLAYLIST:
        if not await need_admin(bot, m.from_user.id):
            return await say(bot, m, "Only group admins can add playlists.")
        await add_playlist_from_text(bot, m, m.text)
    else:
        await do_search(bot, m, m.text.strip())


@router.message(F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def on_private_text(m: Message, bot: Bot) -> None:
    """In a private chat: a playlist link adds the playlist, anything else is a search."""
    if not await can_use(bot, m.chat, m.from_user.id):
        return await deny(m, bot)
    if playlists.parse_playlist_id(m.text):
        if not await need_admin(bot, m.from_user.id):
            return await say(bot, m, "Only group admins can add playlists.")
        await add_playlist_from_text(bot, m, m.text)
    else:
        await do_search(bot, m, m.text.strip())


CB_PATTERN = r"^(m|pl|ar|a|at|ab|al|ap|t|f|fs|fo|fc|noop)(:|$)"


@router.callback_query(ALLOWED_CB, F.data.regexp(CB_PATTERN))
async def on_callback(cb: CallbackQuery, bot: Bot) -> None:
    msg = cb.message
    if msg is None:
        return await cb.answer()
    uid = cb.from_user.id
    if not await can_use(bot, msg.chat, uid):
        return await cb.answer("Join the music group first.", show_alert=True)
    p = (cb.data or "").split(":")
    cmd = p[0] if len(p) == 1 or p[0] not in ("pl", "ar") else f"{p[0]}:{p[1]}"
    ADMIN_ONLY = {"pl:add", "pl:rm", "ar:rm", "f", "fs", "fo", "ap"}
    if cmd in ADMIN_ONLY and not await need_admin(bot, uid):
        return await cb.answer("Only group admins can do that.", show_alert=True)

    if cmd == "noop":
        return await cb.answer()
    if cmd == "m":
        await cb.answer()
        what = p[1]
        if what == "main":
            text, kb = main_menu()
            return await edit(cb, text, kb)
        if what == "pl":
            text, kb = playlists_screen(await need_admin(bot, uid))
            return await edit(cb, text, kb)
        if what == "ar":
            text, kb = artists_screen(await need_admin(bot, uid))
            return await edit(cb, text, kb)
        if what == "ly":
            return await edit(cb, help_text(), KB([[B("↩ Menu", "m:main")]]))
        if what == "st":
            n = lambda q: db.execute(q).fetchone()[0]
            text = (
                "ℹ️ <b>Status</b>\n"
                f"Playlists: {len(playlists.read_playlist_ids())} · songs sent: {n('SELECT COUNT(*) FROM tracks')}\n"
                f"Artists watched: {len(releases.read_artists())} · new releases sent: {n('SELECT COUNT(*) FROM releases')}\n"
                f"Songs added by search: {n('SELECT COUNT(*) FROM picks')}"
            )
            return await edit(cb, text, KB([[B("↩ Menu", "m:main")]]))
        if what == "se":
            return await tg_call(lambda: bot.send_message(
                msg.chat.id, PROMPT_SEARCH, message_thread_id=_thread(msg), reply_markup=ForceReply()))
    if cmd == "pl:add":
        await cb.answer()
        return await tg_call(lambda: bot.send_message(
            msg.chat.id, PROMPT_PLAYLIST, message_thread_id=_thread(msg), reply_markup=ForceReply()))
    if cmd == "pl:rm":
        playlists.remove_playlist_id(p[2])
        await cb.answer("Removed. Songs already in Telegram stay.")
        text, kb = playlists_screen(True)
        return await edit(cb, text, kb)
    if cmd == "ar:rm":
        arts = [a for a in releases.read_artists() if releases.artist_hash(a.name) != p[2]]
        releases.write_artists(arts)
        await cb.answer("Unfollowed.")
        text, kb = artists_screen(True)
        return await edit(cb, text, kb)

    if cmd == "a":
        a = await music.artist(int(p[1]))
        if not a:
            return await cb.answer("Artist not found.", show_alert=True)
        await cb.answer()
        text, kb = artist_screen(a)
        return await edit(cb, text, kb)
    if cmd in ("at", "ab", "al"):
        await cb.answer()
        ident, page = int(p[1]), int(p[2])
        if cmd == "at":
            a, tracks = await music.artist(ident), await music.artist_top(ident)
            title = f"🎵 <b>{esc(a['name'] if a else '')}</b> — top songs"
            text, kb = songs_page(title, tracks, page, f"at:{ident}", "at", ident, [B("↩ Artist", f"a:{ident}")])
            return await edit(cb, text, kb)
        if cmd == "ab":
            a, albums = await music.artist(ident), await music.artist_albums(ident)
            pages = max(1, math.ceil(len(albums) / PER_PAGE))
            page = min(max(page, 0), pages - 1)
            rows = [[B(f"💿 {al['year']} · {cut(al['title'], 38)} ({al['type']})", f"al:{al['id']}:0")]
                    for al in albums[page * PER_PAGE:(page + 1) * PER_PAGE]]
            if pages > 1:
                rows.append(nav(f"ab:{ident}", page, pages))
            rows.append([B("↩ Artist", f"a:{ident}"), B("↩ Menu", "m:main")])
            return await edit(cb, f"💿 <b>{esc(a['name'] if a else '')}</b> — albums & singles", KB(rows))
        al = await music.album(ident)
        if not al:
            return await cb.answer("Album not found.", show_alert=True)
        title = f"💿 <b>{esc(al['title'])}</b> ({al['year']}) — {esc(al['artist'])}"
        back = [B("↩ Albums", f"ab:{al['artist_id']}:0")] if al.get("artist_id") else []
        text, kb = songs_page(title, al["tracks"], page, f"al:{ident}", "al", ident, back)
        return await edit(cb, text, kb)
    if cmd == "t":
        tr = TRK.get(int(p[1])) or await music.track(int(p[1]))
        if not tr:
            return await cb.answer("Song not found.", show_alert=True)
        await cb.answer("⏳ Adding… I'll tell you when it's in the group.")
        return spawn(add_songs_task(bot, msg, [tr]))
    if cmd == "ap":  # ap:<kind>:<id>:<page>   (page -1 = whole album)
        kind, ident, page = p[1], int(p[2]), int(p[3])
        if kind == "at":
            tracks = await music.artist_top(ident)
        else:
            al = await music.album(ident)
            tracks = al["tracks"] if al else []
        if page >= 0:
            tracks = tracks[page * PER_PAGE:(page + 1) * PER_PAGE]
        tracks = tracks[:MAX_ADD_ALL]
        if not tracks:
            return await cb.answer("Nothing to add.", show_alert=True)
        await cb.answer(f"⏳ Adding {len(tracks)} songs…")
        return spawn(add_songs_task(bot, msg, tracks))

    # ---- follow an artist: pick where to watch, then save
    if cmd == "f":
        a = await music.artist(int(p[1]))
        if not a:
            return await cb.answer("Artist not found.", show_alert=True)
        await cb.answer("🔎 Looking for channels…")
        await edit(cb, f"⏳ Looking for <b>{esc(a['name'])}</b> on YouTube and SoundCloud…")
        cands = await asyncio.to_thread(releases.follow_candidates, a["name"])
        for k in [k for k, v in PENDING.items() if time.time() - v["ts"] > 1800]:
            PENDING.pop(k, None)
        token = secrets.token_hex(3)
        PENDING[token] = {"name": a["name"], "cands": cands, "ts": time.time()}
        text, kb = follow_screen(token)
        return await edit(cb, text, kb)
    if cmd in ("fs", "fo", "fc"):
        st = PENDING.get(p[1])
        if not st:
            return await cb.answer("This dialog expired. Open the artist again.", show_alert=True)
        if cmd == "fc":
            PENDING.pop(p[1], None)
            await cb.answer("Cancelled.")
            text, kb = main_menu()
            return await edit(cb, text, kb)
        if cmd == "fs":
            c = st["cands"][int(p[2])]
            c["on"] = not c["on"]
            await cb.answer()
            text, kb = follow_screen(p[1])
            return await edit(cb, text, kb)
        picks = [(c["platform"], c["id"], c["label"]) for c in st["cands"] if c["on"]]
        if not picks:
            return await cb.answer("Select at least one source.", show_alert=True)
        releases.add_artist(releases.read_artists(), st["name"], picks, save=True)
        PENDING.pop(p[1], None)
        await cb.answer("Saved!")
        return await edit(
            cb,
            f"⭐ Following <b>{esc(st['name'])}</b> on {len(picks)} source(s).\n"
            f"New songs will appear in <b>{esc(releases.RELEASES_TOPIC_NAME)}</b> "
            "(songs that already exist are ignored; checks run every few minutes).",
            KB([[B("↩ Menu", "m:main")]]),
        )
    await cb.answer()


def follow_screen(token: str) -> tuple[str, InlineKeyboardMarkup]:
    st = PENDING[token]
    head = f"⭐ Follow <b>{esc(st['name'])}</b>\nWhere should I watch for new songs? Tap to toggle (✅ = watched), then Save."
    if not st["cands"]:
        return head + "\n\nI could not find channels automatically.", KB([[B("✖ Cancel", f"fc:{token}")]])
    rows = []
    for i, c in enumerate(st["cands"]):
        site = "YouTube" if c["platform"] == "yt" else "SoundCloud"
        rows.append([B(f"{'✅' if c['on'] else '⬜'} {site}: {cut(c['label'], 40)}", f"fs:{token}:{i}")])
    rows.append([B("💾 Save", f"fo:{token}"), B("✖ Cancel", f"fc:{token}")])
    return head, KB(rows)
