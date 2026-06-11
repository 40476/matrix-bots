#!/usr/bin/env python3
"""
betterFM - A secure, highly customizable Matrix Last.fm Bot.
Features:
- Now playing cards (!fm) with dynamic PIL-rendered image generation.
- Dynamic weekly/daily scrobble stats (!fmstats).
- Custom database to pair Matrix IDs with Last.fm accounts (!setuser).
- Configurable style presets and a secure, sandboxed custom canvas rendering language (!setstyle / custom layout definitions).
- Extremely secure layout parsing engine (no eval, no path traversals, strict validation).
- Interactive first-time CLI setup wizard with config file generation.
- Automated room invite-joining mechanism with retry safety logic.
- Secure, keyless alternative cover art fetcher (iTunes API fallback) when Last.fm missing artwork.
- Relative Last Active/Activity tracking notice ({activity}) built into rendering pipeline.
- Proper alpha channel compositing for beautiful glassmorphism and frosted glass blends.

Dependencies:
    pip install matrix-nio pillow requests aiohttp
"""

import os
import re
import json
import time
import asyncio
import logging
from io import BytesIO
from typing import Dict, Any, Tuple, Optional, Union
from urllib.parse import quote_plus

# Third party dependencies
from nio import AsyncClient, MatrixRoom, RoomMessageText, InviteMemberEvent
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageColor
import aiohttp

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("betterFM")

# --- Configuration & Defaults ---
CONFIG_PATH = os.getenv("BETTERFM_CONFIG", "config.json")

def setup_config() -> bool:
    """Checks for configuration, prompting the user interactively if missing."""
    if os.path.exists(CONFIG_PATH):
        return True
        
    print("="*60)
    print("       INITIAL SETUP (Ugh, making me do manual labor already?)     ")
    print("="*60)
    print(f"Configuration file '{CONFIG_PATH}' not found.")
    print("Please input the connection configuration for your Last.fm bot:")
    
    homeserver = input("Enter Matrix homeserver URL (default: https://matrix.org): ").strip()
    if not homeserver:
        homeserver = "https://matrix.org"
        
    if not homeserver.startswith("http://") and not homeserver.startswith("https://"):
        homeserver = "https://" + homeserver
        
    username = input("Enter bot's Matrix username (e.g., @mybot:matrix.org): ").strip()
    while not username or not username.startswith("@"):
        username = input("Please enter a valid username starting with '@': ").strip()
        
    password = input("Enter Matrix password: ").strip()
    while not password:
        password = input("Password cannot be blank. Enter password: ").strip()

    lastfm_key = input("Enter Last.fm API Key: ").strip()
    while not lastfm_key:
        lastfm_key = input("You need a Last.fm API Key. Please enter it: ").strip()
    
    config_data = {
        "homeserver": homeserver,
        "username": username,
        "password": password,
        "lastfm_api_key": lastfm_key,
        "db_file": "betterfm_db.json",
        "cache_dir": "./cache"
    }
    
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(config_data, f, indent=4)
        print(f"\n[+] Configuration saved to '{CONFIG_PATH}'! Don't lose it, I won't ask nicely next time.")
        print("="*60)
        return True
    except Exception as e:
        logger.error(f"Failed to write configuration file: {e}")
        return False

# Trigger config setup check before loading CONFIG
setup_config()

# Read config from file if available, falling back to environment variables
FILE_CONFIG = {}
if os.path.exists(CONFIG_PATH):
    try:
        with open(CONFIG_PATH, "r") as f:
            FILE_CONFIG = json.load(f)
    except Exception as e:
        logger.error(f"Error reading configuration file: {e}")

CONFIG = {
    "HOMESERVER": FILE_CONFIG.get("homeserver") or os.getenv("MATRIX_HOMESERVER", "https://matrix.org"),
    "USER_ID": FILE_CONFIG.get("username") or os.getenv("MATRIX_USER_ID", "@betterfm_bot:matrix.org"),
    "PASSWORD": FILE_CONFIG.get("password") or os.getenv("MATRIX_PASSWORD", ""),
    "ACCESS_TOKEN": os.getenv("MATRIX_ACCESS_TOKEN", ""),
    "LASTFM_API_KEY": FILE_CONFIG.get("lastfm_api_key") or os.getenv("LASTFM_API_KEY", ""),
    "DB_FILE": FILE_CONFIG.get("db_file") or os.getenv("BETTERFM_DB", "betterfm_db.json"),
    "CACHE_DIR": FILE_CONFIG.get("cache_dir") or os.getenv("BETTERFM_CACHE", "./cache"),
}

# Ensure cache directory exists
os.makedirs(CONFIG["CACHE_DIR"], exist_ok=True)


# --- Database Operations (Secure JSON Storage) ---
def load_db() -> Dict[str, Any]:
    if os.path.exists(CONFIG["DB_FILE"]):
        try:
            with open(CONFIG["DB_FILE"], "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error reading database file: {e}")
    return {"users": {}, "styles": {}}

def save_db(data: Dict[str, Any]):
    try:
        with open(CONFIG["DB_FILE"], "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logger.error(f"Error writing to database: {e}")

def get_user_lastfm(matrix_id: str) -> Optional[str]:
    db = load_db()
    return db.get("users", {}).get(matrix_id, {}).get("lastfm")

def set_user_lastfm(matrix_id: str, lastfm_username: str):
    db = load_db()
    if "users" not in db:
        db["users"] = {}
    if matrix_id not in db["users"]:
        db["users"][matrix_id] = {}
    db["users"][matrix_id]["lastfm"] = lastfm_username
    save_db(db)

def get_user_style(matrix_id: str) -> str:
    db = load_db()
    return db.get("users", {}).get(matrix_id, {}).get("style", "modern_dark")

def set_user_style(matrix_id: str, style_name_or_spec: str):
    db = load_db()
    if "users" not in db:
        db["users"] = {}
    if matrix_id not in db["users"]:
        db["users"][matrix_id] = {}
    db["users"][matrix_id]["style"] = style_name_or_spec
    save_db(db)


# --- Last.fm API Client ---
class LastFMClient:
    BASE_URL = "http://ws.audioscrobbler.com/2.0/"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def _fetch(self, params: Dict[str, str]) -> Optional[Dict[str, Any]]:
        params["api_key"] = self.api_key
        params["format"] = "json"
        
        if not self.api_key:
            logger.error("Last.fm API Key is missing!")
            return None

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(self.BASE_URL, params=params, timeout=10) as response:
                    if response.status == 200:
                        return await response.json()
                    else:
                        logger.warning(f"Last.fm API error status {response.status}")
                        return None
            except Exception as e:
                logger.error(f"Failed connecting to Last.fm API: {e}")
                return None

    async def get_fallback_cover_art(self, artist: str, title: str, album: str) -> Optional[str]:
        """
        Securely fetches high-quality cover art from the iTunes Search API 
        if Last.fm fails to provide a URL.
        """
        search_term = f"{artist} {title}"
        if album and album.lower() != "unknown album":
            search_term += f" {album}"

        # Clean search term from special or problematic characters
        search_term = re.sub(r"[^\w\s\-]", "", search_term)
        url = f"https://itunes.apple.com/search?term={quote_plus(search_term)}&entity=song&limit=1"

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, timeout=5) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        results = data.get("results", [])
                        if results:
                            art_url = results[0].get("artworkUrl100", "")
                            if art_url:
                                return art_url.replace("100x100bb", "600x600bb")
            except Exception as e:
                logger.warning(f"Failed fetching fallback artwork from iTunes: {e}")
        return None

    async def get_now_playing(self, username: str) -> Optional[Dict[str, Any]]:
        data = await self._fetch({
            "method": "user.getrecenttracks",
            "user": username,
            "limit": "1"
        })
        if not data or "recenttracks" not in data or not data["recenttracks"].get("track"):
            return None

        track_data = data["recenttracks"]["track"]
        if not track_data:
            return None
        
        track = track_data[0] if isinstance(track_data, list) else track_data
        
        is_now_playing = False
        attr = track.get("@attr", {})
        if attr.get("nowplaying") == "true":
            is_now_playing = True

        artist_name = track.get("artist", {}).get("#text", "Unknown Artist")
        track_title = track.get("name", "Unknown Title")
        album_name = track.get("album", {}).get("#text", "Unknown Album")

        images = track.get("image", [])
        album_art_url = ""
        for img in images:
            if img.get("size") == "extralarge" or img.get("size") == "large":
                album_art_url = img.get("#text", "")

        # Fallback cover art search if Last.fm didn't provide any artwork image URL
        if not album_art_url:
            logger.info(f"Last.fm artwork missing. Querying alternative sources for: {artist_name} - {track_title}")
            album_art_url = await self.get_fallback_cover_art(artist_name, track_title, album_name) or ""

        # Activity Relative Notice calculation
        activity = "Active"
        if not is_now_playing:
            uts = track.get("date", {}).get("uts")
            if uts:
                try:
                    diff = int(time.time()) - int(uts)
                    if diff < 60:
                        activity = "Last Activity: just now"
                    elif diff < 3600:
                        activity = f"Last Activity: {diff // 60}m ago"
                    elif diff < 86400:
                        activity = f"Last Activity: {diff // 3600}h ago"
                    else:
                        activity = f"Last Activity: {diff // 86400}d ago"
                except Exception:
                    activity = "Last Activity: offline"
            else:
                activity = "Last Activity: inactive"

        return {
            "username": username,
            "title": track_title,
            "artist": artist_name,
            "album": album_name,
            "now_playing": is_now_playing,
            "album_art": album_art_url or None,
            "activity": activity
        }

    async def get_user_stats(self, username: str, period: str = "7day") -> Optional[Dict[str, Any]]:
        data = await self._fetch({
            "method": "user.gettoptracks",
            "user": username,
            "period": period,
            "limit": "5"
        })
        profile = await self._fetch({
            "method": "user.getinfo",
            "user": username
        })
        
        scrobble_count = "0"
        if profile and "user" in profile:
            scrobble_count = profile["user"].get("playcount", "0")

        if not data or "toptracks" not in data:
            return None
            
        tracks = data["toptracks"].get("track", [])
        if not isinstance(tracks, list):
            tracks = [tracks]

        top_tracks_list = []
        for t in tracks[:5]:
            top_tracks_list.append({
                "title": t.get("name", "Unknown Track"),
                "artist": t.get("artist", {}).get("name", "Unknown Artist"),
                "scrobbles": t.get("playcount", "0")
            })

        return {
            "username": username,
            "total_scrobbles": scrobble_count,
            "top_tracks": top_tracks_list,
            "period": period
        }


# --- SECURE CANVAS / RENDERING ENGINE Presets ---
# Upgraded with proper alpha-layered designs, beautiful square/vertical sizes, and 4 brand new highly-distinguished styles!
STYLE_PRESETS = {
    "modern_dark": (
        "canvas 800 250 #1e1e24\n"
        "rect 0 0 800 250 #121214\n"
        "rect 20 20 210 210 #2a2a35\n"
        "image 20 20 210 210 {album_art}\n"
        "text 250 45 {artist} #ffffff 28 bold\n"
        "text 250 90 {title} #1db954 22\n"
        "text 250 135 {album} #a0a0b0 18 italic\n"
        "text 250 190 Last.fm: {username} • {activity} #888899 14\n"
    ),
    "vinyl_square": (
        "canvas 500 500 #0a0a0c\n"
        # Concentric classic vinyl record plates
        "ellipse 30 30 470 470 #111113\n"
        "ellipse 90 90 410 410 #18181b\n"
        "ellipse 160 160 340 350 #000000\n"
        # Center sharp cover art inside vinyl core
        "image 160 160 180 180 {album_art}\n"
        # Curved labels and metadata on the outer space
        "text 35 375 {artist} #ffffff 24 bold\n"
        "text 35 410 {title} #1db954 18 bold\n"
        "text 35 440 {album} #8c8c99 14 italic\n"
        "text 35 470 Status: {activity} #00ffff 12 bold\n"
    ),
    "glass_square": (
        "canvas 600 600 #0f0b18\n"
        # Dynamically blurred background art filling the entire 1:1 square canvas
        "image 0 0 600 600 {album_art} 45\n"
        # Deep translucent dark frost layer over background
        "rect 0 0 600 600 #000000b0\n"
        # Frosted glass inner plate with blended semi-transparent white
        "rect 40 40 560 560 #ffffff14\n"
        "rect 40 40 560 560 #ffffff08\n"
        "image 150 90 300 300 {album_art}\n"
        "text 80 415 {artist} #ffffff 28 bold\n"
        "text 80 455 {title} #00f0ff 22 bold\n"
        "text 80 495 {album} #ffffffcc 16 italic\n"
        "text 80 525 {activity} #ffffff80 13 bold\n"
    ),
    "poster_vertical": (
        "canvas 450 700 #101014\n"
        # Full-height portrait blurred backdrop art
        "image 0 0 450 700 {album_art} 45\n"
        "rect 0 0 450 700 #0000009c\n"
        # Symmetrical border framing lines
        "rect 30 30 420 670 #ffffff14\n"
        # Sharp front cover centering
        "image 75 70 300 300 {album_art}\n"
        # Text details aligned vertically
        "text 75 410 {artist} #ffffff 28 bold\n"
        "text 75 465 {title} #00f0ff 22 bold\n"
        "text 75 515 {album} #e2e2e9 18 italic\n"
        "text 75 565 {activity} #ffe5b4 14 bold\n"
        "text 75 605 LISTENER: {username} #ffffff80 13 bold\n"
    ),
    "retro_vertical": (
        "canvas 400 650 #020208\n"
        "rect 10 10 390 640 #ff007f\n"
        "rect 15 15 385 635 #050510\n"
        # Sharp framed cassette/arcade art window
        "rect 40 40 360 360 #00ffff\n"
        "image 45 45 310 310 {album_art}\n"
        # Retro layout details stacked
        "text 45 425 {artist} #00ffff 26 bold\n"
        "text 45 465 {title} #ff007f 20 bold\n"
        "text 45 505 {album} #ffff00 16 italic\n"
        "text 45 545 {activity} #00ffff 14 bold\n"
        "text 45 590 RETRO_STREAM // {username} #00ffffa0 12\n"
    ),
    "cyberpunk_vertical": (
        "canvas 450 700 #fcee0a\n"
        "rect 12 0 450 700 #000000\n"
        "rect 0 0 12 700 #fcee0a\n"
        # Hazard style frames
        "rect 40 40 410 410 #00f0ff\n"
        "image 45 45 360 360 {album_art}\n"
        # High contrast futuristic neon readouts
        "rect 40 440 410 490 #fcee0a\n"
        "text 50 452 {artist} #000000 24 bold\n"
        "text 40 515 {title} #ffffff 22 bold\n"
        "text 40 565 {album} #00f0ff 18 italic\n"
        "text 40 595 STATE: {activity} #00f0ff 13 bold\n"
        "text 40 635 [USER_CONNECT: {username}] #fcee0a 12 bold\n"
    ),
    "cozy_vertical": (
        "canvas 400 600 #faf0e6\n"
        "rect 15 15 385 585 #f3e9dc\n"
        # Polaroid styled photograph alignment
        "rect 50 45 350 345 #ffffff\n"
        "image 65 60 270 270 {album_art}\n"
        # Soft details underneath
        "text 50 410 {artist} #5e503f 26 bold\n"
        "text 50 450 {title} #8c7a6b 20 bold\n"
        "text 50 490 {album} #bdaaa4 16 italic\n"
        "text 50 525 {activity} #a3938b 14 italic\n"
        "text 50 555 Cozy listener: {username} #a3938b 13\n"
    ),
    "vaporwave": (
        "canvas 850 260 #f3dbcf\n"
        # Retro sunset backdrop blur layers
        "ellipse 350 -50 750 350 #ff71ce\n"
        "ellipse 420 50 620 250 #01cdfe\n"
        "blur 30\n"
        "rect 30 30 220 220 #05ffa1\n"
        "image 30 30 220 220 {album_art}\n"
        "text 262 52 {artist} #ff71ce 30 bold\n"
        "text 260 50 {artist} #01cdfe 30 bold\n"
        "text 261 101 {title} #05ffa1 21\n"
        "text 260 100 {title} #b967ff 21\n"
        "text 260 142 {album} #ffffff 16 italic\n"
        "text 260 172 {activity} #05ffa1 14 bold\n"
        "text 260 205 [A E S T H E T I C : {username}] #01cdfe 13 bold\n"
    ),
    "cassette_retro": (
        "canvas 800 300 #282828\n"
        # Cassette outer body frame with rounded holes
        "rect 20 20 780 280 #1e1e1e\n"
        "rect 40 40 760 260 #121212\n"
        # Classic cassette sticker label (Cream colored)
        "rect 100 60 700 240 #faf6e6\n"
        "rect 100 110 700 120 #e53935\n" # Retro red accent stripe
        "rect 100 125 700 135 #1e88e5\n" # Retro blue accent stripe
        # Cassette dynamic center label window
        "rect 260 140 540 220 #121212\n"
        "ellipse 280 150 340 210 #faf6e6\n" # Cassette wheel left
        "ellipse 460 150 520 210 #faf6e6\n" # Cassette wheel right
        # Handwriting style text inside the tape label
        "text 120 70 {artist} #121212 24 bold\n"
        "text 120 150 Track: {title} #121212 18 bold\n"
        "text 120 185 Album: {album} #424242 15 italic\n"
        "text 120 215 Cassette Stream • {activity} #e53935 13 bold\n"
    ),
    "neon_club": (
        "canvas 800 260 #08080f\n"
        # Glowing neon background brick gridlines simulation
        "rect 10 10 790 250 #12121e\n"
        "rect 15 15 785 245 #08080f\n"
        # Glowing border around album art
        "rect 35 35 215 215 #ff007f\n"
        "image 40 40 205 205 {album_art}\n"
        # Glowing multi-layered neon text
        "text 262 52 {artist} #00ffff 28 bold\n"
        "text 260 50 {artist} #ffffff 28 bold\n"
        "text 261 101 {title} #ff007f 22 bold\n"
        "text 260 100 {title} #00ffff 22 bold\n"
        "text 260 145 {album} #ff007f 16 italic\n"
        "text 260 195 Club Stream • {activity} #05ffa1 13 bold\n"
    ),
    "minimal_album": (
        "canvas 500 500 #ffffff\n"
        # Elegant clean gallery/museum card look
        "rect 15 15 485 485 #f9f9fb\n"
        "rect 20 20 480 480 #ffffff\n"
        "image 50 50 400 300 {album_art}\n"
        # Clean typography centered underneath
        "text 50 370 {artist} #111115 24 bold\n"
        "text 50 405 {title} #5c5c68 18\n"
        "text 50 435 {album} #8e8e9c 14 italic\n"
        "text 50 460 Listener: {username} • {activity} #9a80b0 12 bold\n"
    ),
    "manga_panel": (
        "canvas 820 260 #ffffff\n"
        # Manga screen margins
        "rect 10 10 810 250 #000000\n"
        "rect 15 15 805 245 #ffffff\n"
        # Left image halftone outline
        "rect 30 30 215 215 #000000\n"
        "image 33 33 209 209 {album_art} 0\n"
        # Manga speed lines / screentone columns
        "rect 730 15 740 245 #000000\n"
        "rect 750 15 755 245 #000000\n"
        "rect 765 15 768 245 #000000\n"
        "rect 775 15 776 245 #000000\n"
        # Comic book bubble background & sharp metadata
        "rect 240 30 700 215 #000000\n"
        "rect 243 33 697 212 #ffffff\n"
        "text 265 45 {artist} #000000 28 bold\n"
        "text 265 95 > {title} #000000 22 bold\n"
        "text 265 140 {album} #000000 16 italic\n"
        "text 265 180 STATUS: {activity} #000000 13 bold\n"
    )
}

class SecureRenderer:
    HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3,4}){1,2}$")

    @classmethod
    def parse_color(cls, color_str: str) -> Tuple[int, int, int, int]:
        color_str = color_str.strip()
        if cls.HEX_COLOR_RE.match(color_str):
            try:
                rgba = ImageColor.getrgb(color_str)
                if len(rgba) == 3:
                    return (rgba[0], rgba[1], rgba[2], 255)
                return rgba
            except ValueError:
                return (128, 128, 128, 255)
        return (255, 255, 255, 255)

    @classmethod
    def sanitize_metadata(cls, text: str) -> str:
        sanitized = re.sub(r"[\x00-\x1f\x7f-\x9f\\\'\"]", "", text)
        return sanitized[:80]

    @classmethod
    async def render_card(cls, track_info: Dict[str, Any], style_spec: str) -> BytesIO:
        canvas_width, canvas_height = 800, 250
        bg_color = (30, 30, 36, 255)
        
        lines = style_spec.split("\n")
        
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if parts[0] == "canvas":
                if len(parts) >= 3:
                    try:
                        canvas_width = min(max(int(parts[1]), 100), 1200)
                        canvas_height = min(max(int(parts[2]), 100), 800)
                        if len(parts) >= 4:
                            bg_color = cls.parse_color(parts[3])
                    except ValueError:
                        pass

        # Complete RGBA canvas backing for premium quality blending
        img = Image.new("RGBA", (canvas_width, canvas_height), bg_color)
        draw = ImageDraw.Draw(img)

        album_art_img = None
        if track_info.get("album_art"):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(track_info["album_art"], timeout=5) as resp:
                        if resp.status == 200:
                            art_data = await resp.read()
                            album_art_img = Image.open(BytesIO(art_data)).convert("RGBA")
            except Exception as e:
                logger.warning(f"Could not load album art: {e}")

        if not album_art_img:
            album_art_img = Image.new("RGBA", (300, 300), (42, 42, 50, 255))
            p_draw = ImageDraw.Draw(album_art_img)
            p_draw.rectangle([20, 20, 280, 280], outline=(128, 128, 128), width=3)

        replacements = {
            "{artist}": cls.sanitize_metadata(track_info.get("artist", "Unknown Artist")),
            "{title}": cls.sanitize_metadata(track_info.get("title", "Unknown Title")),
            "{album}": cls.sanitize_metadata(track_info.get("album", "Unknown Album")),
            "{username}": cls.sanitize_metadata(track_info.get("username", "User")),
            "{activity}": cls.sanitize_metadata(track_info.get("activity", "Inactive")),
        }

        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
                
            parts = line.split()
            cmd = parts[0].lower()

            try:
                if cmd == "rect" and len(parts) >= 6:
                    x0, y0, x1, y1 = map(int, parts[1:5])
                    color = cls.parse_color(parts[5])
                    # Fix: Handle true transparency alpha compositing to avoid overwrite artifacts
                    if len(color) == 4 and color[3] < 255:
                        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
                        overlay_draw = ImageDraw.Draw(overlay)
                        overlay_draw.rectangle([x0, y0, x1, y1], fill=color)
                        img = Image.alpha_composite(img, overlay)
                        draw = ImageDraw.Draw(img) # Re-establish context
                    else:
                        draw.rectangle([x0, y0, x1, y1], fill=color)

                elif cmd == "ellipse" and len(parts) >= 6:
                    x0, y0, x1, y1 = map(int, parts[1:5])
                    color = cls.parse_color(parts[5])
                    # Fix: Handle true transparency alpha compositing to avoid overwrite artifacts
                    if len(color) == 4 and color[3] < 255:
                        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
                        overlay_draw = ImageDraw.Draw(overlay)
                        overlay_draw.ellipse([x0, y0, x1, y1], fill=color)
                        img = Image.alpha_composite(img, overlay)
                        draw = ImageDraw.Draw(img) # Re-establish context
                    else:
                        draw.ellipse([x0, y0, x1, y1], fill=color)

                elif cmd == "image" and len(parts) >= 5:
                    x, y, w, h = map(int, parts[1:5])
                    
                    # Search all remaining arguments securely for any digit to use as blur
                    blur_val = 0
                    for p in parts[5:]:
                        if p.isdigit():
                            blur_val = min(max(int(p), 0), 100)
                            break

                    resized_art = album_art_img.resize((w, h), Image.Resampling.LANCZOS)
                    if blur_val > 0:
                        resized_art = resized_art.filter(ImageFilter.GaussianBlur(blur_val))
                    img.alpha_composite(resized_art, (x, y))

                elif cmd == "blur" and len(parts) >= 2:
                    radius = min(max(int(parts[1]), 1), 50)
                    img = img.filter(ImageFilter.GaussianBlur(radius))
                    draw = ImageDraw.Draw(img)

                elif cmd == "text" and len(parts) >= 5:
                    x, y = int(parts[1]), int(parts[2])
                    
                    color_idx = -1
                    for idx, part in enumerate(parts[3:], start=3):
                        if part.startswith("#"):
                            color_idx = idx
                            break
                    
                    if color_idx != -1:
                        raw_text = " ".join(parts[3:color_idx])
                        color = cls.parse_color(parts[color_idx])
                        size = 18
                        if len(parts) > color_idx + 1:
                            try:
                                size = min(max(int(parts[color_idx+1]), 8), 72)
                            except ValueError:
                                pass
                    else:
                        raw_text = parts[3]
                        color = (255, 255, 255, 255)
                        size = 18

                    for key, val in replacements.items():
                        raw_text = raw_text.replace(key, val)

                    font = None
                    for font_path in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 
                                      "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                                      "Arial", "Helvetica"]:
                        try:
                            font = ImageFont.truetype(font_path, size)
                            break
                        except IOError:
                            continue
                    
                    if not font:
                        font = ImageFont.load_default()

                    draw.text((x, y), raw_text, fill=color, font=font)

            except Exception as parse_error:
                logger.warning(f"Failed parsing custom style line [{line}]: {parse_error}")

        output = BytesIO()
        img.save(output, format="PNG")
        output.seek(0)
        return output


# --- Matrix Bot Main Logic ---
class BetterFMBot:
    def __init__(self, config: Dict[str, str]):
        self.config = config
        self.client = AsyncClient(config["HOMESERVER"], config["USER_ID"])
        self.lastfm = LastFMClient(config["LASTFM_API_KEY"])
        # Record startup timestamp in epoch milliseconds to ignore older replayed messages
        self.start_time_ms = int(time.time() * 1000)

    async def start(self):
        logger.info("Connecting to Matrix homeserver...")
        if self.config["ACCESS_TOKEN"]:
            self.client.access_token = self.config["ACCESS_TOKEN"]
            self.client.user_id = self.config["USER_ID"]
        else:
            await self.client.login(self.config["PASSWORD"])

        logger.info("Bot is logged in. Starting room sync and event handlers...")
        self.client.add_event_callback(self.on_room_message, RoomMessageText)
        self.client.add_event_callback(self.handle_invite, InviteMemberEvent)
        await self.client.sync_forever(timeout=30000, full_state=True)

    async def handle_invite(self, room: MatrixRoom, event: InviteMemberEvent):
        """Automatically joins any room the bot is invited to with retry safety."""
        if event.state_key == self.client.user_id:
            logger.info(f"Received invite from {event.sender} to join room {room.room_id}.")
            
            for attempt in range(3):
                logger.info(f"Trying to join room (Attempt {attempt + 1}/3)...")
                response = await self.client.join(room.room_id)
                
                if hasattr(response, "room_id"):
                    logger.info(f"Successfully joined room {room.room_id}!")
                    break
                else:
                    err_msg = getattr(response, 'message', 'Unknown error')
                    logger.warning(f"Failed to join room: {err_msg}")
                    await asyncio.sleep(3)

    async def on_room_message(self, room: MatrixRoom, event: RoomMessageText):
        # Ignore our own messages
        if event.sender == self.client.user_id:
            return

        # Securely ignore older messages that were cached/replayed during sync startup
        if event.server_timestamp < self.start_time_ms:
            return

        body = event.body.strip()
        if not body:
            return

        parts = body.split()
        cmd = parts[0].lower()

        if cmd == "!fm":
            await self.handle_fm(room, event, parts)
        elif cmd == "!fmstats":
            await self.handle_fm_stats(room, event, parts)
        elif cmd == "!setuser":
            await self.handle_set_user(room, event, parts)
        elif cmd == "!setstyle":
            await self.handle_set_style(room, event, body)

    async def upload_image_to_matrix(self, image_data: BytesIO, filename: str) -> Optional[str]:
        """Uploads a PIL generated PNG binary onto Matrix media storage securely."""
        try:
            logger.info(f"Starting upload for {filename} ({image_data.getbuffer().nbytes} bytes)...")
            resp = await self.client.upload(
                image_data,
                "image/png",
                filename
            )
            logger.info(f"Upload completed. Server response: {resp}")
            
            if isinstance(resp, tuple):
                resp = resp[0]
            
            if hasattr(resp, "content_uri"):
                logger.info(f"Upload successful! MXC URI: {resp.content_uri}")
                return resp.content_uri
            else:
                if hasattr(resp, "message"):
                    logger.error(f"Matrix media upload rejected: {resp.message} (Code: {getattr(resp, 'status_code', 'N/A')})")
                else:
                    logger.error(f"Matrix media upload failed with unexpected response type: {type(resp)}")
                return None
        except Exception as e:
            logger.error(f"Failed to upload media to Matrix server due to exception: {e}", exc_info=True)
            return None

    async def handle_fm(self, room: MatrixRoom, event: RoomMessageText, parts: list):
        sender = event.sender
        target_lastfm = None

        if len(parts) > 1:
            possible_target = parts[1]
            if possible_target.startswith("@"):
                target_lastfm = get_user_lastfm(possible_target)
                if not target_lastfm:
                    await self.client.room_send(
                        room.room_id,
                        "m.room.message",
                        {
                            "msgtype": "m.text",
                            "body": f"No Last.fm account bound to Matrix user: {possible_target}. Ask them to run !setuser <username>."
                        }
                    )
                    return
            else:
                target_lastfm = possible_target
        else:
            target_lastfm = get_user_lastfm(sender)
            if not target_lastfm:
                await self.client.room_send(
                    room.room_id,
                    "m.room.message",
                    {
                        "msgtype": "m.text",
                        "body": f"Hi {sender}! You haven't registered your Last.fm profile with this bot. Please set it using: !setuser <lastfm_username>"
                    }
                )
                return

        await self.client.room_typing(room.room_id, True)
        
        try:
            track_info = await self.lastfm.get_now_playing(target_lastfm)
            if not track_info:
                await self.client.room_send(
                    room.room_id,
                    "m.room.message",
                    {
                        "msgtype": "m.text",
                        "body": f"Could not find recent track data for user: {target_lastfm}."
                    }
                )
                return

            style_setting = get_user_style(sender)
            style_spec = STYLE_PRESETS.get(style_setting)
            
            # Extract canvas configuration dimensions to configure custom sizing cleanly
            card_width, card_height = 800, 250
            if not style_spec:
                if "canvas" in style_setting:
                    style_spec = style_setting
                    # Parse custom width/height from the custom spec block to pass info securely to room_send
                    lines = style_spec.split("\n")
                    for line in lines:
                        line = line.strip()
                        if line.startswith("canvas"):
                            sp = line.split()
                            if len(sp) >= 3:
                                try:
                                    card_width = min(max(int(sp[1]), 100), 1200)
                                    card_height = min(max(int(sp[2]), 100), 800)
                                except ValueError:
                                    pass
                else:
                    style_spec = STYLE_PRESETS["modern_dark"]
            else:
                # Retrieve the standard dimensions for our customized style presets
                lines = style_spec.split("\n")
                for line in lines:
                    line = line.strip()
                    if line.startswith("canvas"):
                        sp = line.split()
                        if len(sp) >= 3:
                            try:
                                card_width = int(sp[1])
                                card_height = int(sp[2])
                            except ValueError:
                                pass

            image_stream = await SecureRenderer.render_card(track_info, style_spec)
            mxc_uri = await self.upload_image_to_matrix(image_stream, f"{target_lastfm}_fm.png")
            
            if mxc_uri:
                status_msg = "Currently playing:" if track_info["now_playing"] else "Last played track:"
                formatted_body = (
                    f"🎵 {status_msg} {track_info['artist']} - {track_info['title']} "
                    f"(Album: {track_info['album']}) [{target_lastfm}]"
                )
                
                await self.client.room_send(
                    room.room_id,
                    "m.room.message",
                    {
                        "msgtype": "m.image",
                        "body": formatted_body,
                        "url": mxc_uri,
                        "info": {
                            "mimetype": "image/png",
                            "w": card_width,
                            "h": card_height
                        }
                    }
                )
            else:
                await self.client.room_send(
                    room.room_id,
                    "m.room.message",
                    {
                        "msgtype": "m.text",
                        "body": f"🎵 {target_lastfm}: {track_info['artist']} - {track_info['title']} (Album: {track_info['album']})"
                    }
                )

        finally:
            await self.client.room_typing(room.room_id, False)

    async def handle_fm_stats(self, room: MatrixRoom, event: RoomMessageText, parts: list):
        sender = event.sender
        period = "7day"
        
        target_lastfm = get_user_lastfm(sender)
        if len(parts) > 1:
            possible_target = parts[1]
            if possible_target.startswith("@"):
                target_lastfm = get_user_lastfm(possible_target)
            else:
                target_lastfm = possible_target

        if not target_lastfm:
            await self.client.room_send(
                room.room_id,
                "m.room.message",
                {
                    "msgtype": "m.text",
                    "body": "No username registered. Please use !setuser <username> first."
                }
            )
            return

        if "daily" in parts or "day" in parts:
            period = "7day"
        elif "monthly" in parts or "month" in parts:
            period = "1month"
        elif "overall" in parts:
            period = "overall"

        await self.client.room_typing(room.room_id, True)
        try:
            stats = await self.lastfm.get_user_stats(target_lastfm, period)
            if not stats:
                await self.client.room_send(
                    room.room_id,
                    "m.room.message",
                    {
                        "msgtype": "m.text",
                        "body": f"Unable to fetch statistics for Last.fm user: {target_lastfm}"
                    }
                )
                return

            p_title = {"7day": "Weekly", "1month": "Monthly", "overall": "Overall"}.get(period, "Weekly")
            
            msg = f"📊 **{p_title} Last.fm Stats for {stats['username']}**\n"
            msg += f"Total play scrobbles: **{stats['total_scrobbles']}**\n\n"
            msg += "🏆 **Top Tracks:**\n"
            for index, track in enumerate(stats["top_tracks"], start=1):
                msg += f"{index}. **{track['artist']}** - *{track['title']}* ({track['scrobbles']} plays)\n"

            await self.client.room_send(
                room.room_id,
                "m.room.message",
                {
                    "msgtype": "m.text",
                    "body": msg,
                    "format": "org.matrix.custom.html",
                    "formatted_body": msg.replace("\n", "<br>")
                }
            )
        finally:
            await self.client.room_typing(room.room_id, False)

    async def handle_set_user(self, room: MatrixRoom, event: RoomMessageText, parts: list):
        if len(parts) < 2:
            await self.client.room_send(
                room.room_id,
                "m.room.message",
                {
                    "msgtype": "m.text",
                    "body": "Usage: !setuser <lastfm_username>"
                }
            )
            return

        lastfm_username = parts[1].strip()
        if not re.match(r"^[a-zA-Z0-9_\-]{2,15}$", lastfm_username):
            await self.client.room_send(
                room.room_id,
                "m.room.message",
                {
                    "msgtype": "m.text",
                    "body": "Error: Last.fm usernames must be alphanumeric (2-15 chars long)."
                }
            )
            return

        set_user_lastfm(event.sender, lastfm_username)
        await self.client.room_send(
            room.room_id,
            "m.room.message",
            {
                "msgtype": "m.text",
                "body": f"✅ Bound your Matrix handle to Last.fm username: {lastfm_username}"
            }
        )

    async def handle_set_style(self, room: MatrixRoom, event: RoomMessageText, body: str):
        sender = event.sender
        parts = body.split()
        
        if len(parts) < 2:
            presets_list = ", ".join(STYLE_PRESETS.keys())
            await self.client.room_send(
                room.room_id,
                "m.room.message",
                {
                    "msgtype": "m.text",
                    "body": f"Usage: !setstyle <preset_name> OR !setstyle custom <layout directives>\nAvailable Presets: {presets_list}"
                }
            )
            return

        option = parts[1].lower()

        if option in STYLE_PRESETS:
            set_user_style(sender, option)
            await self.client.room_send(
                room.room_id,
                "m.room.message",
                {
                    "msgtype": "m.text",
                    "body": f"✅ Style updated to preset: {option}"
                }
            )
        elif option == "custom":
            custom_spec = body[body.lower().find("custom") + 6:].strip()
            
            if custom_spec.startswith("```"):
                custom_spec = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", custom_spec)
                custom_spec = re.sub(r"\n?```$", "", custom_spec)
                custom_spec = custom_spec.strip()

            if not custom_spec or "canvas" not in custom_spec:
                await self.client.room_send(
                    room.room_id,
                    "m.room.message",
                    {
                        "msgtype": "m.text",
                        "body": "Error: Custom layout must start with a `canvas` directive setup."
                    }
                )
                return

            set_user_style(sender, custom_spec)
            await self.client.room_send(
                room.room_id,
                "m.room.message",
                {
                    "msgtype": "m.text",
                    "body": "🎨 Custom style set successfully. Run `!fm` to test your look!"
                }
            )
        else:
            await self.client.room_send(
                room.room_id,
                "m.room.message",
                {
                    "msgtype": "m.text",
                    "body": f"Unknown style preset '{option}'. Standard choices are: {', '.join(STYLE_PRESETS.keys())}"
                }
            )


if __name__ == "__main__":
    if not CONFIG["LASTFM_API_KEY"]:
        logger.error("LASTFM_API_KEY is required to run!")
        exit(1)

    if not CONFIG["ACCESS_TOKEN"] and not CONFIG["PASSWORD"]:
        logger.error("Matrix authentication is missing! Set access token or password.")
        exit(1)

    bot = BetterFMBot(CONFIG)
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        logger.info("Bot shutting down gracefully.")
