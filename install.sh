#!/usr/bin/env bash
# Spotigram - installer & management menu
# One-line install:
#   bash <(curl -fsSL https://raw.githubusercontent.com/amirebi9156/spotigram/main/install.sh)
# After install, just run:  spotigram
set -u

APP_DIR="${SPOTIGRAM_DIR:-/opt/spotigram}"
REPO_URL="${SPOTIGRAM_REPO:-https://github.com/amirebi9156/spotigram.git}"
BRANCH="${SPOTIGRAM_BRANCH:-main}"
SERVICE="spotigram"
UNIT="/etc/systemd/system/${SERVICE}.service"
ENV_FILE="$APP_DIR/.env"
VENV="$APP_DIR/venv"
CLI="/usr/local/bin/spotigram"

RED=$'\e[31m'; GREEN=$'\e[32m'; YELLOW=$'\e[33m'; CYAN=$'\e[36m'; BOLD=$'\e[1m'; NC=$'\e[0m'
ok()   { echo -e "${GREEN}✔${NC} $*"; }
warn() { echo -e "${YELLOW}!${NC} $*"; }
err()  { echo -e "${RED}✘${NC} $*"; }
info() { echo -e "${CYAN}➜${NC} $*"; }

need_root() {
  if [ "${EUID:-$(id -u)}" -ne 0 ]; then
    err "Run as root:  sudo bash $0"
    exit 1
  fi
}

pause() { read -r -p "Press Enter to continue..." _ < /dev/tty; }

# yes_no "question" default(y|n)
yes_no() {
  local a def="${2:-n}" hint="[y/N]"
  [ "$def" = "y" ] && hint="[Y/n]"
  read -r -p "$1 $hint " a < /dev/tty
  a="${a:-$def}"
  [[ "${a,,}" == y* ]]
}

is_installed() { [ -d "$APP_DIR/.git" ] && [ -x "$VENV/bin/python" ]; }

# ------------------------------------------------------------------ .env
get_env() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -n1 | cut -d= -f2-; }

set_env() {
  python3 - "$ENV_FILE" "$1" "$2" <<'PY'
import os, sys
path, key, val = sys.argv[1:4]
lines = open(path).read().splitlines() if os.path.exists(path) else []
out, done = [], False
for l in lines:
    if l.startswith(key + "="):
        out.append(f"{key}={val}")
        done = True
    else:
        out.append(l)
if not done:
    out.append(f"{key}={val}")
open(path, "w").write("\n".join(out) + "\n")
PY
}

mask() {
  local s="$1"
  if [ -z "$s" ]; then echo "empty"
  elif [ "${#s}" -gt 8 ]; then echo "${s:0:4}...${s: -3}"
  else echo "set"; fi
}

configure() {
  [ -f "$ENV_FILE" ] || cp "$APP_DIR/.env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo
  echo -e "${BOLD}Settings${NC} (press Enter to keep the current value)"
  local pairs=(
    "BOT_TOKEN|Telegram bot token (from @BotFather)"
    "CHAT_ID|Telegram supergroup id (starts with -100)"
    "SPOTIPY_CLIENT_ID|Spotify client id"
    "SPOTIPY_CLIENT_SECRET|Spotify client secret"
  )
  local p key label cur shown val
  for p in "${pairs[@]}"; do
    key="${p%%|*}"; label="${p#*|}"
    cur="$(get_env "$key")"
    shown="$cur"
    case "$key" in *TOKEN|*SECRET) shown="$(mask "$cur")";; esac
    [ -z "$shown" ] && shown="empty"
    read -r -p "$label [$shown]: " val < /dev/tty
    [ -n "$val" ] && set_env "$key" "$val"
  done

  if yes_no "Edit advanced settings (sync interval, playlist filter, yt-dlp cookies)?" n; then
    local adv=(
      "SYNC_INTERVAL|Sync interval in seconds"
      "ONLY_PLAYLISTS|Only these playlist ids (comma separated, empty = all)"
      "YTDLP_COOKIES|Path to cookies.txt for yt-dlp (empty = none)"
    )
    for p in "${adv[@]}"; do
      key="${p%%|*}"; label="${p#*|}"
      cur="$(get_env "$key")"
      read -r -p "$label [${cur:-empty}]: " val < /dev/tty
      [ -n "$val" ] && set_env "$key" "$val"
    done
  fi
  ok "Settings saved to $ENV_FILE"
  if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
    yes_no "Restart the service to apply?" y && systemctl restart "$SERVICE" && ok "Restarted"
  fi
}

# ------------------------------------------------------------ install parts
install_deps() {
  info "Installing system packages..."
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq && apt-get install -y -qq python3 python3-venv python3-pip git curl ca-certificates || return 1
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y -q python3 python3-pip git curl || return 1
  elif command -v yum >/dev/null 2>&1; then
    yum install -y -q python3 python3-pip git curl || return 1
  else
    err "Unsupported distro (need apt, dnf or yum)"; return 1
  fi
  if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    err "Python 3.9+ is required (found $(python3 -V 2>&1))"; return 1
  fi
  ok "System packages ready"
}

fetch_code() {
  if [ -d "$APP_DIR/.git" ]; then
    info "Fetching latest code..."
    git -C "$APP_DIR" fetch --quiet origin "$BRANCH" && \
      git -C "$APP_DIR" reset --hard --quiet "origin/$BRANCH" || { err "git update failed"; return 1; }
  else
    mkdir -p "$APP_DIR"
    if [ -n "$(ls -A "$APP_DIR" 2>/dev/null)" ]; then
      err "$APP_DIR exists and is not a git checkout"; return 1
    fi
    info "Cloning $REPO_URL ..."
    git clone --quiet --branch "$BRANCH" "$REPO_URL" "$APP_DIR" || { err "git clone failed (is the repo public?)"; return 1; }
  fi
  chmod +x "$APP_DIR/install.sh"
  ln -sf "$APP_DIR/install.sh" "$CLI"
  ok "Code is up to date ($(git -C "$APP_DIR" log -1 --format=%h))"
}

setup_venv() {
  info "Setting up Python environment..."
  [ -d "$VENV" ] || python3 -m venv "$VENV" || { err "venv creation failed"; return 1; }
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q -r "$APP_DIR/requirements.txt" || { err "pip install failed"; return 1; }
  ok "Python dependencies installed"
}

write_service() {
  cat > "$UNIT" <<EOF
[Unit]
Description=Spotigram - Spotify playlists to Telegram topics
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$VENV/bin/python $APP_DIR/bot.py
Restart=always
RestartSec=10
Nice=10

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
}

spotify_login() {
  is_installed || { err "Install first"; return 1; }
  if [ -z "$(get_env SPOTIPY_CLIENT_ID)" ] || [ -z "$(get_env SPOTIPY_CLIENT_SECRET)" ]; then
    err "Spotify client id/secret missing (menu: Edit settings)"; return 1
  fi
  local redirect; redirect="$(get_env SPOTIPY_REDIRECT_URI)"
  echo
  echo -e "${BOLD}Spotify login${NC}"
  echo " 1) In your Spotify app settings, the Redirect URI must be exactly:"
  echo "      ${redirect:-http://127.0.0.1:8888/callback}"
  echo " 2) Open the link below in any browser and approve access."
  echo " 3) The final page will fail to load - that is expected."
  echo "    Copy the FULL URL from the address bar and paste it here."
  echo
  rm -f "$APP_DIR/.spotify_cache"
  if (cd "$APP_DIR" && "$VENV/bin/python" bot.py auth); then
    chmod 600 "$APP_DIR/.spotify_cache" 2>/dev/null
    ok "Spotify authorized"
  else
    err "Spotify login failed"; return 1
  fi
}

# ------------------------------------------------------------ telegram check
tg_api() { local m="$1"; shift; curl -fsS --max-time 15 "https://api.telegram.org/bot$(get_env BOT_TOKEN)/$m" "$@" 2>/dev/null; }

check_telegram() {
  local chat me mid c m uname rc
  chat="$(get_env CHAT_ID)"
  [ -n "$(get_env BOT_TOKEN)" ] || { err "Telegram: BOT_TOKEN is empty"; return 1; }
  me="$(tg_api getMe)" || { err "Telegram: token invalid or API unreachable"; return 1; }
  uname="$(echo "$me" | python3 -c 'import sys,json; print(json.load(sys.stdin)["result"]["username"])')"
  mid="$(echo "$me" | python3 -c 'import sys,json; print(json.load(sys.stdin)["result"]["id"])')"
  ok "Telegram: bot token valid (@$uname)"
  [ -n "$chat" ] || { err "Telegram: CHAT_ID is empty"; return 1; }
  c="$(tg_api getChat -d "chat_id=$chat")" || { err "Telegram: cannot access chat $chat (wrong id, or bot not added)"; return 1; }
  m="$(tg_api getChatMember -d "chat_id=$chat" -d "user_id=$mid")" || { err "Telegram: cannot read bot membership"; return 1; }
  python3 - "$c" "$m" <<'PY'
import json, sys
c = json.loads(sys.argv[1])["result"]
m = json.loads(sys.argv[2])["result"]
status = m.get("status")
topics = c.get("is_forum", False)
can = m.get("can_manage_topics", status == "creator")
print(f"  chat: {c.get('title')} | type: {c.get('type')} | topics enabled: {topics}")
print(f"  bot status: {status} | can manage topics: {can}")
bad = (c.get("type") != "supergroup" or not topics
       or status not in ("administrator", "creator")
       or (status == "administrator" and not can))
sys.exit(1 if bad else 0)
PY
  rc=$?
  if [ $rc -eq 0 ]; then ok "Telegram: group is ready"
  else err "Telegram: group must be a supergroup with Topics ON, and the bot must be admin with 'Manage Topics'"; fi
  return $rc
}

# ------------------------------------------------------------------ actions
do_install() {
  install_deps || return 1
  fetch_code   || return 1
  setup_venv   || return 1
  configure
  write_service
  if [ ! -f "$APP_DIR/.spotify_cache" ]; then spotify_login || warn "Run 'Spotify login' from the menu later"; fi
  systemctl enable --now "$SERVICE" >/dev/null 2>&1
  sleep 2
  health_check
  echo
  ok "Done. Manage anytime with:  ${BOLD}spotigram${NC}"
}

do_update() {
  is_installed || { err "Not installed"; return 1; }
  fetch_code || return 1
  setup_venv || return 1
  write_service
  systemctl restart "$SERVICE" && ok "Updated and restarted"
}

update_ytdlp() {
  is_installed || { err "Not installed"; return 1; }
  info "Updating yt-dlp..."
  "$VENV/bin/pip" install -q -U yt-dlp && ok "yt-dlp $("$VENV/bin/python" -c 'import yt_dlp.version as v; print(v.__version__)')"
  systemctl restart "$SERVICE" 2>/dev/null && ok "Service restarted"
}

svc() {
  is_installed || { err "Not installed"; return 1; }
  systemctl "$1" "$SERVICE" && ok "$1 done"
}

follow_logs() {
  echo "Live logs (Ctrl+C to go back)"
  trap : INT
  journalctl -u "$SERVICE" -n 40 -f --no-pager
  trap - INT
}

show_stats() {
  [ -f "$APP_DIR/state.db" ] || { warn "No database yet (service has not run)"; return; }
  python3 - "$APP_DIR/state.db" <<'PY'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
q = lambda s: db.execute(s).fetchall()
total = q("SELECT COUNT(*) FROM playlists")[0][0]
synced = q("SELECT COUNT(*) FROM playlists WHERE snapshot_id IS NOT NULL")[0][0]
print(f"Playlists fully synced : {synced} / {total}")
print(f"Tracks sent            : {q('SELECT COUNT(*) FROM tracks')[0][0]}")
print(f"Unique files cached    : {q('SELECT COUNT(*) FROM files')[0][0]}")
print(f"Failed, will retry     : {q('SELECT COUNT(*) FROM failed WHERE attempts < 3')[0][0]}")
print(f"Failed, gave up        : {q('SELECT COUNT(*) FROM failed WHERE attempts >= 3')[0][0]}")
print()
rows = q("""SELECT p.name, COUNT(t.track_id) FROM playlists p
            LEFT JOIN tracks t ON t.playlist_id = p.playlist_id
            GROUP BY p.playlist_id ORDER BY 2 DESC""")
for name, n in rows:
    print(f"  {n:5d}  {name}")
PY
}

retry_failed() {
  [ -f "$APP_DIR/state.db" ] || { warn "No database yet"; return; }
  python3 - "$APP_DIR/state.db" <<'PY'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute("DELETE FROM failed")
db.execute("UPDATE playlists SET snapshot_id = NULL")
db.commit()
PY
  ok "Failed list cleared; all playlists will be re-checked on the next cycle"
  systemctl restart "$SERVICE" 2>/dev/null && ok "Service restarted"
}

health_check() {
  echo -e "\n${BOLD}== Health check ==${NC}"
  if is_installed; then
    ok "Code: $APP_DIR ($(git -C "$APP_DIR" log -1 --format='%h, %cd' --date=short 2>/dev/null))"
  else
    err "Not installed in $APP_DIR"; return 1
  fi

  local p v
  for p in aiogram spotipy yt-dlp; do
    v="$("$VENV/bin/python" -c "import importlib.metadata as m; print(m.version('$p'))" 2>/dev/null)" \
      && ok "$p $v" || err "$p is missing (menu: Update)"
  done

  local k
  for k in BOT_TOKEN CHAT_ID SPOTIPY_CLIENT_ID SPOTIPY_CLIENT_SECRET; do
    if [ -n "$(get_env "$k")" ]; then ok "$k is set"; else err "$k is empty (menu: Edit settings)"; fi
  done

  if systemctl is-active --quiet "$SERVICE"; then
    ok "Service running since $(systemctl show "$SERVICE" -p ActiveEnterTimestamp --value)"
    local mem; mem="$(systemctl show "$SERVICE" -p MemoryCurrent --value)"
    if [[ "$mem" =~ ^[0-9]+$ ]]; then ok "Memory usage: $((mem / 1024 / 1024)) MB"; fi
  else
    err "Service is not running (menu: Start)"
  fi
  if systemctl is-enabled --quiet "$SERVICE" 2>/dev/null; then ok "Autostart on boot: enabled"; else warn "Autostart on boot: disabled"; fi

  local u code
  for u in "https://api.telegram.org" "https://accounts.spotify.com" "https://www.youtube.com"; do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$u")"
    if [ "$code" != "000" ]; then ok "Reachable: $u (HTTP $code)"; else err "Unreachable: $u"; fi
  done

  check_telegram

  if [ -f "$APP_DIR/.spotify_cache" ]; then
    local out
    out="$(cd "$APP_DIR" && timeout 40 "$VENV/bin/python" bot.py auth </dev/null 2>&1)"
    if echo "$out" | grep -q "logged in as"; then
      ok "Spotify: $(echo "$out" | grep 'logged in as' | sed 's/^OK, //')"
    else
      err "Spotify token invalid (menu: Spotify login)"
    fi
  else
    err "Spotify not authorized yet (menu: Spotify login)"
  fi

  ok "Disk: $(df -h "$APP_DIR" | awk 'NR==2 {print $4 " free of " $2}')"

  local issues
  issues="$(journalctl -u "$SERVICE" -n 300 --no-pager 2>/dev/null | grep -E ' (ERROR|WARNING) ' | tail -n 5)"
  if [ -n "$issues" ]; then
    warn "Recent warnings/errors:"
    echo "$issues" | sed 's/^/    /'
  else
    ok "No recent warnings or errors in logs"
  fi
}

do_uninstall() {
  yes_no "Remove the Spotigram service?" n || return 0
  systemctl disable --now "$SERVICE" >/dev/null 2>&1
  rm -f "$UNIT" "$CLI"
  systemctl daemon-reload
  ok "Service removed"
  if yes_no "Also delete $APP_DIR (settings, Spotify login and database)?" n; then
    rm -rf "$APP_DIR"
    ok "Files deleted"
  else
    info "Files kept in $APP_DIR"
  fi
}

# --------------------------------------------------------------------- menu
svc_state() {
  if [ ! -f "$UNIT" ]; then echo "${RED}not installed${NC}"
  elif systemctl is-active --quiet "$SERVICE"; then echo "${GREEN}running${NC}"
  else echo "${YELLOW}stopped${NC}"; fi
}

menu_loop() {
  local c
  if ! is_installed; then
    echo -e "${BOLD}Spotigram is not installed.${NC}"
    yes_no "Install now?" y && { do_install; pause; }
  fi
  while true; do
    clear
    echo -e "${BOLD}${CYAN}Spotigram${NC} - Spotify playlists -> Telegram topics"
    echo -e "Status: $(svc_state)"
    echo
    echo " 1) Install / reinstall"
    echo " 2) Update from GitHub"
    echo " 3) Edit settings"
    echo " 4) Spotify login"
    echo " 5) Start"
    echo " 6) Stop"
    echo " 7) Restart"
    echo " 8) Health check"
    echo " 9) Live logs"
    echo "10) Statistics"
    echo "11) Retry failed tracks / force resync"
    echo "12) Update yt-dlp"
    echo "13) Uninstall"
    echo " 0) Exit"
    echo
    read -r -p "Choose: " c < /dev/tty
    echo
    case "$c" in
      1)  do_install; pause;;
      2)  do_update; pause;;
      3)  is_installed && configure || err "Install first"; pause;;
      4)  spotify_login; pause;;
      5)  svc start; pause;;
      6)  svc stop; pause;;
      7)  svc restart; pause;;
      8)  health_check; pause;;
      9)  follow_logs;;
      10) show_stats; pause;;
      11) retry_failed; pause;;
      12) update_ytdlp; pause;;
      13) do_uninstall; pause;;
      0|q) exit 0;;
      *)  warn "Invalid choice"; sleep 1;;
    esac
  done
}

main() {
  need_root
  case "${1:-menu}" in
    install)  do_install;;
    update)   do_update;;
    status|health) health_check;;
    logs)     follow_logs;;
    stats)    show_stats;;
    start|stop|restart) svc "$1";;
    login)    spotify_login;;
    uninstall) do_uninstall;;
    menu|*)   menu_loop;;
  esac
}

main "$@"
