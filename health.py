"""Failure tracking and admin alerts. Alerts go to group admins by DM (those who pressed /start
in the bot); if nobody can be reached, they are posted in the group instead."""
from __future__ import annotations

import time
from collections import deque

from aiogram import Bot

from core import CHAT_ID, log

START = time.time()
REPEAT_AFTER = 12 * 3600  # remind again if a problem lasts this long

_fails: dict[str, int] = {}
_alerted: dict[str, float] = {}
LAST_OK: dict[str, float] = {}  # "playlists" / "releases": time of last finished cycle
RECENT: deque = deque(maxlen=8)  # latest problems, for /status


def hint(err: str) -> str:
    e = (err or "").lower()
    if "not a bot" in e or "sign in to confirm" in e or "cookies" in e:
        return "YouTube is blocking this server. Fix: export cookies.txt from a logged-in browser and set YTDLP_COOKIES in settings."
    if "429" in e or "too many requests" in e:
        return "Rate limited (HTTP 429). It usually clears by itself; if it lasts, use cookies or a different IP."
    if "403" in e:
        return "Access denied (HTTP 403). Try: spotigram -> Update yt-dlp."
    if "404" in e or "not found" in e:
        return "Not found (HTTP 404). The source may be wrong or deleted."
    if "unable to extract" in e or "unsupported url" in e or "nsig" in e or "signature" in e:
        return "yt-dlp is probably outdated. Fix: spotigram -> Update yt-dlp."
    if "timed out" in e or "connection" in e or "network" in e:
        return "Network problem on the server."
    return ""


_admins: tuple[float, list[int]] = (0.0, [])


async def admin_ids(bot: Bot) -> list[int]:
    global _admins
    if time.time() - _admins[0] < 600 and _admins[1]:
        return _admins[1]
    try:
        ids = [m.user.id for m in await bot.get_chat_administrators(CHAT_ID) if not m.user.is_bot]
    except Exception as e:
        log.warning("could not list admins: %s", e)
        ids = _admins[1]
    _admins = (time.time(), ids)
    return ids


async def alert(bot: Bot, text: str) -> None:
    sent = 0
    for uid in await admin_ids(bot):
        try:
            await bot.send_message(uid, text, disable_web_page_preview=True)
            sent += 1
        except Exception:
            pass  # admin has not started the bot
    if not sent:
        try:
            await bot.send_message(CHAT_ID, text + "\n\n(Admins: send /start to me in private to get these by DM.)")
        except Exception as e:
            log.warning("alert could not be delivered: %s", e)


async def fail(bot: Bot, key: str, what: str, err: str = "", threshold: int = 3) -> bool:
    """Count a failure; alert when it happened `threshold` times in a row. Returns True if alerted now."""
    _fails[key] = _fails.get(key, 0) + 1
    if _fails[key] < threshold:
        return False
    last = _alerted.get(key)
    if last and time.time() - last < REPEAT_AFTER:
        return False
    _alerted[key] = time.time()
    h = hint(err)
    short = (err or "").strip().splitlines()[-1][:200] if err else ""
    RECENT.appendleft((time.time(), what))
    await alert(bot, f"⚠️ {what}\n({_fails[key]} failures in a row)" + (f"\n{short}" if short else "") + (f"\n\n💡 {h}" if h else ""))
    return True


async def ok(bot: Bot, key: str, what: str = "") -> None:
    n = _fails.pop(key, 0)
    if key in _alerted:
        _alerted.pop(key, None)
        if what:
            await alert(bot, f"✅ Recovered: {what}")
    return None


def failing() -> dict[str, int]:
    return {k: v for k, v in _fails.items() if v >= 2}


def mark_cycle(name: str) -> None:
    LAST_OK[name] = time.time()
