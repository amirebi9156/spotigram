# Spotigram

Mirror your Spotify playlists into a Telegram group: one topic per playlist, every track uploaded as audio, new tracks picked up automatically. Built to be light enough for a small VPS (single process, no ffmpeg, SQLite state).

## One-line install

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/amirebi9156/spotigram/main/install.sh)
```

After installing, open the management menu any time with:

```bash
sudo spotigram
```

The menu covers install, update, settings, Spotify login, start/stop/restart, health check, live logs, statistics, retrying failed tracks, yt-dlp update and uninstall. Most actions also work as a command: `spotigram status`, `spotigram logs`, `spotigram update`, ...

## Before you install

1. **Telegram**: create a bot with @BotFather. Create a supergroup, turn **Topics** on, add the bot as admin with **Manage Topics**. You need the group id (starts with `-100`).
2. **Spotify**: create an app at https://developer.spotify.com/dashboard and add this Redirect URI exactly: `http://127.0.0.1:8888/callback`. You need the client id and secret.
3. Run the server **outside Iran** (Spotify, YouTube and Telegram APIs must be reachable).

## How it works

- Spotify API (`spotipy`) lists your playlists; `snapshot_id` tells the bot when a playlist changed.
- Each track is matched on YouTube by name and duration, downloaded as m4a with `yt-dlp`, and sent with `sendAudio` into the playlist's topic.
- SQLite keeps `playlist -> topic`, `track -> message` and Telegram `file_id`s, so nothing is sent twice and shared tracks upload only once.

## Config (`/opt/spotigram/.env`)

| Key | Meaning |
| --- | --- |
| `BOT_TOKEN` | Telegram bot token |
| `CHAT_ID` | Supergroup id |
| `SPOTIPY_CLIENT_ID` / `SPOTIPY_CLIENT_SECRET` | Spotify app credentials |
| `SYNC_INTERVAL` | Seconds between sync cycles (default 600) |
| `ONLY_PLAYLISTS` | Comma-separated playlist ids to limit syncing |
| `YTDLP_COOKIES` | Optional cookies.txt path if YouTube blocks the server |

## Notes

For personal use only. Respect the terms of the services you connect and copyright law in your country.
