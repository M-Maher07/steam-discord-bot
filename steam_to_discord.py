"""
Steam → Discord Notifier (Python) — Replit friendly (single file)

What it does
- Polls Steam Web API for a specific friend's status using their SteamID64.
- When they transition to Online or start Playing a game, it pings you in a Discord channel.
- Supports either a Discord Webhook (simple) or a proper Discord Bot (BOT_MODE=true).

Replit & Keep-Alive
- Set KEEPALIVE=true to run a tiny HTTP server (standard library) on port 3000.
- Point UptimeRobot at your Replit web URL to keep the process awake.

Environment (use .env locally or Replit Secrets)
BOT_MODE=true|false
DISCORD_BOT_TOKEN=...         # required if BOT_MODE=true
DISCORD_CHANNEL_ID=...        # required if BOT_MODE=true
DISCORD_WEBHOOK_URL=...       # required if BOT_MODE=false
DISCORD_USER_ID=...           # optional (your Discord ID to @mention)
STEAM_API_KEY=...
STEAM_FRIEND_ID64=...
POLL_SECONDS=60
KEEPALIVE=true                # optional (default true on Replit)
ONLY_ONLINE=false             # optional: true = alert only when they come online (ignore games)
ONLY_GAMES=                   # optional: comma-separated list of game names to alert on (case-insensitive)
"""

import json
import os
import time
import threading
import signal
from typing import Dict, Any, Tuple, Optional
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

# Optional .env support for local dev; Replit uses Secrets
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

# --------- Config ----------
STEAM_API_KEY = os.getenv("STEAM_API_KEY", "").strip()
STEAM_FRIEND_ID64 = os.getenv("STEAM_FRIEND_ID64", "").strip()

# Webhook mode (simple) OR Bot mode (set BOT_MODE=true)
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID", "").strip()  # to @mention
POLL_SECONDS = max(15, int(os.getenv("POLL_SECONDS", "60")))  # hard floor to be kind to APIs

BOT_MODE = os.getenv("BOT_MODE", "").lower() in {"1", "true", "yes"}
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID", "").strip()

# Replit keep-alive
KEEPALIVE = os.getenv("KEEPALIVE", "true").lower() in {"1", "true", "yes"}
KEEPALIVE_PORT = int(os.getenv("PORT", "3000"))  # Replit expects 3000
KEEPALIVE_HOST = "0.0.0.0"

# Filtering options
ONLY_ONLINE = os.getenv("ONLY_ONLINE", "false").lower() in {"1", "true", "yes"}
ONLY_GAMES = [g.strip().lower() for g in os.getenv("ONLY_GAMES", "").split(",") if g.strip()]

STATUS_FILE = ".status.json"
STEAM_SUMMARIES_URL = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
PERSONA_MAP = {0: "offline", 1: "online", 2: "busy", 3: "away", 4: "snooze", 5: "looking to trade", 6: "looking to play"}

_shutdown = False  # for graceful exit

# --------- Keep-alive HTTP server (no external deps) ----------
class _OKHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK - Steam->Discord notifier is alive.")
    def log_message(self, *args, **kwargs):
        return  # silence default logging

def start_keepalive():
    def _run():
        with ThreadingHTTPServer((KEEPALIVE_HOST, KEEPALIVE_PORT), _OKHandler) as httpd:
            httpd.serve_forever(poll_interval=1)
    t = threading.Thread(target=_run, daemon=True)
    t.start()

# --------- Persistence ----------
def load_last_status() -> Dict[str, Any]:
    if not os.path.exists(STATUS_FILE):
        return {}
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_last_status(data: Dict[str, Any]) -> None:
    try:
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass

# --------- Steam ----------
def fetch_steam_status() -> Dict[str, Any]:
    params = {"key": STEAM_API_KEY, "steamids": STEAM_FRIEND_ID64}
    r = requests.get(STEAM_SUMMARIES_URL, params=params, timeout=20)
    r.raise_for_status()
    players = r.json().get("response", {}).get("players", [])
    if not players:
        raise RuntimeError("No player data returned — check SteamID64 and API key.")
    p = players[0]
    personastate = int(p.get("personastate", 0))
    state_label = PERSONA_MAP.get(personastate, f"unknown({personastate})")
    in_game = "gameextrainfo" in p
    game = p.get("gameextrainfo")
    name = p.get("personaname", "Friend")
    avatar = p.get("avatarfull")
    profile_url = p.get("profileurl")
    return {
        "name": name,
        "state": state_label,
        "personastate": personastate,
        "in_game": in_game,
        "game": game,
        "avatar": avatar,
        "profile_url": profile_url,
        "timestamp": int(time.time()),
    }

# --------- Notify logic ----------
def should_notify(prev: Dict[str, Any], curr: Dict[str, Any]) -> Tuple[bool, str]:
    """Return (notify?, reason)."""
    prev_state = int(prev.get("personastate", 0)) if prev else 0
    curr_state = int(curr.get("personastate", 0))
    prev_in_game = bool(prev.get("in_game")) if prev else False
    curr_in_game = bool(curr.get("in_game"))

    # Game filters (if set)
    if curr_in_game and ONLY_GAMES:
        game_name = (curr.get("game") or "").lower()
        if game_name and game_name not in ONLY_GAMES:
            # Playing a game but not one we care about
            # Still allow Online transition if ONLY_ONLINE is false and they just came online
            pass

    # 1) Offline → Online
    if prev_state == 0 and curr_state > 0:
        if ONLY_GAMES:
            # If you only care about certain games, you might not want generic "came online"
            # Leave it enabled unless ONLY_ONLINE is true and ONLY_GAMES set.
            if ONLY_ONLINE:
                return False, ""
        return True, "came online"

    # 2) Started playing
    if not prev_in_game and curr_in_game:
        if ONLY_GAMES:
            game_name = (curr.get("game") or "").lower()
            if game_name and game_name not in ONLY_GAMES:
                return False, ""
        if ONLY_ONLINE:
            # ONLY_ONLINE=true means ignore game alerts
            return False, ""
        return True, "started playing"

    return False, ""

def _mention_prefix() -> str:
    return f"<@{DISCORD_USER_ID}> " if DISCORD_USER_ID else ""

def send_discord_webhook(curr: Dict[str, Any], reason: str) -> None:
    if not DISCORD_WEBHOOK_URL:
        print("[warn] No DISCORD_WEBHOOK_URL provided; skipping notify.")
        return
    title = f"{curr['name']} {reason}!"
    desc = (
        f"Status: **{curr['state']}**\n"
        + (f"Game: **{curr['game']}**\n" if curr["in_game"] and curr.get("game") else "")
        + (f"Profile: {curr['profile_url']}\n" if curr.get("profile_url") else "")
    )
    payload = {
        "content": _mention_prefix() + "Steam update:",
        "embeds": [{"title": title, "description": desc, "thumbnail": {"url": curr.get("avatar", "")}}],
        "allowed_mentions": {"parse": ["users"]},
    }
    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=20)
    if r.status_code >= 300:
        print(f"[error] Webhook failed: {r.status_code} {r.text}")

def send_discord_bot(curr: Dict[str, Any], reason: str) -> None:
    if not (DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID):
        print("[warn] BOT_MODE enabled but token/channel missing; skipping notify.")
        return
    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages"
    title = f"{curr['name']} {reason}!"
    desc = (
        f"Status: **{curr['state']}**\n"
        + (f"Game: **{curr['game']}**\n" if curr["in_game"] and curr.get("game") else "")
        + (f"Profile: {curr['profile_url']}\n" if curr.get("profile_url") else "")
    )
    json_payload = {
        "content": _mention_prefix() + "Steam update:",
        "embeds": [{"title": title, "description": desc, "thumbnail": {"url": curr.get("avatar", "")}}],
        "allowed_mentions": {"parse": ["users"]},
    }
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    r = requests.post(url, headers=headers, json=json_payload, timeout=20)
    if r.status_code >= 300:
        print(f"[error] Bot send failed: {r.status_code} {r.text}")

# --------- Main loop ----------
def _handle_sigterm(signum, frame):
    global _shutdown
    _shutdown = True
signal.signal(signal.SIGTERM, _handle_sigterm)
signal.signal(signal.SIGINT, _handle_sigterm)

def main():
    if not (STEAM_API_KEY and STEAM_FRIEND_ID64):
        raise SystemExit("Please set STEAM_API_KEY and STEAM_FRIEND_ID64 as env vars (or in .env locally).")
    if not BOT_MODE and not DISCORD_WEBHOOK_URL:
        raise SystemExit("Provide DISCORD_WEBHOOK_URL (webhook mode) or set BOT_MODE=true with bot token+channel.")

    if KEEPALIVE:
        start_keepalive()
        print(f"[keepalive] HTTP server on http://{KEEPALIVE_HOST}:{KEEPALIVE_PORT}/")

    # 🔔 Send startup notification
    startup_message = {
        "name": "Bot",
        "state": "startup",
        "personastate": 1,
        "in_game": False,
        "game": None,
        "avatar": "",
        "profile_url": "",
        "timestamp": int(time.time())
    }
    if BOT_MODE:
        send_discord_bot(startup_message, "is now online ✅")
    else:
        send_discord_webhook(startup_message, "is now online ✅")

    state = load_last_status()
    print("Steam → Discord notifier running. Poll interval:", POLL_SECONDS, "seconds")

    while not _shutdown:
        try:
            curr = fetch_steam_status()
            notify, reason = should_notify(state, curr)
            if notify:
                if BOT_MODE:
                    send_discord_bot(curr, reason)
                else:
                    send_discord_webhook(curr, reason)
                state = curr
                save_last_status(state)
        except requests.HTTPError as e:
            print("[error] HTTP:", e)
        except Exception as e:
            print("[error]", e)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
