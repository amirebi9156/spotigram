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
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, BotCommandScopeAllGroupChats, CallbackQuery, Message

import lyrics
import playlists
import releases
from core import BOT_TOKEN, CHAT_ID, log

router = Router()

HELP = (
    "📝 Lyrics:\n"
    "• tap the 📝 Lyrics button under a song\n"
    "• or reply to a song with /lyrics (or the word: lyrics / متن / تکست)\n"
    "• or /lyrics Artist - Title"
)
TRIGGERS = {"lyrics", "lyric", "text", "متن", "تکست", "متنش", "متن آهنگ"}


async def lyrics_for_audio(bot: Bot, msg: Message) -> None:
    a = msg.audio
    if not a or not (a.title or a.performer):
        await bot.send_message(CHAT_ID, "That song has no title/artist tags. Use /lyrics Artist - Title",
                               message_thread_id=msg.message_thread_id if msg.is_topic_message else None)
        return
    await lyrics.deliver(bot, msg, a.performer or "", a.title or "", a.duration or 0)


@router.callback_query(F.data == "lyr", F.message.chat.id == CHAT_ID)
async def on_lyrics_button(cb: CallbackQuery, bot: Bot) -> None:
    if not cb.message or not getattr(cb.message, "audio", None):
        await cb.answer("No song here")
        return
    await cb.answer("🔎 Searching lyrics...")
    await lyrics_for_audio(bot, cb.message)


@router.message(Command("lyrics", "l", "text", "matn"), F.chat.id == CHAT_ID)
async def on_lyrics_command(message: Message, command: CommandObject, bot: Bot) -> None:
    target = message.reply_to_message
    if target and target.audio:
        await lyrics_for_audio(bot, target)
    elif command.args:
        parts = re.split(r"\s+[-–—]\s+", command.args.strip(), maxsplit=1)
        artist, title = (parts[0], parts[1]) if len(parts) == 2 else ("", parts[0])
        await lyrics.deliver(bot, message, artist, title)
    else:
        await message.reply(HELP)


@router.message(F.chat.id == CHAT_ID, F.reply_to_message.audio, F.text)
async def on_reply_word(message: Message, bot: Bot) -> None:
    if message.text.strip().lower() in TRIGGERS:
        await lyrics_for_audio(bot, message.reply_to_message)


async def main() -> None:
    if not BOT_TOKEN or not CHAT_ID:
        sys.exit("BOT_TOKEN and CHAT_ID must be set in .env")
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    try:
        await bot.set_my_commands(
            [BotCommand(command="lyrics", description="Lyrics of the song you reply to")],
            scope=BotCommandScopeAllGroupChats(),
        )
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
