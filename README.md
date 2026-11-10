# Spotigram

Your music, in Telegram. One private supergroup with Topics, and a light bot (single process, no ffmpeg, SQLite state) that does three things:

1. **Playlists**: every Spotify playlist becomes a topic; its tracks are uploaded as audio and new tracks are picked up automatically. The playlist cover is posted (and pinned) at the top of its topic.
2. **New releases**: watches the artists you choose on **YouTube** and **SoundCloud** and posts each new song to one topic (`🆕 New Releases`) as soon as *any* platform shows it first. The copy that appears later on another platform is skipped.
3. **Lyrics**: tap **📝 Lyrics** under any song, or reply to a song with `/lyrics` (or the word `lyrics` / `متن` / `تکست`), or send `/lyrics Artist - Title`. Lyrics come from [LRCLIB](https://lrclib.net).

4. **Control from Telegram**: everything above can be managed from the group or from a private chat with the bot (see below).

No Spotify login, developer app or Premium is needed. Playlists are read from Spotify's public embed page, so each playlist just has to be **Public**.

## Using it from Telegram

Works in the group and in a private chat with the bot (DM only for members of the group). Send `/menu` for buttons.

| Command | What it does |
| --- | --- |
| `/menu` | Main menu |
| `/search name` | Search a song or artist (DM: just type the name) |
| `/playlists`, `/addplaylist link` | List / add playlists (DM: paste a Spotify link). Admins only for add/remove |
| `/artists` | Artists you follow for new releases |
| `/status` | Health, failed songs, connection check, **Retry failed** (admins) |
| `/lyrics` | Lyrics of the song you reply to |

Search shows matching artists and songs (metadata from Deezer's public API). Tap a song to add it to the `🎧 Picks` topic, or open an artist to see top songs and albums (add one song, a page, or a whole album), or tap **Follow** to pick their YouTube / SoundCloud sources; new songs then go to `🆕 New Releases` automatically. Adding playlists in bulk, following and removing are admin-only.

**Alerts**: when something keeps failing (YouTube blocking the server, 5 failed downloads in a row, a Spotify playlist that is no longer Public, a broken artist source) group admins get a message with a suggested fix, and another when it recovers. Admins must press /start in a private chat with the bot to receive them by DM; otherwise they are posted in the group.

**Persian / non-Latin names**: when Deezer has little or nothing, search also lists YouTube results directly (▶️) and offers to follow the typed name.

## One-line install

```bash
curl -fsSL -o /tmp/spotigram-install.sh https://raw.githubusercontent.com/amirebi9156/spotigram/main/install.sh && sudo bash /tmp/spotigram-install.sh
```

Afterwards open the management menu any time with `sudo spotigram`. It covers install, update, settings, playlists, artists, start/stop/restart, health check, live logs, statistics, retrying failed tracks, yt-dlp update and uninstall. Most actions also work as commands: `spotigram status`, `spotigram logs`, `spotigram playlists`, `spotigram artists`, `spotigram update`, ...

## Before you install

1. **Telegram**: create a bot with @BotFather. Create a **private** supergroup, turn **Topics** on, add the bot as admin with **Manage Topics** (and **Pin Messages** for pinned covers). You need the group id (starts with `-100`).
2. **Playlists**: make the playlists you want **Public** in the Spotify app, then copy each link (Share -> Copy link). Add them during install or later from the menu.
3. **Artists** (optional): in the menu choose *Manage artists*, type an artist name, and pick the YouTube channels and SoundCloud profile the bot found (or paste links). An `Artist - Topic` YouTube channel is the auto-generated audio channel and is usually the fastest source.
4. Run the server **outside Iran**.

## How it works

- **Playlists**: every cycle the bot reads each playlist's embed page and compares it with the database. New tracks are matched on YouTube by name and duration, downloaded as m4a with `yt-dlp` and sent into the playlist's topic. Telegram `file_id`s are reused when a track is in several playlists.
- **New releases**: YouTube channel RSS feeds and SoundCloud profiles are checked every 3 minutes. The first time an artist is added, existing uploads are only recorded, so nothing old is sent. A new upload is ignored if it looks like a teaser/snippet/live/interview, is shorter than 1 or longer than 12 minutes, or is older than 72 hours. Songs are identified by a normalized "artist + title", so `Artist - Song (Official Video)` and `Song` are the same release.
- **Lyrics**: the bot reads replies and button taps in your group only. Long lyrics are sent as a `.txt` file.
- Everything is stored in `/opt/spotigram` (`.env`, `playlists.txt`, `artists.txt`, `state.db`). The two lists are re-read every cycle, so edits need no restart.

## Limits

- Only **Public** playlists work, and Spotify's embed page exposes the **first 100 tracks** of a playlist. For longer ones, move already-delivered tracks to another playlist (the bot never deletes anything from Telegram) or split the playlist.
- This relies on unofficial page formats, so Spotify, YouTube or SoundCloud changes can break it. `spotigram update` and *Update yt-dlp* fix most cases.
- Release detection is as fast as the platforms' feeds (YouTube RSS can lag a few minutes). Spotify is not monitored.
- Lyrics coverage depends on the community database; some songs (especially non-English) may not be found.

## Config (`/opt/spotigram/.env`)

| Key | Meaning |
| --- | --- |
| `BOT_TOKEN` | Telegram bot token |
| `CHAT_ID` | Supergroup id |
| `SYNC_INTERVAL` | Seconds between playlist checks (default 600) |
| `RELEASE_INTERVAL` | Seconds between new-release checks (default 180) |
| `RELEASES_TOPIC_NAME` | Name of the new-releases topic |
| `PICKS_TOPIC_NAME` | Name of the topic for songs added from search |
| `RELEASE_MAX_AGE_HOURS`, `RELEASE_MIN_SECONDS`, `RELEASE_MAX_SECONDS` | Release filters |
| `YTDLP_COOKIES` | Optional cookies.txt path if YouTube blocks the server |

## Notes

For personal use only. Keep the group private, and respect the terms of the services you connect and copyright law in your country.
