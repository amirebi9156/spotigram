# Spotigram

Mirror your Spotify playlists into a Telegram group: one topic per playlist, every track uploaded as audio, new tracks picked up automatically. Built to be light enough for a small VPS (single process, no ffmpeg, SQLite state).

**No Spotify account login, developer app or Premium is needed.** Playlists are read from Spotify's public embed page, so each playlist just has to be **Public**.

## One-line install

```bash
curl -fsSL -o /tmp/spotigram-install.sh https://raw.githubusercontent.com/amirebi9156/spotigram/main/install.sh && sudo bash /tmp/spotigram-install.sh
```

After installing, open the management menu any time with:

```bash
sudo spotigram
```

The menu covers install, update, settings, playlist management, start/stop/restart, health check, live logs, statistics, retrying failed tracks, yt-dlp update and uninstall. Most actions also work as a command: `spotigram status`, `spotigram logs`, `spotigram playlists`, `spotigram update`, ...

## Before you install

1. **Telegram**: create a bot with @BotFather. Create a supergroup, turn **Topics** on, add the bot as admin with **Manage Topics**. You need the group id (starts with `-100`).
2. **Spotify**: in the Spotify app make the playlists you want **Public** (playlist menu -> *Add to profile* / *Make public*), then copy each playlist link (Share -> Copy link). You add the links during install or later from the menu.
3. Run the server **outside Iran** (Spotify, YouTube and Telegram must be reachable).

## How it works

- Every sync cycle the bot downloads each playlist's embed page (`open.spotify.com/embed/playlist/<id>`) and reads the track list from the JSON inside it. New tracks are detected by comparing against the database.
- Each track is matched on YouTube by name and duration, downloaded as m4a with `yt-dlp`, and sent with `sendAudio` into the playlist's topic.
- SQLite keeps `playlist -> topic`, `track -> message` and Telegram `file_id`s, so nothing is sent twice and shared tracks upload only once.
- Playlist links live in `/opt/spotigram/playlists.txt` and are re-read every cycle, so adding one needs no restart.

## Limits

- Only **Public** playlists work.
- Spotify's embed page exposes about 100 tracks per playlist; longer playlists may be incomplete.
- This relies on an unofficial page format, so Spotify can change it. If playlists stop loading, run `spotigram update`.

## Config (`/opt/spotigram/.env`)

| Key | Meaning |
| --- | --- |
| `BOT_TOKEN` | Telegram bot token |
| `CHAT_ID` | Supergroup id |
| `SYNC_INTERVAL` | Seconds between sync cycles (default 600) |
| `YTDLP_COOKIES` | Optional cookies.txt path if YouTube blocks the server |

## Notes

For personal use only. Respect the terms of the services you connect and copyright law in your country.
