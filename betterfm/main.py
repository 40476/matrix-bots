#!/usr/bin/env python3
"""
betterFM - A secure, highly customizable Matrix Last.fm Bot.
Features:
- Now playing cards (!fm) with dynamic PIL-rendered image generation.
- Dynamic weekly/daily scrobble stats (!fmstats).
- Custom database to pair Matrix IDs with Last.fm accounts (!setuser).
- Configurable style presets and a secure, sandboxed custom canvas rendering language (!setstyle / custom layout definitions).
- Animated, looping GIF style presets (pulsing glow, spinning vinyl label, bouncing equalizer,
  neon ring, CRT scanline sweep) plus generic per-image `rotate=`/`mask=circle` support and a
  `---FRAME---` directive so custom styles can define their own multi-frame animations too.
- Control-flow scripting in the canvas DSL: `loop N ... endloop`, `if <a> <op> <b> ... endif`,
  and arithmetic `{...}` expressions over `{i}` (loop index) and `{f}` (frame index) - all
  parsed by a tiny sandboxed evaluator (no eval), enabling complex generated effects.
- "Who Knows" leaderboard (!bwk / !bwhoknows): ranks every user registered with this bot by
  how many times they've scrobbled a given artist of all time (defaulting to the caller's own
  top artist), always surfacing the caller's own rank even outside the top 10.
- Wiki/bio lookup (!wiki): pulls a cleaned-up Last.fm wiki summary for a track (falling back to
  the artist's biography), defaulting to the caller's current or most recent track.
- Extremely secure layout parsing engine (no eval, no path traversals, strict validation).
- Interactive first-time CLI setup wizard with config file generation.
- Automated room invite-joining mechanism with retry safety logic.
- Robust, keyless 5-tier fallback cover art fetcher (iTunes, Deezer, MusicBrainz CAA,
  TheAudioDB, and Odesli/song.link thumbnails) when Last.fm is missing artwork.
- Smart split-artist queries: tries individual artists separately if combined group query fails to yield artwork.
- Automatic music link resolver (YouTube, Spotify, Apple Music) that links directly to the
  actual track via the free Odesli/song.link API, and gracefully falls back to a search link
  on each platform when a direct match can't be found.
- Double-lookup iTunes reliability: automatically extracts track ID from song URL and retries via Lookup API for accurate art.
- Relative Last Active/Activity tracking notice ({activity}) built into rendering pipeline.
- Proper alpha channel compositing for beautiful glassmorphism and frosted glass blends.
- Customizable command triggers rebindable via config.json (to prevent conflict with other bots).
- Dynamic, auto-adjusting Help Menu.

Dependencies:
    pip install matrix-nio pillow requests aiohttp
"""

import os
import re
import json
import time
import math
import html
import asyncio
import logging
from io import BytesIO
from typing import Dict, Any, Tuple, Optional, Union, List
from urllib.parse import quote, quote_plus

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
        "cache_dir": "./cache",
        "cmd_fm": "!fm",
        "cmd_stats": "!fmstats",
        "cmd_setuser": "!setuser",
        "cmd_setstyle": "!setstyle",
        "cmd_help": "!fmhelp",
        "cmd_bwk": "!bwk",
        "cmd_bwhoknows": "!bwhoknows",
        "cmd_wiki": "!wiki"
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
    # Rebindable trigger commands configurations
    "CMD_FM": FILE_CONFIG.get("cmd_fm", "!fm").strip().lower(),
    "CMD_STATS": FILE_CONFIG.get("cmd_stats", "!fmstats").strip().lower(),
    "CMD_SETUSER": FILE_CONFIG.get("cmd_setuser", "!setuser").strip().lower(),
    "CMD_SETSTYLE": FILE_CONFIG.get("cmd_setstyle", "!setstyle").strip().lower(),
    "CMD_HELP": FILE_CONFIG.get("cmd_help", "!fmhelp").strip().lower(),
    "CMD_BWK": FILE_CONFIG.get("cmd_bwk", "!bwk").strip().lower(),
    "CMD_BWHOKNOWS": FILE_CONFIG.get("cmd_bwhoknows", "!bwhoknows").strip().lower(),
    "CMD_WIKI": FILE_CONFIG.get("cmd_wiki", "!wiki").strip().lower(),
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


# --- Wiki text cleanup helper ---
_WIKI_TAG_RE = re.compile(r"<[^>]+>")

def clean_wiki_text(raw: str, max_len: int = 600) -> str:
    """
    Strips Last.fm's embedded HTML from wiki/bio text (including the trailing
    '<a href="...">Read more on Last.fm</a>' boilerplate every wiki blob ships with),
    unescapes HTML entities, and truncates to a reasonable chat-friendly length.
    """
    if not raw:
        return ""
    text = _WIKI_TAG_RE.sub("", raw)
    text = html.unescape(text).strip()
    if len(text) > max_len:
        truncated = text[:max_len].rsplit(" ", 1)[0]
        text = truncated + "…"
    return text


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

    @staticmethod
    def split_artists(artist_str: str) -> list:
        """Splits complex combined group/featured artists into individual tokens."""
        # Split by comma, ampersand, 'and', 'feat.', 'ft.', slash, plus, with optional surrounding whitespace
        tokens = re.split(r'\s*(?:,|&|\band\b|\bfeat\b|\bfeat\.\b|\bft\.\b|\bft\b|/|\+)\s*', artist_str, flags=re.IGNORECASE)
        # Filter out empty items
        cleaned = [t.strip() for t in tokens if t.strip()]
        return cleaned

    async def _fetch_itunes(self, search_term: str) -> Optional[str]:
        """Queries iTunes Search API (Fallback Tier 1)."""
        url = f"https://itunes.apple.com/search?term={quote_plus(search_term)}&entity=song&limit=1"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, timeout=4) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        results = data.get("results", [])
                        if results:
                            art_url = results[0].get("artworkUrl100", "")
                            if art_url:
                                return art_url.replace("100x100bb", "600x600bb")
            except Exception as e:
                logger.warning(f"iTunes fallback fetch failed: {e}")
        return None

    async def _fetch_deezer(self, search_term: str) -> Optional[str]:
        """Queries Deezer Public Search API (Fallback Tier 2)."""
        url = f"https://api.deezer.com/search?q={quote_plus(search_term)}&limit=1"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, timeout=4) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        results = data.get("data", [])
                        if results:
                            album = results[0].get("album", {})
                            art_url = album.get("cover_xl") or album.get("cover_big")
                            if art_url:
                                return art_url
            except Exception as e:
                logger.warning(f"Deezer fallback fetch failed: {e}")
        return None

    async def _fetch_musicbrainz(self, artist: str, album_name: str) -> Optional[str]:
        """Queries MusicBrainz API & Cover Art Archive (Fallback Tier 3)."""
        if not album_name or album_name.lower() == "unknown album":
            return None
            
        mb_query = f'artist:"{artist}" AND release:"{album_name}"'
        url = f"https://musicbrainz.org/ws/2/release?query={quote_plus(mb_query)}&fmt=json"
        headers = {"User-Agent": "betterFM-Matrix-Bot (https://github.com/40476/matrix-bots/tree/main/betterfm)"}
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=headers, timeout=4) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        releases = data.get("releases", [])
                        if releases:
                            mbid = releases[0].get("id")
                            if mbid:
                                # Query the free Cover Art Archive
                                caa_url = f"https://coverartarchive.org/release/{mbid}"
                                async with session.get(caa_url, timeout=4) as caa_resp:
                                    if caa_resp.status == 200:
                                        caa_data = await caa_resp.json(content_type=None)
                                        images = caa_data.get("images", [])
                                        if images:
                                            return images[0].get("image")
            except Exception as e:
                logger.warning(f"MusicBrainz Cover Art Archive fallback failed: {e}")
        return None

    async def _fetch_theaudiodb(self, artist: str, album_name: str, title: str) -> Optional[str]:
        """Queries TheAudioDB's public API (Fallback Tier 4). Tries an album lookup
        first (best quality artwork), then falls back to a track lookup if that
        misses (covers singles / tracks without a proper album tag)."""
        base = "https://www.theaudiodb.com/api/v1/json/2"

        async with aiohttp.ClientSession() as session:
            if album_name and album_name.lower() != "unknown album":
                try:
                    url = f"{base}/searchalbum.php?s={quote_plus(artist)}&a={quote_plus(album_name)}"
                    async with session.get(url, timeout=4) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            albums = data.get("album") or []
                            if albums:
                                thumb = albums[0].get("strAlbumThumb")
                                if thumb:
                                    return thumb
                except Exception as e:
                    logger.warning(f"TheAudioDB album lookup failed: {e}")

            try:
                url = f"{base}/searchtrack.php?s={quote_plus(artist)}&t={quote_plus(title)}"
                async with session.get(url, timeout=4) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        tracks = data.get("track") or []
                        if tracks:
                            thumb = tracks[0].get("strTrackThumb") or tracks[0].get("strAlbumThumb")
                            if thumb:
                                return thumb
            except Exception as e:
                logger.warning(f"TheAudioDB track lookup failed: {e}")
        return None

    async def _fetch_odesli(self, source_url: str) -> Optional[Dict[str, Any]]:
        """Queries the free Odesli/song.link API (Fallback Tier 5 for art, and the
        primary resolver for direct cross-platform song links). Given any single
        known streaming URL (we feed it the resolved Apple Music track link), it
        returns the matching track URL on Spotify, YouTube, etc, plus a thumbnail
        that can be used as a last-resort artwork fallback."""
        url = f"https://api.song.link/v1-alpha.1/links?url={quote_plus(source_url)}&userCountry=US"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, timeout=6) as resp:
                    if resp.status == 200:
                        return await resp.json(content_type=None)
                    else:
                        logger.warning(f"Odesli/song.link returned status {resp.status}")
            except Exception as e:
                logger.warning(f"Odesli/song.link resolution failed: {e}")
        return None

    async def resolve_metadata(self, artist: str, title: str, album: str, album_art_url: Optional[str]) -> Tuple[Optional[str], Dict[str, str]]:
        """
        An advanced fallback pipeline trying multiple public APIs to resolve cover
        art (including individual split artist retry and a reliable direct iTunes
        lookup ID retry loop) and to resolve direct per-platform streaming links,
        falling back to a plain search link on any platform it can't match.
        """
        clean_artist = re.sub(r"[^\w\s\-]", "", artist)
        clean_title = re.sub(r"[^\w\s\-]", "", title)
        clean_album = re.sub(r"[^\w\s\-]", "", album) if album else ""
        
        # Build standard fallbacks (search fallback links). These are always valid
        # and are what we ship if a direct match can't be found for that platform.
        search_query_encoded = quote_plus(f"{artist} - {title}")
        links = {
            "youtube": f"https://www.youtube.com/results?search_query={search_query_encoded}",
            # NOTE: open.spotify.com/search/<query> takes the query as a URL *path*
            # segment, not a query-string parameter, so spaces must be percent-encoded
            # (%20) via quote(), NOT quote_plus() which produces literal '+' characters
            # that Spotify doesn't decode as spaces - that was why these links 404'd.
            "spotify": f"https://open.spotify.com/search/{quote(f'{artist} {title}')}",
            "apple": f"https://music.apple.com/us/search?term={search_query_encoded}"
        }
        
        resolved_art = album_art_url
        direct_apple_url = None
        
        # Build search term for first-pass
        search_query = f"{clean_artist} {clean_title}"
        if clean_album and clean_album.lower() != "unknown album":
            search_query += f" {clean_album}"

        # Consolidate first-pass requests to fetch fallback artwork AND direct links
        async with aiohttp.ClientSession() as session:
            # 1. Query iTunes Search (returns direct Apple Music links!)
            try:
                itunes_url = f"https://itunes.apple.com/search?term={quote_plus(search_query)}&entity=song&limit=1"
                async with session.get(itunes_url, timeout=4) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        results = data.get("results", [])
                        if results:
                            # Direct Apple Music/iTunes track url found!
                            direct_apple = results[0].get("trackViewUrl")
                            if direct_apple:
                                links["apple"] = direct_apple
                                direct_apple_url = direct_apple
                                
                                # Extract track ID to run direct lookup retry for top-tier reliability
                                track_id_match = re.search(r"[?&]i=(\d+)", direct_apple) or re.search(r"/id(\d+)", direct_apple)
                                if track_id_match:
                                    track_id = track_id_match.group(1)
                                    logger.info(f"Direct Apple Music Track ID {track_id} found. Fetching via exact lookup API...")
                                    lookup_url = f"https://itunes.apple.com/lookup?id={track_id}"
                                    async with session.get(lookup_url, timeout=4) as lookup_resp:
                                        if lookup_resp.status == 200:
                                            lookup_data = await lookup_resp.json(content_type=None)
                                            lookup_results = lookup_data.get("results", [])
                                            if lookup_results:
                                                # Exact match direct lookup successful!
                                                raw_art = lookup_results[0].get("artworkUrl100", "")
                                                if raw_art:
                                                    resolved_art = raw_art.replace("100x100bb", "600x600bb")
                                                    logger.info("Successfully fetched verified direct artwork via iTunes Lookup API!")
                                
                            # Fallback if track ID query was not processed or failed
                            if not resolved_art:
                                raw_art = results[0].get("artworkUrl100", "")
                                if raw_art:
                                    resolved_art = raw_art.replace("100x100bb", "600x600bb")
            except Exception as e:
                logger.warning(f"iTunes query pass failed: {e}")
                
            # 2. Query Deezer (extremely reliable global search)
            try:
                deezer_url = f"https://api.deezer.com/search?q={quote_plus(search_query)}&limit=1"
                async with session.get(deezer_url, timeout=4) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        results = data.get("data", [])
                        if results:
                            if not resolved_art:
                                raw_art = results[0].get("album", {}).get("cover_xl") or results[0].get("album", {}).get("cover_big")
                                if raw_art:
                                    resolved_art = raw_art
            except Exception as e:
                logger.warning(f"Deezer query pass failed: {e}")

        # 3. Resolve real per-platform song links (Spotify/YouTube/Apple) via Odesli,
        # anchored off the direct Apple Music track URL we just found. Whatever it
        # can't match, the pre-built search links from above are left standing.
        if direct_apple_url:
            odesli_data = await self._fetch_odesli(direct_apple_url)
            if odesli_data:
                platforms = odesli_data.get("linksByPlatform", {}) or {}

                spotify_entry = platforms.get("spotify")
                if spotify_entry and spotify_entry.get("url"):
                    links["spotify"] = spotify_entry["url"]
                    logger.info("Resolved direct Spotify track link via Odesli.")

                yt_entry = platforms.get("youtube") or platforms.get("youtubeMusic")
                if yt_entry and yt_entry.get("url"):
                    links["youtube"] = yt_entry["url"]
                    logger.info("Resolved direct YouTube link via Odesli.")

                apple_entry = platforms.get("appleMusic") or platforms.get("itunes")
                if apple_entry and apple_entry.get("url"):
                    links["apple"] = apple_entry["url"]

                # Use Odesli's thumbnail as an extra art fallback tier if we still
                # don't have artwork at this point.
                if not resolved_art:
                    thumb = odesli_data.get("thumbnailUrl")
                    if thumb:
                        resolved_art = thumb
                        logger.info("Using Odesli thumbnail as fallback artwork.")

        # If combined search fails to find artwork, split combined group artist strings and try separately
        if not resolved_art:
            artists_list = self.split_artists(artist)
            if len(artists_list) > 1:
                logger.info(f"Main combined artist artwork failed. Trying split individual artists separately: {artists_list}")
                for split_art in artists_list:
                    split_query = f"{split_art} {clean_title}"
                    if clean_album and clean_album.lower() != "unknown album":
                        split_query += f" {clean_album}"
                    
                    # Sequential keyless fallback tries on split artist
                    resolved_art = await self._fetch_itunes(split_query)
                    if resolved_art:
                        logger.info(f"Successfully resolved fallback artwork using split artist: {split_art}")
                        break
                        
                    resolved_art = await self._fetch_deezer(split_query)
                    if resolved_art:
                        logger.info(f"Successfully resolved fallback artwork using split artist: {split_art}")
                        break
                        
                    resolved_art = await self._fetch_musicbrainz(split_art, clean_album)
                    if resolved_art:
                        logger.info(f"Successfully resolved fallback artwork using split artist: {split_art}")
                        break

                    resolved_art = await self._fetch_theaudiodb(split_art, clean_album, clean_title)
                    if resolved_art:
                        logger.info(f"Successfully resolved fallback artwork using split artist: {split_art}")
                        break

        # Fallback to MusicBrainz main combined query as further effort
        if not resolved_art:
            resolved_art = await self._fetch_musicbrainz(clean_artist, clean_album)

        # Final effort: TheAudioDB combined query
        if not resolved_art:
            resolved_art = await self._fetch_theaudiodb(clean_artist, clean_album, clean_title)

        return resolved_art, links

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

        # Unified fallbacks resolve pass (Artwork fallbacks & Music links extraction)
        resolved_art, links = await self.resolve_metadata(artist_name, track_title, album_name, album_art_url or None)

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
            "album_art": resolved_art,
            "activity": activity,
            "links": links
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

    async def get_top_artist(self, username: str, period: str = "overall") -> Optional[str]:
        """Returns the display name of a user's #1 most-played artist (all-time by default)."""
        data = await self._fetch({
            "method": "user.gettopartists",
            "user": username,
            "period": period,
            "limit": "1"
        })
        if not data or "topartists" not in data:
            return None
        artists = data["topartists"].get("artist", [])
        if isinstance(artists, dict):
            artists = [artists]
        if not artists:
            return None
        return artists[0].get("name")

    async def get_artist_playcount(self, username: str, artist: str) -> int:
        """
        Returns how many times `username` has scrobbled `artist` across their whole
        library (all-time). Backs the !bwk / !bwhoknows leaderboard.

        Uses artist.getInfo with a `username`, which returns the clean all-time
        per-user play count (`artist.stats.userplaycount`). This is much more
        reliable than the old approach of paging user.getArtistTracks with a
        startTimestamp filter - which frequently returned no/zero data and left
        the leaderboard looking dead. Falls back to getArtistTracks' @attr.total
        only if the preferred field is ever missing.
        """
        data = await self._fetch({
            "method": "artist.getinfo",
            "artist": artist,
            "username": username,
        })
        if data and "artist" in data:
            stats = data["artist"].get("stats", {}) if isinstance(data["artist"], dict) else {}
            userplays = stats.get("userplaycount")
            if userplays is not None:
                try:
                    return int(userplays)
                except (TypeError, ValueError):
                    pass

        # Defensive fallback: all-time total from user.getArtistTracks (no time filter).
        data = await self._fetch({
            "method": "user.getartisttracks",
            "user": username,
            "artist": artist,
        })
        if not data:
            return 0

        block = data.get("artisttracks", {})
        attr = block.get("@attr", {}) if isinstance(block, dict) else {}
        total = attr.get("total")
        if total is not None:
            try:
                return int(total)
            except (TypeError, ValueError):
                pass

        # Last-resort defensive fallback if the API ever omits the @attr.total summary field
        tracks = block.get("track", []) if isinstance(block, dict) else []
        if isinstance(tracks, dict):
            tracks = [tracks]
        return len(tracks)

    async def get_wiki(self, artist: str, title: Optional[str] = None) -> Optional[Dict[str, str]]:
        """
        Fetches a wiki entry for a track. Most individual tracks don't have their own wiki
        on Last.fm, so if `track.getInfo` comes back empty this falls back to the artist's
        biography instead, so !wiki always has a decent shot at returning *something* useful.
        """
        if title:
            data = await self._fetch({
                "method": "track.getinfo",
                "artist": artist,
                "track": title,
            })
            if data and "track" in data:
                track = data["track"]
                wiki = track.get("wiki") or {}
                text = wiki.get("summary") or wiki.get("content")
                if text:
                    track_artist = track.get("artist", {})
                    artist_name = track_artist.get("name") if isinstance(track_artist, dict) else artist
                    return {
                        "subject": f"{artist_name} - {track.get('name', title)}",
                        "text": text,
                        "url": track.get("url", ""),
                    }

        # Fallback: the artist's own biography
        data = await self._fetch({
            "method": "artist.getinfo",
            "artist": artist,
        })
        if data and "artist" in data:
            art = data["artist"]
            bio = art.get("bio") or {}
            text = bio.get("summary") or bio.get("content")
            if text:
                return {
                    "subject": art.get("name", artist),
                    "text": text,
                    "url": art.get("url", ""),
                }

        return None


# --- SECURE CANVAS / RENDERING ENGINE Presets ---
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


def _build_animated_style_presets() -> Dict[str, str]:
    """
    Programmatically builds a few looping, animated GIF style presets.

    Animated styles are written in the same tiny DSL as static ones, just split into
    frames with a literal '---FRAME---' marker line. Everything *before* the first
    marker is shared "header" content (canvas size, an optional `delay <ms>` line,
    and any static text/background) that gets redrawn underneath every single frame;
    everything after each marker is that frame's own extra directives layered on top.
    """
    presets: Dict[str, str] = {}

    # --- Pulsing neon glow ring, with the cover art perfectly centred on the frame ---
    pulse_header = (
        "canvas 560 620 #0e0e12\n"
        "delay 60\n"
        "text 150 475 {artist} #ffffff 28 bold\n"
        "text 150 520 {title} #39ff88 22 bold\n"
        "text 150 562 {album} #9aa0aa 16 italic\n"
        "text 280 600 {activity} #7d8590 13\n"
    )
    # 16 frames of glow breathing behind a fixed, centred art block (box 150-410, centre x=280).
    glow_alphas = [40, 90, 150, 210, 255, 210, 150, 90] * 2
    pulse_frames = [
        "rect 124 124 436 436 #39ff88%02x\n" % a
        + "rect 150 150 410 410 #191922\n"
        + "image 150 150 260 260 {album_art}\n"
        for a in glow_alphas
    ]
    presets["pulse_glow_gif"] = pulse_header + "".join(f"---FRAME---\n{f}" for f in pulse_frames)

    # --- Spinning center label, vinyl-style (art masked to a clean disc + centre spindle) ---
    vinyl_header = (
        "canvas 400 460 #0a0a0c\n"
        "delay 50\n"
        "ellipse 40 40 360 360 #16161a\n"
        "ellipse 100 100 300 300 #1e1e22\n"
        "ellipse 165 165 235 235 #000000\n"
        "text 40 380 {artist} #ffffff 22 bold\n"
        "text 40 412 {title} #39d0ff 17 bold\n"
        "text 40 440 {activity} #8a8f98 12\n"
    )
    # 24 frames at 50ms (~20fps). mask=circle keeps the rotating art inside the label ring
    # (no more square corners chopping into the grooves), and a spindle hole + shine on top.
    spin_frames = []
    for deg in range(0, 360, 15):
        spin_frames.append(
            f"image 165 165 70 70 {{album_art}} rotate={deg} mask=circle\n"
            "ellipse 192 192 208 208 #0a0a0c\n"
            "ellipse 197 197 203 203 #ffffff55\n"
        )
    presets["vinyl_spin_gif"] = vinyl_header + "".join(f"---FRAME---\n{f}" for f in spin_frames)

    # --- Bouncing equalizer bars, higher framerate (12 bars-frames at 50ms) ---
    eq_header = (
        "canvas 1000 260 #0d0d12\n"
        "delay 20\n"
        "rect 20 20 200 200 #1a1a22\n"
        "image 20 20 200 200 {album_art}\n"
        "text 240 40 {artist} #ffffff 24 bold\n"
        "text 240 78 {title} #ff5fa2 19 bold\n"
        "text 240 112 {album} #9aa0aa 14 italic\n"
        "text 240 220 {activity} #7d8590 12\n"
    )

    start_x = 240
    total_width = 720  
    num_bars = 16      
    bar_width = 14     

    available_space = total_width - (num_bars * bar_width)
    spacing = available_space / (num_bars - 1) if num_bars > 1 else 0

    bar_x_positions = [int(start_x + i * (bar_width + spacing)) for i in range(num_bars)]

    n_frames = 250
    eq_frames = []

    for f in range(n_frames):
        bar_lines = ""
        for i, x in enumerate(bar_x_positions):
            height = int(15 + 48 * abs(math.sin((f / n_frames) * 2 * math.pi + i * 0.8)))
            y0 = 200 - height
            # Fixed: passed x + bar_width for x1, and 200 for y1
            bar_lines += f"rect {x} {y0} {x + bar_width} 200 #ff5fa2\n"
        eq_frames.append(bar_lines)

    presets["equalizer_gif"] = eq_header + "".join(f"---FRAME---\n{f}" for f in eq_frames)

    # --- Neon ring: breathing double halo with cycling colours around centred art ---
    ring_header = (
        "canvas 560 620 #07070b\n"
        "delay 40\n"
        "text 150 470 {artist} #ffffff 28 bold\n"
        "text 150 515 {title} #5ce8ff 22 bold\n"
        "text 150 557 {album} #9aa0aa 16 italic\n"
        "text 280 595 {activity} #7d8590 13\n"
    )
    ring_colors = ["#ff3366", "#ff9933", "#ffdd00", "#66ff33", "#33ffcc", "#33ccff", "#6644ff", "#cc33ff"]
    n_ring_frames = 24
    ring_frames = []
    for k in range(n_ring_frames):
        phase = (k / n_ring_frames) * 2 * math.pi
        color = ring_colors[k % len(ring_colors)]
        a1 = int(120 + 80 * math.sin(phase))
        a2 = int(120 - 80 * math.sin(phase))
        s1 = int(12 * math.sin(phase))
        s2 = int(-12 * math.sin(phase))
        # Two concentric pulsing frames centred on the art block (150-410, centre 280).
        ring_frames.append(
            f"rect {150-s1*2} {150-s1*2} {410+s1*2} {410+s1*2} {color}{a1:02x}\n"
            f"rect {150-s2*2} {150-s2*2} {410+s2*2} {410+s2*2} #0d0d18\n"
            f"image 150 150 260 260 {{album_art}}\n"
        )
    presets["neon_ring_gif"] = ring_header + "".join(f"---FRAME---\n{f}" for f in ring_frames)

    # --- CRT scanline sweep: blurred backdrop + indicator + soft scanlines (loop) + a bright
    #     head that sweeps down using {f}; demonstrates the new loop/if/expression scripting ---
    crt_header = (
        "canvas 560 320 #0a0a0e\n"
        "delay 40\n"
        "image 0 0 560 320 {album_art} 18\n"
        "rect 0 0 560 320 #00000060\n"
        "image 34 34 212 212 {album_art}\n"
        "text 264 40 {artist} #ffffff 24 bold\n"
        "text 264 78 {title} #ff8a3d 19 bold\n"
        "loop 40\n"
        "  rect 0 {i*8} 560 {i*8+1} #0000001e\n"
        "endloop\n"
    )
    n_crt_frames = 16
    crt_frames = []
    for f in range(n_crt_frames):
        y = f * 20
        crt_frames.append(
            f"rect 0 {y} 560 {y + 12} #6bff9f\n"
            f"rect 0 {y + 13} 560 {y + 14} #ffffff\n"
        )
    presets["scanline_crt_gif"] = crt_header + "".join(f"---FRAME---\n{f}" for f in crt_frames)

    return presets


STYLE_PRESETS.update(_build_animated_style_presets())


class SecureRenderer:
    HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3,4}){1,2}$")
    # Case-insensitive marker line that splits an animated style spec into frames.
    FRAME_SEPARATOR = "---frame---"

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
    def is_animated(cls, style_spec: str) -> bool:
        """An style spec is animated if it contains at least one '---FRAME---' marker line."""
        return cls.FRAME_SEPARATOR in style_spec.lower()

    @classmethod
    def _extract_canvas_size(cls, lines: List[str]) -> Tuple[int, int, Tuple[int, int, int, int]]:
        canvas_width, canvas_height = 800, 250
        bg_color = (30, 30, 36, 255)
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("canvas"):
                parts = stripped.split()
                if len(parts) >= 3:
                    try:
                        canvas_width = min(max(int(parts[1]), 100), 1200)
                        canvas_height = min(max(int(parts[2]), 100), 800)
                        if len(parts) >= 4:
                            bg_color = cls.parse_color(parts[3])
                    except ValueError:
                        pass
                break
        return canvas_width, canvas_height, bg_color

    @classmethod
    def _extract_delay(cls, lines: List[str]) -> int:
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("delay"):
                parts = stripped.split()
                if len(parts) >= 2:
                    try:
                        return min(max(int(parts[1]), 5), 2000)
                    except ValueError:
                        pass
        return 120

# Known metadata placeholder keys - these are left untouched by the math expression
    # substitution below (they get replaced later by the text/image handlers).
    KNOWN_TOKENS = frozenset(["album_art", "artist", "title", "album", "username", "activity"])

    @classmethod
    def _expand_control(cls, lines: List[str], env: Dict[str, Any]) -> List[str]:
        """
        Expands the DSL's control-flow sugar into plain drawing directives:

            loop <N>
                ... directives (may use {i} and arithmetic like {i*40})
            endloop

            if <expr> <op> <expr>
                ... directives (run only when the comparison is true)
            endif

        Each `<expr>` is an integer arithmetic expression that may contain the
        loop index `i` and/or the frame index `f`, with `+ - * / % ( )` and also
        braces, e.g. `if {i} % 4 == 0` or `if {f%2}` ... `<op>` is one of
        `== != < <= > >=`. With no operator the whole expression is treated as a
        truth value (non-zero runs the body). Loops and ifs may be nested.
        Everything is parsed by a tiny sandboxed evaluator - never eval() - so
        the environment stays safe.
        """
        out: List[str] = []
        i = 0
        n = len(lines)
        while i < n:
            line = lines[i]
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                out.append(line)
                i += 1
                continue

            low = stripped.lower()
            if low.startswith("loop "):
                parts = stripped.split()
                try:
                    count = max(0, min(int(parts[1]), 1000))
                except (ValueError, IndexError):
                    count = 0
                block, i = cls._grab_block(lines, i, "loop")
                for it in range(count):
                    inner_env = dict(env)
                    inner_env["i"] = it
                    out.extend(cls._expand_control(block, inner_env))
                continue

            if low.startswith("if "):
                block, i = cls._grab_block(lines, i, "if")
                cond_tokens = stripped.split()[1:]
                # Find the comparison operator among the condition tokens.
                op_idx = None
                _COMPARE_OPS = ("==", "!=", "<=", ">=", "<", ">")
                for _idx, _tok in enumerate(cond_tokens):
                    if _tok in _COMPARE_OPS:
                        op_idx = _idx
                        break
                take = False
                if op_idx is None:
                    # No operator - the whole expression is a truth value.
                    take = cls._eval_expr("".join(cond_tokens), env) != 0 if cond_tokens else False
                elif op_idx + 1 < len(cond_tokens):
                    left = "".join(cond_tokens[:op_idx])
                    op = cond_tokens[op_idx]
                    right = "".join(cond_tokens[op_idx + 1:])
                    take = cls._compare(cls._eval_expr(left, env), op, cls._eval_expr(right, env))
                if take:
                    out.extend(cls._expand_control(block, env))
                continue

            # Stray endloop/endif - drop it defensively (should already be consumed).
            if low == "endloop" or low == "endif":
                i += 1
                continue

            out.append(cls._substitute_exprs(lines[i], env))
            i += 1
        return out

    @classmethod
    def _grab_block(cls, lines: List[str], start: int, kind: str) -> Tuple[List[str], int]:
        """
        Collects the body of a `loop`/`if` block opened at `lines[start]`, handling
        arbitrary nesting, and returns (body_lines, index_after_closer).
        """
        depth = 0
        j = start
        body: List[str] = []
        while j < len(lines):
            low = lines[j].strip().lower()
            if low.startswith("loop ") or low.startswith("if "):
                depth += 1
            if low == "endloop" or low == "endif":
                depth -= 1
                if depth <= 0:
                    return body, j + 1
            if j != start:
                body.append(lines[j])
            j += 1
        # Unclosed block: treat the rest of the script as the body (defensive).
        return body, len(lines)

    @classmethod
    def _substitute_exprs(cls, text: str, env: Dict[str, Any]) -> str:
        """Replaces `{...}` arithmetic expressions (over i/f) with their integer values."""

        def repl(match):
            inner = match.group(1).strip()
            if inner in cls.KNOWN_TOKENS:
                return match.group(0)  # leave metadata placeholders alone
            try:
                return str(cls._eval_expr(inner, env))
            except Exception:
                return match.group(0)  # leave anything we can't parse untouched

        return re.sub(r"\{([^{}]*)\}", repl, text)

    @classmethod
    def _tokenize_expr(cls, expr: str) -> List[Any]:
        tokens: List[Any] = []
        num = ""
        i = 0
        chars = list(expr)

        def flush():
            nonlocal num
            if num:
                tokens.append(int(num))
                num = ""

        while i < len(chars):
            c = chars[i]
            if c.isdigit():
                num += c
            elif c == "i" or c == "f":
                flush()
                tokens.append(c)
            elif c in "+-*/%()":
                flush()
                tokens.append(c)
            elif c.isspace():
                flush()
            i += 1
        flush()
        return tokens

    @classmethod
    def _eval_expr(cls, expr: str, env: Dict[str, Any]) -> int:
        """Evaluates a tiny integer arithmetic expression; the only variables are the
        loop index `i` and frame index `f` pulled from `env`. No eval, no builtins."""
        expr = (expr or "").strip()
        if not expr:
            return 0
        tokens = cls._tokenize_expr(expr)
        pos = [0]

        def peek():
            return tokens[pos[0]] if pos[0] < len(tokens) else None

        def advance():
            t = tokens[pos[0]]
            pos[0] += 1
            return t

        def primary():
            t = peek()
            if t is None:
                return 0
            if t in ("+", "-"):
                advance()
                v = primary()
                return v if t == "+" else -v
            if t == "(":
                advance()
                v = expression()
                if peek() == ")":
                    advance()
                return v
            if t == "i" or t == "f":
                advance()
                return int(env.get(t, 0))
            if isinstance(t, int):
                advance()
                return t
            advance()  # unknown token - skip it harmlessly
            return 0

        def term():
            value = primary()
            while peek() in ("*", "/", "%"):
                op = advance()
                rhs = primary()
                if op == "*":
                    value = value * rhs
                elif op == "%":
                    value = value % rhs if rhs else 0
                else:
                    value = value // rhs if rhs else 0
            return value

        def expression():
            value = term()
            while peek() in ("+", "-"):
                op = advance()
                rhs = term()
                value = value + rhs if op == "+" else value - rhs
            return value

        return expression()

    @classmethod
    def _compare(cls, left: int, op: str, right: int) -> bool:
        if op == "==":
            return left == right
        if op == "!=":
            return left != right
        if op == "<":
            return left < right
        if op == "<=":
            return left <= right
        if op == ">":
            return left > right
        if op == ">=":
            return left >= right
        return False

    @classmethod
    def _draw_frame(
        cls,
        lines: List[str],
        canvas_size: Tuple[int, int],
        bg_color: Tuple[int, int, int, int],
        replacements: Dict[str, str],
        album_art_img: Image.Image,
        frame_index: int = 0,
    ) -> Image.Image:
        """Draws one full frame's worth of directives onto a fresh RGBA canvas and returns it."""
        # Expand loop/if script sugar, exposing {f} (frame index) so scripts can animate.
        lines = cls._expand_control(lines, {"i": 0, "f": frame_index})
        img = Image.new("RGBA", canvas_size, bg_color)
        draw = ImageDraw.Draw(img)

        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("canvas") or line.startswith("delay"):
                continue

            parts = line.split()
            if not parts:
                continue
            cmd = parts[0].lower()

            try:
                if cmd == "rect" and len(parts) >= 6:
                    x0, y0, x1, y1 = map(int, parts[1:5])
                    color = cls.parse_color(parts[5])
                    # Handle true transparency alpha compositing to avoid overwrite artifacts
                    if len(color) == 4 and color[3] < 255:
                        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
                        overlay_draw = ImageDraw.Draw(overlay)
                        overlay_draw.rectangle([x0, y0, x1, y1], fill=color)
                        img = Image.alpha_composite(img, overlay)
                        draw = ImageDraw.Draw(img)  # Re-establish context
                    else:
                        draw.rectangle([x0, y0, x1, y1], fill=color)

                elif cmd == "ellipse" and len(parts) >= 6:
                    x0, y0, x1, y1 = map(int, parts[1:5])
                    color = cls.parse_color(parts[5])
                    # Handle true transparency alpha compositing to avoid overwrite artifacts
                    if len(color) == 4 and color[3] < 255:
                        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
                        overlay_draw = ImageDraw.Draw(overlay)
                        overlay_draw.ellipse([x0, y0, x1, y1], fill=color)
                        img = Image.alpha_composite(img, overlay)
                        draw = ImageDraw.Draw(img)  # Re-establish context
                    else:
                        draw.ellipse([x0, y0, x1, y1], fill=color)

                elif cmd == "image" and len(parts) >= 5:
                    x, y, w, h = map(int, parts[1:5])

                    # Search all remaining arguments securely for a blur radius (bare digits),
                    # a "rotate=<deg>" directive that powers the spinning-label styles, and/or
                    # a "mask=circle" directive that crops the art into a clean disc.
                    blur_val = 0
                    rotate_val = 0
                    mask_circle = False
                    for p in parts[5:]:
                        if p.isdigit():
                            blur_val = min(max(int(p), 0), 100)
                        elif p.lower().startswith("rotate="):
                            try:
                                rotate_val = int(p.split("=", 1)[1]) % 360
                            except ValueError:
                                pass
                        elif p.lower() == "mask=circle":
                            mask_circle = True

                    resized_art = album_art_img.resize((w, h), Image.Resampling.LANCZOS)

                    if rotate_val:
                        rotated = resized_art.rotate(rotate_val, resample=Image.Resampling.BICUBIC, expand=True)
                        # The rotated image's bounding box grows larger at diagonal angles.
                        # Downscale it to fit inside the target box instead of cropping with a
                        # negative offset - which used to chop the corners off spinning labels.
                        if rotated.width > w or rotated.height > h:
                            rotated.thumbnail((w, h), Image.Resampling.LANCZOS)
                        # Re-center the image back into a box the same size as the original
                        # target so the layout stays fixed.
                        centered = Image.new("RGBA", (w, h), (0, 0, 0, 0))
                        offset = ((w - rotated.width) // 2, (h - rotated.height) // 2)
                        centered.paste(rotated, offset, rotated)
                        resized_art = centered

                    if blur_val > 0:
                        resized_art = resized_art.filter(ImageFilter.GaussianBlur(blur_val))

                    if mask_circle:
                        # Crop into a perfect circle centred on the box - turns square album
                        # art into a clean vinyl-style label even while it rotates.
                        mask = Image.new("L", resized_art.size, 0)
                        ImageDraw.Draw(mask).ellipse(
                            [0, 0, resized_art.width - 1, resized_art.height - 1], fill=255
                        )
                        resized_art.putalpha(mask)

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
                                size = min(max(int(parts[color_idx + 1]), 8), 72)
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

        return img

    @classmethod
    async def render_card(cls, track_info: Dict[str, Any], style_spec: str) -> Tuple[BytesIO, str, str]:
        """
        Renders a now-playing card from a style spec.
        Returns (image_bytes, mimetype, file_extension) - a plain PNG for static styles,
        or an animated, infinitely-looping GIF for any style containing '---FRAME---' markers.
        """
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

        raw_lines = style_spec.split("\n")

        if not cls.is_animated(style_spec):
            canvas_width, canvas_height, bg_color = cls._extract_canvas_size(raw_lines)
            frame_img = cls._draw_frame(raw_lines, (canvas_width, canvas_height), bg_color, replacements, album_art_img)
            output = BytesIO()
            frame_img.save(output, format="PNG")
            output.seek(0)
            return output, "image/png", "png"

        # --- Animated path: split into a shared header + one or more per-frame blocks ---
        segments: List[List[str]] = [[]]
        for line in raw_lines:
            if line.strip().lower() == cls.FRAME_SEPARATOR:
                segments.append([])
            else:
                segments[-1].append(line)

        header_lines = segments[0]
        frame_blocks = segments[1:] if len(segments) > 1 else []

        canvas_width, canvas_height, bg_color = cls._extract_canvas_size(header_lines)
        delay_ms = cls._extract_delay(header_lines)

        rendered_frames = []
        if frame_blocks:
            for f_idx, frame_lines in enumerate(frame_blocks):
                combined = header_lines + frame_lines
                frame_img = cls._draw_frame(combined, (canvas_width, canvas_height), bg_color, replacements, album_art_img, frame_index=f_idx)
                rendered_frames.append(frame_img.convert("RGB"))
        else:
            # Defensive fallback: a marker with nothing after it just renders the header once.
            frame_img = cls._draw_frame(header_lines, (canvas_width, canvas_height), bg_color, replacements, album_art_img, frame_index=0)
            rendered_frames.append(frame_img.convert("RGB"))

        output = BytesIO()
        rendered_frames[0].save(
            output,
            format="GIF",
            save_all=True,
            append_images=rendered_frames[1:],
            duration=delay_ms,
            loop=0,
            disposal=2,
        )
        output.seek(0)
        return output, "image/gif", "gif"


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

        # Dynamic, configurable command mapping checks
        if cmd == CONFIG["CMD_FM"]:
            await self.handle_fm(room, event, parts)
        elif cmd == CONFIG["CMD_STATS"]:
            await self.handle_fm_stats(room, event, parts)
        elif cmd == CONFIG["CMD_SETUSER"]:
            await self.handle_set_user(room, event, parts)
        elif cmd == CONFIG["CMD_SETSTYLE"]:
            await self.handle_set_style(room, event, body)
        elif cmd == CONFIG["CMD_HELP"]:
            await self.handle_help(room)
        elif cmd == CONFIG["CMD_BWK"] or cmd == CONFIG["CMD_BWHOKNOWS"]:
            await self.handle_bwk(room, event, parts)
        elif cmd == CONFIG["CMD_WIKI"]:
            await self.handle_wiki(room, event, parts, body)

    async def upload_image_to_matrix(self, image_data: BytesIO, filename: str, mimetype: str = "image/png") -> Optional[str]:
        """Uploads a PIL generated image binary (PNG or GIF) onto Matrix media storage securely."""
        try:
            logger.info(f"Starting upload for {filename} ({image_data.getbuffer().nbytes} bytes)...")
            resp = await self.client.upload(
                image_data,
                mimetype,
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

    async def handle_help(self, room: MatrixRoom):
        """Sends a beautiful markdown help menu reflecting current trigger command configurations."""
        help_msg = (
            "📖 **betterFM Bot Help Menu**\n"
            "This bot shows what you are listening to on Last.fm!\n\n"
            "**Commands:**\n"
            f"- `{CONFIG['CMD_FM']} [user]` - Shows currently playing track with album cover card (defaults to you).\n"
            f"- `{CONFIG['CMD_STATS']} [user] [daily/monthly/overall]` - Fetches playstats and top tracks.\n"
            f"- `{CONFIG['CMD_SETUSER']} <lastfm_username>` - Links your Matrix ID to your Last.fm account.\n"
            f"- `{CONFIG['CMD_SETSTYLE']} <preset>` - Switches your design theme.\n"
            f"- `{CONFIG['CMD_SETSTYLE']} custom <directives>` - Saves a custom canvas design (add `---FRAME---` blocks for an animated GIF; script with `loop N ... endloop`, `if`/`endif`, and `{{i}}`/`{{f}}` `{{...}}` math for complex effects).\n"
            f"- `{CONFIG['CMD_BWK']} [artist]` (alias `{CONFIG['CMD_BWHOKNOWS']}`) - Who Knows leaderboard: ranks everyone registered with this bot by scrobbles of that artist (all-time). Leave the artist blank to use your own #1 artist.\n"
            f"- `{CONFIG['CMD_WIKI']} [artist] - [title]` - Looks up a short wiki entry for a track (falls back to the artist's bio). Leave blank to use your current/last track.\n"
            f"- `{CONFIG['CMD_HELP']}` - Shows this help menu.\n\n"
            "**Available Presets** (`_gif` presets are animated & loop):\n"
            f"`{', '.join(STYLE_PRESETS.keys())}`"
        )
        
        await self.client.room_send(
            room.room_id,
            "m.room.message",
            {
                "msgtype": "m.text",
                "body": help_msg.replace("**", ""),
                "format": "org.matrix.custom.html",
                "formatted_body": help_msg.replace("\n", "<br>")
            }
        )

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
                            "body": f"No Last.fm account bound to Matrix user: {possible_target}. Ask them to run {CONFIG['CMD_SETUSER']} <username>."
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
                        "body": f"Hi {sender}! You haven't registered your Last.fm profile with this bot. Please set it using: {CONFIG['CMD_SETUSER']} <lastfm_username>"
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

            image_stream, mimetype, ext = await SecureRenderer.render_card(track_info, style_spec)
            mxc_uri = await self.upload_image_to_matrix(image_stream, f"{target_lastfm}_fm.{ext}", mimetype)
            
            if mxc_uri:
                status_msg = "Currently playing:" if track_info["now_playing"] else "Last played track:"
                
                # Fetch Resolved direct URLs for music services (Spotify, YouTube, Apple Music)
                youtube_url = track_info["links"]["youtube"]
                spotify_url = track_info["links"]["spotify"]
                apple_url = track_info["links"]["apple"]
                
                # Sane plain-text fallback caption
                formatted_body = (
                    f"🎵 {status_msg} {track_info['artist']} - {track_info['title']} "
                    f"(Album: {track_info['album']}) [{target_lastfm}]\n"
                    f"Listen: YouTube: {youtube_url} | Spotify: {spotify_url} | Apple Music: {apple_url}"
                )
                
                # Premium HTML-styled caption with clickable hyperlinked services
                html_body = (
                    f"🎵 {status_msg} <b>{track_info['artist']}</b> - <b>{track_info['title']}</b> "
                    f"(<i>Album: {track_info['album']}</i>) [{target_lastfm}]<br/>"
                    f"▶️ Listen: <a href='{youtube_url}'>YouTube</a> | "
                    f"<a href='{spotify_url}'>Spotify</a> | "
                    f"<a href='{apple_url}'>Apple Music</a>"
                )
                
                # Send room message matching the client standard json structure (incorporating top level filename)
                await self.client.room_send(
                    room.room_id,
                    "m.room.message",
                    {
                        "msgtype": "m.image",
                        "body": formatted_body,
                        "url": mxc_uri,
                        "filename": f"{target_lastfm}_fm.{ext}",
                        "format": "org.matrix.custom.html",
                        "formatted_body": html_body,
                        "info": {
                            "mimetype": mimetype,
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
                    "body": f"No username registered. Please use {CONFIG['CMD_SETUSER']} <username> first."
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
                    "body": f"Usage: {CONFIG['CMD_SETUSER']} <lastfm_username>"
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
                    "body": f"Usage: {CONFIG['CMD_SETSTYLE']} <preset_name> OR {CONFIG['CMD_SETSTYLE']} custom <layout directives>\nAvailable Presets: {presets_list}"
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
                        "body": "Error: Custom layout must start with a `canvas` directive setup. (Add one or more `---FRAME---` lines to make it an animated GIF.)"
                    }
                )
                return

            set_user_style(sender, custom_spec)
            await self.client.room_send(
                room.room_id,
                "m.room.message",
                {
                    "msgtype": "m.text",
                    "body": f"🎨 Custom style set successfully. Run {CONFIG['CMD_FM']} to test your look!"
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

    async def handle_bwk(self, room: MatrixRoom, event: RoomMessageText, parts: list):
        """
        !bwk / !bwhoknows [artist] - Leaderboard of everyone registered with this bot,
        ranked by how many times each of them has scrobbled the given artist of all time.
        If no artist is given, defaults to the caller's own all-time #1 artist. Ties are
        broken in the caller's favor (their "highest valid spot"), and the caller's own rank
        is always shown even if they land outside the top 10.
        """
        sender = event.sender
        caller_lastfm = get_user_lastfm(sender)
        if not caller_lastfm:
            await self.client.room_send(
                room.room_id,
                "m.room.message",
                {
                    "msgtype": "m.text",
                    "body": f"You need to register first: {CONFIG['CMD_SETUSER']} <lastfm_username>"
                }
            )
            return

        if len(parts) > 1:
            artist_query = " ".join(parts[1:]).strip()
        else:
            artist_query = await self.lastfm.get_top_artist(caller_lastfm)
            if not artist_query:
                await self.client.room_send(
                    room.room_id,
                    "m.room.message",
                    {
                        "msgtype": "m.text",
                        "body": f"Couldn't determine your top artist. Try: {CONFIG['CMD_BWK']} <artist name>"
                    }
                )
                return

        await self.client.room_typing(room.room_id, True)
        try:
            db = load_db()
            users = db.get("users", {})

            # De-duplicate by Last.fm username, in case multiple Matrix IDs are bound to the same account.
            seen_lastfm: Dict[str, str] = {}
            for matrix_id, info in users.items():
                lfm = info.get("lastfm")
                if lfm and lfm not in seen_lastfm:
                    seen_lastfm[lfm] = matrix_id

            # Make sure the caller is always included, even if their own DB entry is somehow missing.
            if caller_lastfm not in seen_lastfm:
                seen_lastfm[caller_lastfm] = sender

            if not seen_lastfm:
                await self.client.room_send(
                    room.room_id,
                    "m.room.message",
                    {"msgtype": "m.text", "body": "No users are registered with this bot yet."}
                )
                return

            lastfm_names = list(seen_lastfm.keys())

            results = await asyncio.gather(
                *[self.lastfm.get_artist_playcount(name, artist_query) for name in lastfm_names],
                return_exceptions=True
            )

            leaderboard = []
            for lfm_name, count in zip(lastfm_names, results):
                if isinstance(count, Exception):
                    logger.warning(f"Failed fetching playcount for {lfm_name}: {count}")
                    count = 0
                leaderboard.append({
                    "lastfm": lfm_name,
                    "plays": count,
                    "is_caller": lfm_name == caller_lastfm,
                })

            # Sort by playcount, descending. Ties go to the caller first - their "highest valid spot".
            leaderboard.sort(key=lambda e: (-e["plays"], not e["is_caller"]))

            caller_rank = next((i + 1 for i, e in enumerate(leaderboard) if e["is_caller"]), None)
            caller_entry = next((e for e in leaderboard if e["is_caller"]), None)

            medals = {1: "🥇", 2: "🥈", 3: "🥉"}
            lines = [f"🏆 **Who Knows \"{artist_query}\"** (all-time)"]

            ranked = [e for e in leaderboard if e["plays"] > 0]
            top10 = ranked[:10]

            if not top10:
                lines.append(f"Nobody registered with this bot has scrobbled **{artist_query}** yet.")
            else:
                for idx, entry in enumerate(top10, start=1):
                    prefix = medals.get(idx, f"{idx}.")
                    you_tag = " (you)" if entry["is_caller"] else ""
                    lines.append(f"{prefix} **{entry['lastfm']}**{you_tag} — {entry['plays']} plays")

                if caller_entry and caller_entry not in top10:
                    lines.append("…")
                    lines.append(f"#{caller_rank}. **{caller_entry['lastfm']}** (you) — {caller_entry['plays']} plays")

            msg = "\n".join(lines)
            await self.client.room_send(
                room.room_id,
                "m.room.message",
                {
                    "msgtype": "m.text",
                    "body": msg.replace("**", ""),
                    "format": "org.matrix.custom.html",
                    "formatted_body": msg.replace("\n", "<br>")
                }
            )
        finally:
            await self.client.room_typing(room.room_id, False)

    async def handle_wiki(self, room: MatrixRoom, event: RoomMessageText, parts: list, body: str):
        """
        !wiki [artist] - [title] - Looks up a wiki entry for a track. Falls back to the
        artist's biography if the track itself has no wiki. With no arguments, uses the
        caller's current (or most recent) track.
        """
        sender = event.sender
        artist_query = None
        title_query = None

        if len(parts) > 1:
            # Re-slice from the raw message body so multi-word artist/title names survive intact.
            raw_query = body[len(parts[0]):].strip()
            if " - " in raw_query:
                artist_query, title_query = [p.strip() for p in raw_query.split(" - ", 1)]
            else:
                artist_query = raw_query
        else:
            caller_lastfm = get_user_lastfm(sender)
            if not caller_lastfm:
                await self.client.room_send(
                    room.room_id,
                    "m.room.message",
                    {
                        "msgtype": "m.text",
                        "body": f"Usage: {CONFIG['CMD_WIKI']} <artist> - <title>  (or register with {CONFIG['CMD_SETUSER']} and leave it blank to use your current track)"
                    }
                )
                return

            now_playing = await self.lastfm.get_now_playing(caller_lastfm)
            if not now_playing:
                await self.client.room_send(
                    room.room_id,
                    "m.room.message",
                    {"msgtype": "m.text", "body": "Couldn't find a current or recent track to look up."}
                )
                return
            artist_query = now_playing["artist"]
            title_query = now_playing["title"]

        await self.client.room_typing(room.room_id, True)
        try:
            wiki = await self.lastfm.get_wiki(artist_query, title_query)
            if not wiki:
                lookup_desc = f"{artist_query} - {title_query}" if title_query else artist_query
                await self.client.room_send(
                    room.room_id,
                    "m.room.message",
                    {"msgtype": "m.text", "body": f"No wiki entry found for {lookup_desc}."}
                )
                return

            clean_text = clean_wiki_text(wiki["text"])
            msg = f"📖 **{wiki['subject']}**\n\n{clean_text}"
            if wiki.get("url"):
                msg += f"\n\n🔗 {wiki['url']}"

            await self.client.room_send(
                room.room_id,
                "m.room.message",
                {
                    "msgtype": "m.text",
                    "body": msg.replace("**", ""),
                    "format": "org.matrix.custom.html",
                    "formatted_body": msg.replace("\n", "<br>")
                }
            )
        finally:
            await self.client.room_typing(room.room_id, False)


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
