#!/usr/bin/env python3
"""
Matrix Based Bot
================
A Matrix bot that listens for "!based <text>" replies, validates that the <text>
matches (case-insensitively) the message being replied to (with at most 1 word
added or removed, and ignoring emojis), downloads the target's profile picture,
and generates a single-frame demotivational style meme.

If the replied-to message is an image, it skips the text match and uses the
replied-to image instead of the user's profile picture.

The text is placed at the bottom in a beautiful, capitalized, centered serif font
resembling a printed book/ebook, and is the only text on the black canvas.
"""

import os
import sys
import json
import asyncio
import io
import re
import time
import urllib.request
import aiohttp
import logging
import markdown
from typing import Optional, List, Tuple
from PIL import Image, ImageDraw, ImageFont

# Import matrix-nio components
from nio import (
    AsyncClient,
    AsyncClientConfig,
    MatrixRoom,
    RoomMessageText,
    LoginResponse,
    InviteMemberEvent,
    UploadResponse,
    RoomSendResponse,
    RoomSendError
)

# ==============================================================================
# Logging and Quiet Settings
# ==============================================================================
# Configure standard logging to look clean in systemd journalctl
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

# Suppress matrix-nio validation warnings (e.g. legacy room predecessor events schema errors)
logging.getLogger("nio").setLevel(logging.ERROR)

# Configuration and Session Paths
CONFIG_PATH = "config.json"
SESSION_PATH = "session.json"

# Local font storage path in user's home directory
FONT_DIR = os.path.expanduser("~/.local/share/basedbot")
FONT_SERIF_PATH = os.path.join(FONT_DIR, "ebgaramond.ttf")
FONT_SANS_PATH = os.path.join(FONT_DIR, "notosans.ttf")
FONT_EMOJI_PATH = os.path.join(FONT_DIR, "notoemoji.ttf")

# Highly stable Font source URLs from the official Google Fonts repository (direct raw downloads)
URL_SERIF = "https://github.com/google/fonts/raw/main/ofl/ebgaramond/static/EBGaramond-Regular.ttf"
URL_SANS = "https://github.com/google/fonts/raw/main/ofl/notosans/NotoSans%5Bwdth%2Cwght%5D.ttf"
# Switch to Pillow-compatible vector-based monochrome Noto Emoji
URL_EMOJI = "https://github.com/google/fonts/raw/refs/heads/main/ofl/notoemoji/NotoEmoji%5Bwght%5D.ttf"

# Structured instructions for users who use incorrect syntax
HELP_MESSAGE = (
    "⚠️ **Incorrect Based Bot Usage!**\n\n"
    "Here is how to use the bot correctly:\n\n"
    "1️⃣ **Reply to a Text Message** with `!based <word>`\n\n"
    "   *(Note: <word> must match a word inside the message you reply to with at most 1 word difference)*\n\n"
    "2️⃣ **Reply to an Image Message** with `!based <caption_text>`\n\n"
    "   *(This will turn that exact image into a demotivational poster)*"
)

# ==========================================
# 1. Image Generation Module
# ==========================================

def download_file(url: str, dest_path: str):
    """Utility helper to download any file to a target destination."""
    try:
        req = urllib.request.Request(
            url, 
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        )
        with urllib.request.urlopen(req, timeout=20) as response, open(dest_path, "wb") as out_file:
            out_file.write(response.read())
        print(f"[+] Downloaded: {dest_path}")
    except Exception as e:
        print(f"[!] Error downloading {url}: {e}")

def ensure_font_installed():
    """
    Checks if all required custom fonts are present in the local directory.
    If not, downloads them directly from official public sources.
    """
    os.makedirs(FONT_DIR, exist_ok=True)
    
    if not os.path.exists(FONT_SERIF_PATH):
        print("[*] Downloading Serif Font...")
        download_file(URL_SERIF, FONT_SERIF_PATH)
        
    if not os.path.exists(FONT_SANS_PATH):
        print("[*] Downloading Multilingual Sans Font...")
        download_file(URL_SANS, FONT_SANS_PATH)
        
    if not os.path.exists(FONT_EMOJI_PATH):
        print("[*] Downloading Emoji Font...")
        download_file(URL_EMOJI, FONT_EMOJI_PATH)

    return True


def is_combining_or_modifier(char: str) -> bool:
    """
    Identifies zero-width joiners, skin tone modifiers, and variation selectors
    that should inherit the font classification of their parent character run.
    """
    code = ord(char)
    # Variation Selectors
    if 0xFE00 <= code <= 0xFE0F:
        return True
    # Zero Width Joiner (ZWJ)
    if code == 0x200D:
        return True
    # Fitzpatrick Skin Tone Modifiers
    if 0x1F3FB <= code <= 0x1F3FF:
        return True
    # Combining Diacritical Marks
    if 0x0300 <= code <= 0x036F:
        return True
    return False


def get_font_for_char(char: str, size: int) -> Tuple[ImageFont.ImageFont, bool]:
    """
    Analyzes a character and returns the best matching font and a boolean 
    representing whether it requires fallback rendering.
    """
    code = ord(char)
    
    # 1. Emoji / Pictographs / Emoticons
    if (0x1F300 <= code <= 0x1F9FF) or (0x2600 <= code <= 0x27BF) or (0x1F600 <= code <= 0x1F64F):
        if os.path.exists(FONT_EMOJI_PATH):
            try:
                return ImageFont.truetype(FONT_EMOJI_PATH, size), True
            except Exception:
                pass
                
    # 2. General non-Latin Unicode / Multilingual scripts
    elif code > 127:
        if os.path.exists(FONT_SANS_PATH):
            try:
                return ImageFont.truetype(FONT_SANS_PATH, size), False
            except Exception:
                pass
                
    # 3. Default Latin / Serif Text
    if os.path.exists(FONT_SERIF_PATH):
        try:
            return ImageFont.truetype(FONT_SERIF_PATH, size), False
        except Exception:
            pass
            
    return ImageFont.load_default(), False


def segment_text(text: str, size: int) -> List[Tuple[str, ImageFont.ImageFont, bool]]:
    """
    Splits text into consecutive runs of characters sharing the exact same font mapping
    to maintain pristine typesetting/kerning performance in PIL.
    """
    if not text:
        return []
        
    segments = []
    current_run = []
    current_font, current_is_emoji = get_font_for_char(text[0], size)
    
    for char in text:
        # If the character is an emoji modifier, retain parent font properties
        if is_combining_or_modifier(char):
            font = current_font
            is_emoji = current_is_emoji
        else:
            font, is_emoji = get_font_for_char(char, size)
            
        # Check if the font configuration has changed
        if font == current_font and is_emoji == current_is_emoji:
            current_run.append(char)
        else:
            segments.append(("".join(current_run), current_font, current_is_emoji))
            current_run = [char]
            current_font = font
            current_is_emoji = is_emoji
            
    if current_run:
        segments.append(("".join(current_run), current_font, current_is_emoji))
        
    return segments


def measure_mixed_text(text: str, size: int) -> Tuple[int, int]:
    """Calculates the total width and height of a mixed unicode/emoji string."""
    segments = segment_text(text, size)
    total_width = 0
    max_height = 0
    
    # We use a dummy canvas to measure
    temp_img = Image.new("RGB", (1, 1))
    temp_draw = ImageDraw.Draw(temp_img)
    
    for run, font, _ in segments:
        if hasattr(temp_draw, "textbbox"):
            bbox = temp_draw.textbbox((0, 0), run, font=font)
            w = bbox[2] - bbox[0]
            h = bbox[3] - bbox[1]
        else:
            w, h = temp_draw.textsize(run, font=font)
        total_width += w
        if h > max_height:
            max_height = h
            
    return total_width, max_height


def draw_mixed_text(draw: ImageDraw.ImageDraw, text: str, size: int, start_x: int, y: int, fill_color: str = "white"):
    """Draws a line of mixed unicode/emoji text starting from start_x."""
    segments = segment_text(text, size)
    current_x = start_x
    
    for run, font, is_emoji in segments:
        color = fill_color
        # Draw the current run
        draw.text((current_x, y), run, font=font, fill=color)
        
        # Increment X offset by the run's width
        if hasattr(draw, "textbbox"):
            bbox = draw.textbbox((0, 0), run, font=font)
            w = bbox[2] - bbox[0]
        else:
            w, _ = draw.textsize(run, font=font)
        current_x += w


def get_autoscaled_font_size(text: str, max_width: int, max_height: int) -> int:
    """Finds the largest possible font size that allows the entire line to fit."""
    size = 100
    while size > 12:
        w, h = measure_mixed_text(text, size)
        if w <= max_width and h <= max_height:
            return size
        size -= 2
    return 12


def wrap_text_mixed(text: str, font_size: int, max_width: int) -> list[str]:
    """Splits a line of text containing mixed unicode characters/emojis into wrapped lines."""
    lines = []
    words = text.split(" ")
    current_line = []
    
    for word in words:
        test_line = " ".join(current_line + [word]) if current_line else word
        w, _ = measure_mixed_text(test_line, font_size)
        
        if w <= max_width:
            current_line.append(word)
        else:
            if current_line:
                lines.append(" ".join(current_line))
            current_line = [word]
            
    if current_line:
        lines.append(" ".join(current_line))
        
    return lines


# ==========================================
# Word-Level Approximate Matcher Module
# ==========================================

def strip_emojis(text: str) -> str:
    """Removes all emojis and special pictographs from a string for accurate word comparisons."""
    emoji_pattern = re.compile(
        "["
        "\U0001f600-\U0001f64f"  # emoticons
        "\U0001f300-\U0001f5ff"  # symbols & pictographs
        "\U0001f680-\U0001f6ff"  # transport & map symbols
        "\U0001f1e0-\U0001f1ff"  # flags (iOS)
        "\U00002700-\U000027bf"  # dingbats
        "\U00002600-\U000026ff"  # misc symbols
        "\U0001f900-\U0001f9ff"  # supplemental symbols/pictographs
        "\U0001f004-\U0001f0cf"  # Mahjong / Domino tiles etc.
        "\U0000200d"             # Zero-width joiner
        "]+", flags=re.UNICODE
    )
    return emoji_pattern.sub(r'', text)


def clean_and_tokenize(text: str) -> list[str]:
    """Strips emojis and returns an ordered list of lowercase alphanumeric words."""
    no_emoji = strip_emojis(text)
    # Extracts words, ignoring punctuation
    words = re.findall(r'\b\w+\b', no_emoji.lower())
    return words


def word_edit_distance(s1: list[str], s2: list[str]) -> int:
    """Calculates word-level Levenshtein edit distance between two token lists."""
    m, n = len(s1), len(s2)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(m + 1):
        dp[i][0] = i
    for j in range(n + 1):
        dp[0][j] = j
        
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if s1[i-1] == s2[j-1]:
                dp[i][j] = dp[i-1][j-1]
            else:
                dp[i][j] = 1 + min(dp[i-1][j], dp[i][j-1], dp[i-1][j-1])
    return dp[m][n]


def is_approximate_match(parent_body: str, query_text: str) -> bool:
    """
    Checks if the query_text can be matched to any part of parent_body with
    at most one word added or removed (ignoring case, punctuation, and emojis).
    """
    parent_words = clean_and_tokenize(parent_body)
    query_words = clean_and_tokenize(query_text)
    
    # If the user only reacted with emojis, allow the match to pass through
    if not query_words:
        return True
        
    len_q = len(query_words)
    
    # We check slices of length (len_q - 1), len_q, and (len_q + 1)
    for l in (len_q - 1, len_q, len_q + 1):
        if l < 1:
            continue
        for i in range(len(parent_words) - l + 1):
            subsegment = parent_words[i : i + l]
            # If word edit distance is <= 1, then the word sequence matches!
            if word_edit_distance(subsegment, query_words) <= 1:
                return True
                
    return False


def generate_based_meme(
    image_bytes: Optional[bytes], 
    caption: str,
    is_image_mode: bool = False,
    sender_display_name: Optional[str] = None,
    message_body: Optional[str] = None
) -> io.BytesIO:
    """
    Generates a single-frame demotivational style black-box image.
    """
    width, height = 800, 600
    img = Image.new("RGB", (width, height), color="black")
    draw = ImageDraw.Draw(img)
    
    # Define a single inner frame box for the target
    box_x1, box_y1 = 80, 40
    box_x2, box_y2 = 720, 450
    
    # Draw the white frame boundary outline
    draw.rectangle([box_x1, box_y1, box_x2, box_y2], outline="white", width=4)
    
    # Coordinates inside the frame boundary
    img_x1, img_y1 = box_x1 + 4, box_y1 + 4
    img_x2, img_y2 = box_x2 - 4, box_y2 - 4
    img_w = img_x2 - img_x1
    img_h = img_y2 - img_y1
    
    # -------------------------------------------------------------
    # Render Frame Content: IMAGE MODE
    # -------------------------------------------------------------
    if is_image_mode:
        if image_bytes:
            try:
                pfp_img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
                
                # Fit/contain the image inside the frame box
                pfp_ratio = pfp_img.width / pfp_img.height
                target_ratio = img_w / img_h
                
                if pfp_ratio > target_ratio:
                    new_w = img_w
                    new_h = int(img_w / pfp_ratio)
                    offset_x = 0
                    offset_y = (img_h - new_h) // 2
                else:
                    new_h = img_h
                    new_w = int(img_h * pfp_ratio)
                    offset_x = (img_w - new_w) // 2
                    offset_y = 0
                    
                pfp_resized = pfp_img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                
                # Draw the fitted image centered on a clean black background card inside the frame
                frame_bg = Image.new("RGB", (img_w, img_h), "black")
                frame_bg.paste(pfp_resized, (offset_x, offset_y))
                img.paste(frame_bg, (img_x1, img_y1))
            except Exception as e:
                print(f"[Meme Gen] Error processing image: {e}. Drawing placeholder.")
                draw_placeholder(draw, img_x1, img_y1, img_x2, img_y2)
        else:
            draw_placeholder(draw, img_x1, img_y1, img_x2, img_y2)
            
    # -------------------------------------------------------------
    # Render Frame Content: TEXT MODE (Proportional Chat Bubble Card)
    # -------------------------------------------------------------
    else:
        # Fill frame box with a sleek, dark-slate gray client card background
        card_bg = "#151518"
        draw.rectangle([img_x1, img_y1, img_x2, img_y2], fill=card_bg)
        
        body_text = message_body if message_body else ""
        
        # Start with a very large text size and scale down as needed
        body_font_size = 36
        while body_font_size > 12:
            pfp_size = int(body_font_size * 3.2)
            pfp_padding = int(body_font_size * 0.9)
            
            pfp_x = img_x1 + pfp_padding
            pfp_y = img_y1 + pfp_padding
            text_x = pfp_x + pfp_size + pfp_padding
            text_y = pfp_y
            
            available_width = img_x2 - text_x - pfp_padding
            
            # Wrap text inside the dynamic bounds
            wrapped_lines = wrap_text_mixed(body_text, body_font_size, available_width)
            
            # Calculate height
            user_font_size = int(body_font_size * 1.1)
            line_height = body_font_size + int(body_font_size * 0.3)
            total_text_height = user_font_size + 16 + (len(wrapped_lines) * line_height)
            
            max_available_height = img_h - (pfp_padding * 2)
            if total_text_height <= max_available_height:
                break
                
            body_font_size -= 2

        # Draw circular profile picture avatar
        avatar_rendered = False
        if image_bytes:
            try:
                pfp_raw = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
                pfp_resized = pfp_raw.resize((pfp_size, pfp_size), Image.Resampling.LANCZOS)
                
                # Circular mask
                mask = Image.new("L", (pfp_size, pfp_size), 0)
                mask_draw = ImageDraw.Draw(mask)
                mask_draw.ellipse([0, 0, pfp_size, pfp_size], fill=255)
                
                pfp_canvas = Image.new("RGBA", (pfp_size, pfp_size), card_bg)
                pfp_canvas.paste(pfp_resized, (0, 0), mask)
                img.paste(pfp_canvas.convert("RGB"), (pfp_x, pfp_y))
                avatar_rendered = True
            except Exception as e:
                print(f"[Meme Gen] Avatar compositing failed: {e}")
                
        if not avatar_rendered:
            draw_blank_avatar(draw, pfp_x, pfp_y, pfp_size, bg_color="#1e1e1e", fg_color="#4a4f56")
            
        # Render sender pretty display name
        username = sender_display_name if sender_display_name else "UNKNOWN USER"
        user_font_size = int(body_font_size * 1.1)
        draw_mixed_text(draw, username, user_font_size, text_x, text_y, fill_color="#e4e6eb")
        
        # Draw wrapped lines onto the canvas card context
        current_y = text_y + user_font_size + 16
        line_height = body_font_size + int(body_font_size * 0.3)
        for line in wrapped_lines:
            if current_y + body_font_size > img_y2 - 12:
                draw_mixed_text(draw, "... [TRUNCATED]", body_font_size, text_x, current_y, fill_color="#888a8e")
                break
            draw_mixed_text(draw, line, body_font_size, text_x, current_y, fill_color="#b9bbbe")
            current_y += line_height

    # -------------------------------------------------------------
    # Render Bottom Typography Frame (Capitalized Caption)
    # -------------------------------------------------------------
    text_content = caption.upper()
    
    # Safe boundaries for bottom text to prevent running off the frame
    max_text_width = 720  
    max_text_height = 80  
    
    # Fetch font dynamically scaled to fit
    font_size = get_autoscaled_font_size(text_content, max_text_width, max_text_height)
    
    # Position text centering below the box (centered between y=450 and y=600)
    text_y_center = 525
    total_w, total_h = measure_mixed_text(text_content, font_size)
    
    # Centering math
    start_x = (width - total_w) // 2
    start_y = text_y_center - (total_h // 2)
    
    draw_mixed_text(draw, text_content, font_size, start_x, start_y, fill_color="white")
    
    # Output image buffer
    output = io.BytesIO()
    img.save(output, format="PNG")
    output.seek(0)
    return output


def draw_placeholder(draw: ImageDraw.ImageDraw, x1: int, y1: int, x2: int, y2: int):
    """Draws a 'No Image / Broken Image' icon centered in the frame."""
    bg_col = "#1e1e1e"
    frame_col = "#4a4f56"
    icon_col = "#6b7078"
    accent_col = "#8a8f96"
    slash_col = "#ff4444"
    
    cx = (x1 + x2) // 2
    cy = (y1 + y2) // 2
    w = x2 - x1
    h = y2 - y1
    
    s = min(w, h) * 0.35
    draw.rectangle([x1, y1, x2, y2], fill=bg_col)
    
    fw, fh = s * 1.6, s
    fx1, fy1 = cx - fw // 2, cy - fh // 2
    fx2, fy2 = fx1 + fw, fy1 + fh
    line_w = max(2, int(s * 0.03))
    draw.rectangle([fx1, fy1, fx2, fy2], outline=frame_col, width=line_w)
    
    pad = max(2, int(s * 0.05))
    mountain_points = [
        (fx2 - pad, fy2 - pad),                    
        (fx1 + pad, fy2 - pad),                    
        (cx - int(fw * 0.2), fy1 + int(fh * 0.4)), 
        (cx, fy1 + pad),                           
        (cx + int(fw * 0.25), fy1 + int(fh * 0.35)),
    ]
    draw.polygon(mountain_points, fill=icon_col, outline=frame_col)
    
    sun_r = int(s * 0.15)
    sun_x = fx2 - pad - sun_r
    sun_y = fy1 + pad + sun_r
    draw.ellipse([sun_x - sun_r, sun_y - sun_r, sun_x + sun_r, sun_y + sun_r], fill=accent_col, outline=frame_col)
    
    slash_w = max(3, int(s * 0.04))
    draw.line([fx1, fy2, fx2, fy1], fill=slash_col, width=slash_w)
    
def draw_blank_avatar(draw: ImageDraw.ImageDraw, x: int, y: int, size: int, bg_color: str = "#1e1e1e", fg_color: str = "#4a4f56"):
    """
    Draws a standard 'blank profile' silhouette (head + shoulders) 
    centered inside a circular boundary at (x, y) with diameter `size`.
    """
    cx = x + size // 2
    cy = y + size // 2
    draw.ellipse([x, y, x + size, y + size], fill=bg_color, outline="#3a3f44", width=2)
    
    head_radius = int(size * 0.25)
    head_cy = cy - int(size * 0.1)
    draw.ellipse(
        [cx - head_radius, head_cy - head_radius, cx + head_radius, head_cy + head_radius], 
        fill=fg_color
    )
    
    shoulder_width = int(size * 0.65)
    shoulder_top = head_cy + head_radius + int(size * 0.02)
    margin_bottom = int(size * 0.1)
    points = [
        (cx - shoulder_width // 2, shoulder_top),      
        (cx + shoulder_width // 2, shoulder_top),      
        (cx + shoulder_width // 2 - int(size*0.05), y + size - margin_bottom), 
        (cx - shoulder_width // 2 + int(size*0.05), y + size - margin_bottom), 
    ]
    draw.polygon(points, fill=fg_color)


# ==========================================
# 2. Matrix Bot Client Implementation
# ==========================================

class BasedMatrixBot:
    def __init__(self, config: dict):
        self.config = config
        self.homeserver = config["homeserver"]
        self.username = config["username"]
        self.password = config["password"]
        
        # Configure the nio AsyncClient
        client_config = AsyncClientConfig(
            store_sync_tokens=True,
            encryption_enabled=False
        )
        self.client = AsyncClient(self.homeserver, self.username, config=client_config)

    async def run(self):
        """Initializes login state, handles caching, and starts the long sync loop."""
        logged_in = False
        
        # Attempt to load cached session credentials
        if os.path.exists(SESSION_PATH):
            try:
                with open(SESSION_PATH, "r") as f:
                    session_data = json.load(f)
                
                print(f"[*] Attempting to restore session for {self.username}...")
                self.client.access_token = session_data["access_token"]
                self.client.device_id = session_data["device_id"]
                self.client.user_id = session_data["user_id"]
                
                # Check session validity with a sync request
                await self.client.sync(timeout=3000)
                logged_in = True
                print("[+] Successfully restored session from cache!")
            except Exception as e:
                print(f"[!] Session restore failed: {e}. Logging in again...")
                self.client.access_token = None
                self.client.device_id = None
                
        if not logged_in:
            print(f"[*] Authenticating with password to {self.homeserver}...")
            response = await self.client.login(password=self.password)
            
            if isinstance(response, LoginResponse):
                print("[+] Authentication successful!")
                session_data = {
                    "access_token": response.access_token,
                    "device_id": response.device_id,
                    "user_id": response.user_id
                }
                with open(SESSION_PATH, "w") as f:
                    json.dump(session_data, f, indent=4)
                print(f"[*] Session credentials cached to '{SESSION_PATH}'.")
            else:
                print(f"[CRITICAL] Login failed: {response.message}")
                return

        # Register event callbacks correctly via add_event_callback
        self.client.add_event_callback(self.handle_message, RoomMessageText)
        self.client.add_event_callback(self.handle_invite, InviteMemberEvent)
        
        print(f"\n[+] Bot is running as {self.username}. Listening for !based...")
        await self.client.sync_forever(timeout=30000, full_state=True)

    async def handle_invite(self, room: MatrixRoom, event: InviteMemberEvent):
        """Automatically joins any room the bot is invited to."""
        if event.state_key == self.client.user_id:
            print(f"[*] Received room invitation for {room.room_id} from {event.sender}")
            
            # Try to join the room with simple backoff retry logic
            for attempt in range(3):
                print(f"[*] Joining room {room.room_id} (Attempt {attempt + 1}/3)...")
                response = await self.client.join(room.room_id)
                
                if hasattr(response, "room_id"):
                    print(f"[+] Successfully joined room: {room.room_id}")
                    break
                else:
                    print(f"[!] Join failed: {getattr(response, 'message', 'Unknown Error')}")
                    await asyncio.sleep(3)

    async def download_mxc(self, mxc_url: str) -> Optional[bytes]:
        """Downloads arbitrary MXC media using modern authenticated download endpoints."""
        if not mxc_url or not mxc_url.startswith("mxc://"):
            return None
            
        try:
            mxc_parts = mxc_url[6:].split("/", 1)
            if len(mxc_parts) != 2:
                return None
                
            server_name, media_id = mxc_parts
            download_url = f"{self.homeserver}/_matrix/client/v1/media/download/{server_name}/{media_id}"
            
            # Embed OAuth Bearer credentials
            headers = {}
            if self.client.access_token:
                headers["Authorization"] = f"Bearer {self.client.access_token}"
            
            async with aiohttp.ClientSession() as session:
                async with session.get(download_url, headers=headers, timeout=15) as resp:
                    if resp.status == 200:
                        return await resp.read()
                    else:
                        print(f"[!] Failed to download MXC media (HTTP {resp.status})")
        except Exception as e:
            print(f"[!] Failed to fetch/download MXC media for {mxc_url}: {e}")
            
        return None

    async def get_target_avatar(self, user_id: str) -> Optional[bytes]:
        """Downloads the target user's profile avatar."""
        try:
            print(f"[*] Fetching profile for target user {user_id}...")
            profile = await self.client.get_profile(user_id)
            if profile.avatar_url:
                return await self.download_mxc(profile.avatar_url)
            else:
                print(f"[-] Target {user_id} does not have a valid profile avatar.")
        except Exception as e:
            print(f"[!] Failed to fetch profile avatar for {user_id}: {e}")
            
        return None

    async def send_help_reply(self, room_id: str, event_id: str, message_md: str):
        """Sends a formatted HTML notice replying to the sender's incorrect message."""
        # Convert the markdown string to HTML for the Matrix client to render
        html_body = markdown.markdown(message_md)
        
        content = {
            "body": message_md,               # Plain text fallback
            "msgtype": "m.notice",
            "format": "org.matrix.custom.html",
            "formatted_body": html_body,      # This enables formatting
            "m.relates_to": {
                "m.in_reply_to": {
                    "event_id": event_id
                }
            }
        }
        
        try:
            resp = await self.client.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content=content
            )
            if not isinstance(resp, RoomSendResponse):
                print(f"[!] Failed to send help reply: {getattr(resp, 'message', 'Unknown error')}")
        except Exception as e:
            print(f"[!] Exception raised while sending help reply: {e}")

    async def handle_message(self, room: MatrixRoom, event: RoomMessageText):
        """Processes incoming room messages."""
        if event.sender == self.client.user_id:
            return
            
        body = event.body.strip()
        
        # -------------------------------------------------------------
        # Robust Command Parsing: Handle Reply blockquotes/fallbacks
        # -------------------------------------------------------------
        lines = body.split("\n")
        command_lines = [line.strip() for line in lines if not line.strip().startswith(">")]
        clean_body = " ".join(command_lines).strip()
        
        # Command recognition
        if not clean_body.lower().startswith("!based"):
            return

        print(f"[*] Command event detected from {event.sender} in room {room.room_id}")

        # -------------------------------------------------------------
        # Startup Replay Guard: Ignore old commands during startup catch-up
        # -------------------------------------------------------------
        now_ms = int(time.time() * 1000)
        if now_ms - event.server_timestamp > 120000:
            print(f"[*] Skipping old catch-up message from {event.sender} (timestamp delta: {now_ms - event.server_timestamp}ms)")
            return

        # Parse command query text
        match = re.match(r"^!based\s+(.+)$", clean_body, re.IGNORECASE)
        if not match:
            print(f"[-] Incorrect command syntax from {event.sender}. Sending help...")
            await self.send_help_reply(room.room_id, event.event_id, HELP_MESSAGE)
            return
            
        query_text = match.group(1).strip()
        
        # Check if the incoming message is actually a reply
        relates_to = event.source.get("content", {}).get("m.relates_to", {})
        reply_to = relates_to.get("m.in_reply_to", {})
        parent_event_id = reply_to.get("event_id")
        
        if not parent_event_id:
            print(f"[-] Command used without a reply context. Sending help...")
            await self.send_help_reply(room.room_id, event.event_id, HELP_MESSAGE)
            return
            
        print(f"[*] Processing command !based with text: '{query_text}' in room {room.room_id}")
        
        # -------------------------------------------------------------
        # Duplicate Prevention
        # -------------------------------------------------------------
        already_replied = False
        start_token = getattr(room, "prev_batch", None)
        if start_token:
            try:
                history_resp = await self.client.room_messages(
                    room_id=room.room_id,
                    start=start_token,
                    limit=20
                )
                if hasattr(history_resp, "chunk") and history_resp.chunk:
                    for old_event in history_resp.chunk:
                        old_sender = getattr(old_event, "sender", None)
                        if old_sender == self.client.user_id:
                            source = getattr(old_event, "source", {})
                            content = source.get("content", {})
                            old_rel = content.get("m.relates_to", {})
                            old_reply_to = old_rel.get("m.in_reply_to", {})
                            old_parent_id = old_reply_to.get("event_id")
                            
                            if old_parent_id == parent_event_id:
                                already_replied = True
                                break
                            
                            old_body = content.get("body", "")
                            if old_body == f"based_{query_text}.png":
                                already_replied = True
                                break
            except Exception as e:
                print(f"[!] Warning: Failed to query room history for duplicates: {e}")

        if already_replied:
            print(f"[-] Already processed or replied to parent event {parent_event_id}. Skipping.")
            return

        parent_event = None
        try:
            resp = await self.client.room_get_event(room.room_id, parent_event_id)
            if hasattr(resp, "event") and resp.event:
                parent_event = resp.event
            else:
                err_msg = getattr(resp, "message", "Unknown reason")
                print(f"[!] Server returned error while getting parent event: {err_msg}")
        except Exception as e:
            print(f"[!] Failed to retrieve parent event: {e}")
            
        if not parent_event:
            print("[-] Replied-to event not found on the server.")
            return
            
        parent_content = getattr(parent_event, "content", {}) or parent_event.source.get("content", {})
        msgtype = parent_content.get("msgtype")
        target_user_id = parent_event.sender

        image_bytes = None
        is_image_mode = False
        parent_clean_body = ""
        target_display_name = target_user_id

        # Fetch the pretty displayname for the target user
        try:
            profile = await self.client.get_profile(target_user_id)
            if profile.displayname:
                target_display_name = profile.displayname
        except Exception as e:
            print(f"[!] Failed to fetch profile pretty name for {target_user_id}: {e}")

        # -------------------------------------------------------------
        # IMAGE MODE: If the replied-to message is an image
        # -------------------------------------------------------------
        if msgtype == "m.image":
            is_image_mode = True
            mxc_url = parent_content.get("url")
            if mxc_url:
                print(f"[+] Parent message is an image! Fetching image...")
                image_bytes = await self.download_mxc(mxc_url)
            
            if not image_bytes:
                print("[-] Failed to download the replied-to image.")
        
        # -------------------------------------------------------------
        # TEXT MODE: If the replied-to message is a standard text message
        # -------------------------------------------------------------
        else:
            parent_body = getattr(parent_event, "body", "") or parent_content.get("body", "")
            if not parent_body:
                print("[-] Replied-to text event has no body.")
                await self.send_help_reply(room.room_id, event.event_id, HELP_MESSAGE)
                return

            parent_lines = parent_body.split("\n")
            parent_clean_lines = [line.strip() for line in parent_lines if not line.strip().startswith(">")]
            parent_clean_body = " ".join(parent_clean_lines).strip()

            # Perform the approximate word edit distance check (allowing up to 1 word added or removed)
            if not is_approximate_match(parent_clean_body, query_text):
                print(f"[-] Match failed! '{query_text}' differs by more than 1 word from '{parent_clean_body}' (case-insensitive, ignoring emojis).")
                mismatch_reply = (
                    f"⚠️ **Text Match Failed!**\n\n"
                    f"Your caption `\"{query_text}\"` does not sufficiently match the message you replied to.\n"
                    f"Please verify your caption quotes the text accurately (you can add or remove at most one word, ignoring emojis)!"
                )
                await self.send_help_reply(room.room_id, event.event_id, mismatch_reply)
                return
                
            print(f"[+] Match successful! Fetching profile picture for {target_user_id}.")
            image_bytes = await self.get_target_avatar(target_user_id)

        # Generate the meme image in a non-blocking threadpool context
        loop = asyncio.get_running_loop()
        meme_buffer = await loop.run_in_executor(
            None, 
            generate_based_meme, 
            image_bytes, 
            query_text, 
            is_image_mode, 
            target_display_name, 
            parent_clean_body
        )
        
        # Upload the image to the homeserver
        print("[*] Uploading generated image...")
        try:
            data_bytes = meme_buffer.getvalue()
            filesize = len(data_bytes)
            meme_buffer.seek(0)
            
            upload_resp, maybe_keys = await self.client.upload(
                meme_buffer,
                content_type="image/png",
                filename="based_meme.png",
                filesize=filesize
            )
            
            if not hasattr(upload_resp, "content_uri"):
                print(f"[!] Failed to upload image: {upload_resp}")
                return
                
            mxc_uri = upload_resp.content_uri
            print(f"[+] Upload success! MXC URI: {mxc_uri}")
            
            content = {
                "body": f"based_{query_text}.png",
                "info": {
                    "size": filesize,
                    "mimetype": "image/png",
                    "w": 800,
                    "h": 600
                },
                "msgtype": "m.image",
                "url": mxc_uri,
                "m.relates_to": {
                    "m.in_reply_to": {
                        "event_id": event.event_id
                    }
                }
            }
            
            resp = await self.client.room_send(
                room_id=room.room_id,
                message_type="m.room.message",
                content=content
            )
            
            if isinstance(resp, RoomSendResponse):
                print("[+] Meme sent successfully!")
            else:
                print(f"[!] Error posting meme: {getattr(resp, 'message', 'No details provided')}")
            
        except Exception as e:
            print(f"[!] Exception raised during upload or transmission: {e}")


# ==========================================
# 3. Interactive Config Setup Creator
# ==========================================

def setup_config():
    """Checks for configuration, prompting the user interactively if missing."""
    if os.path.exists(CONFIG_PATH):
        return True
        
    print("="*60)
    print("                MATRIX BASED BOT INITIAL SETUP              ")
    print("="*60)
    print("Configuration file 'config.json' not found.")
    print("Please input the connection configuration for your bot:")
    
    homeserver = input("Enter homeserver URL (default: https://matrix.org): ").strip()
    if not homeserver:
        homeserver = "https://matrix.org"
        
    if not homeserver.startswith("http://") and not homeserver.startswith("https://"):
        homeserver = "https://" + homeserver
        
    username = input("Enter bot username (e.g., @mybot:matrix.org): ").strip()
    while not username or not username.startswith("@"):
        username = input("Please enter a valid username starting with '@': ").strip()
        
    password = input("Enter password: ").strip()
    while not password:
        password = input("Password cannot be blank. Enter password: ").strip()
        
    config_data = {
        "homeserver": homeserver,
        "username": username,
        "password": password
    }
    
    with open(CONFIG_PATH, "w") as f:
        json.dump(config_data, f, indent=4)
        
    print(f"\n[+] Configuration file saved successfully to '{CONFIG_PATH}'!")
    print("="*60)
    return True


if __name__ == "__main__":
    # Ensure fonts are downloaded and available locally
    ensure_font_installed()
    
    if not setup_config():
        sys.exit(1)
        
    try:
        with open(CONFIG_PATH, "r") as f:
            config = json.load(f)
            
        bot = BasedMatrixBot(config)
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("\n[-] Shutting down bot... Goodbye!")
    except Exception as e:
        print(f"[CRITICAL] Unexpected crash: {e}")
