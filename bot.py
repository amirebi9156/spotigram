"""
Spotigram: Spotify playlists -> Telegram topics, new-release monitor, lyrics.

Usage:
    python bot.py                    # run everything
    python bot.py info <link|id>     # check a playlist link (used by install.sh)
    python bot.py artists            # interactive artist manager (used by install.sh)
"""
from __future__ import annotations

import asyncio
import re
import sys

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats, CallbackQuery,
    InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

import lyrics
import playlists
import releases
import ui
from core import ALLOWED_CB, ALLOWED_MSG, BOT_TOKEN, CHAT_ID, can_use, log

router = Router()

HELP = (
    "📝 Lyrics are sent to you in private (the group stays clean):\n"
    "• reply to a song with /lyrics (or the word: lyrics / متن / تکست)\n"
    "• or /lyrics Artist - Title\n"
    "• or open me in private and send a song name or the music file"
)
TRIGGERS = {"lyrics", "lyric", "text", "متن", "تکست", "متنش", "متن آهنگ"}


async def bot_link(bot: Bot, start: str = "lyrics") -> str:
    return f"https://t.me/{(await bot.me()).username}?start={start}"


async def send_lyrics_private(bot: Bot, user_id: int, artist: str, title: str, duration: int = 0,
                              group_msg: Message | None = None) -> bool:
    """Lyrics go to the user's private chat. If they never started the bot, tell them how (in the group)."""
    try:
        await lyrics.deliver(bot, None, artist, title, duration, dest=user_id)
        return True
    except TelegramForbiddenError:
        if group_msg is not None:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📩 Open the bot", url=await bot_link(bot))]])
            await group_msg.reply("Press Start in my private chat once, then ask again. Lyrics are sent there.", reply_markup=kb)
        return False


async def lyrics_for_audio(bot: Bot, msg: Message, user_id: int, group_msg: Message | None = None) -> None:
    a = msg.audio
    if not a:
        return
    artist, title = a.performer or "", a.title or ""
    if not title:  # no tags: fall back to the file name
        title = re.sub(r"\.\w{2,4}$", "", a.file_name or "").replace("_", " ").strip()
        if " - " in title and not artist:
            artist, title = [x.strip() for x in title.split(" - ", 1)]
    if not (artist or title):
        txt = "That file has no title/artist. Send the song name instead."
        await (group_msg.reply(txt) if group_msg else bot.send_message(user_id, txt))
        return
    await send_lyrics_private(bot, user_id, artist, title, a.duration or 0, group_msg)


# buttons under songs sent by older versions still work: lyrics go to the user's private chat
@router.callback_query(F.data == "lyr", ALLOWED_CB)
async def on_lyrics_button(cb: CallbackQuery, bot: Bot) -> None:
    if not cb.message or not getattr(cb.message, "audio", None):
        await cb.answer("No song here")
        return
    if not await can_use(bot, cb.message.chat, cb.from_user.id):
        await cb.answer("Join the music group first.", show_alert=True)
        return
    a = cb.message.audio
    try:
        await lyrics.deliver(bot, None, a.performer or "", a.title or "", a.duration or 0, dest=cb.from_user.id)
        await cb.answer("📩 Sent to you in private")
    except TelegramForbiddenError:
        await cb.answer("Open the bot in private and press Start first, then try again.", show_alert=True)


@router.message(Command("lyrics", "l", "text", "matn"), ALLOWED_MSG)
async def on_lyrics_command(message: Message, command: CommandObject, bot: Bot) -> None:
    uid = message.from_user.id
    if not await can_use(bot, message.chat, uid):
        return
    in_group = message.chat.type != "private"
    gm = message if in_group else None
    target = message.reply_to_message
    if target and target.audio:
        await lyrics_for_audio(bot, target, uid, gm)
    elif command.args:
        parts = re.split(r"\s+[-–—]\s+", command.args.strip(), maxsplit=1)
        artist, title = (parts[0], parts[1]) if len(parts) == 2 else ("", parts[0])
        await send_lyrics_private(bot, uid, artist, title, 0, gm)
    elif in_group:
        await message.reply(HELP)
    else:
        await ui.ask_lyrics(bot, message)


@router.message(ALLOWED_MSG, F.reply_to_message.audio, F.text.func(lambda t: t.strip().lower() in TRIGGERS))
async def on_reply_word(message: Message, bot: Bot) -> None:
    if await can_use(bot, message.chat, message.from_user.id):
        gm = message if message.chat.type != "private" else None
        await lyrics_for_audio(bot, message.reply_to_message, message.from_user.id, gm)


@router.message(F.chat.type == "private", F.audio)
async def on_private_audio(message: Message, bot: Bot) -> None:
    """Send me a music file in private and I find its lyrics."""
    if not await can_use(bot, message.chat, message.from_user.id):
        return
    await lyrics_for_audio(bot, message, message.from_user.id)


async def main() -> None:
    if not BOT_TOKEN or not CHAT_ID:
        sys.exit("BOT_TOKEN and CHAT_ID must be set in .env")
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)  # lyrics first, then the menu
    dp.include_router(ui.router)
    try:
        cmds = [
            BotCommand(command="menu", description="Open the menu"),
            BotCommand(command="search", description="Search a song or artist"),
            BotCommand(command="playlists", description="Show / add playlists"),
            BotCommand(command="addplaylist", description="Add a Spotify playlist"),
            BotCommand(command="artists", description="Artists I follow"),
            BotCommand(command="status", description="Bot health and failed songs"),
            BotCommand(command="lyrics", description="Find lyrics (sent to you privately)"),
        ]
        await bot.set_my_commands(cmds, scope=BotCommandScopeAllGroupChats())
        await bot.set_my_commands(cmds, scope=BotCommandScopeAllPrivateChats())
    except Exception as e:
        log.warning("could not set bot commands: %s", e)
    try:
        await asyncio.gather(
            playlists.sync_loop(bot),
            releases.release_loop(bot),
            # handle_signals=False: let SIGTERM from systemd stop the whole process
            dp.start_polling(bot, allowed_updates=["message", "callback_query"], handle_signals=False),
        )
    finally:
        await bot.session.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "info" and len(sys.argv) > 2:
        sys.exit(asyncio.run(playlists.info(sys.argv[2])))
    elif cmd == "artists":
        releases.artists_cli()
    else:
        asyncio.run(main())
