#!/usr/bin/env python3
"""
Matrix Based Bot
================
A Matrix bot that listens for "!based <text>" replies, validates that the <text>
matches (case-insensitively) the message being replied to, downloads the target's
profile picture, and generates a single-frame demotivational style meme.

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
from typing import Optional
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
FONT_PATH = os.path.join(FONT_DIR, "ebgaramond.ttf")

# Direct permanent Google Fonts CDN link to ensure no 404/redirect errors occur
FONT_URL = "https://fonts.gstatic.com/s/ebgaramond/v26/kJF1BvYxA4ggYvqAdU371t7R83fufXpS.ttf"

# Structured instructions for users who use incorrect syntax
HELP_MESSAGE = (
    "⚠️ **Incorrect Based Bot Usage!**\n\n"
    "Here is how to use the bot correctly:\n\n"
    "1️⃣ **Reply to a Text Message** with `!based <word>`\n\n"
    "   *(Note: <word> must match a word inside the message you reply to)*\n\n"
    "2️⃣ **Reply to an Image Message** with `!based <caption_text>`\n\n"
    "   *(This will turn that exact image into a demotivational poster)*"
)

# ==========================================
# 1. Image Generation Module
# ==========================================

def ensure_font_installed():
    """
    Checks if the local custom font is present in the local directory.
    If not, downloads it directly from the official public source.
    """
    if os.path.exists(FONT_PATH):
        return True

    print(f"[*] Local serif font not found. Preparing directory: {FONT_DIR}")
    try:
        os.makedirs(FONT_DIR, exist_ok=True)
        print(f"[*] Downloading book-like serif font from Google Fonts (CDN)...")
        
        # User-Agent header to prevent generic request blocks
        req = urllib.request.Request(
            FONT_URL, 
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        )
        
        with urllib.request.urlopen(req, timeout=15) as response, open(FONT_PATH, "wb") as out_file:
            out_file.write(response.read())
            
        print(f"[+] Font downloaded successfully and saved to: {FONT_PATH}")
        return True
    except Exception as e:
        print(f"[!] Error downloading custom font: {e}")
        return False


def find_serif_font(size: int) -> ImageFont.ImageFont:
    """
    Attempts to locate the automatically downloaded local serif font first.
    Falls back to system fonts or the default system font if unavailable.
    """
    # 1. Prioritize the auto-downloaded custom serif font
    if os.path.exists(FONT_PATH):
        try:
            return ImageFont.truetype(FONT_PATH, size)
        except Exception:
            pass

    # 2. Hardcoded fallback list for standard systems
    font_paths = [
        # Windows (Georgia Regular, Times New Roman Regular)
        "C:\\Windows\\Fonts\\georgia.ttf",
        "C:\\Windows\\Fonts\\times.ttf",
        "C:\\Windows\\Fonts\\georgiab.ttf",
        "C:\\Windows\\Fonts\\timesbd.ttf",
        # Linux / VPS (Ubuntu/Debian)
        "/usr/share/fonts/truetype/msttcorefonts/Georgia.ttf",
        "/usr/share/fonts/truetype/msttcorefonts/Times_New_Roman.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
        # macOS
        "/System/Library/Fonts/Supplemental/Georgia.ttf",
        "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
        "/Library/Fonts/Georgia.ttf",
        "/Library/Fonts/Times New Roman.ttf",
    ]
    
    for path in font_paths:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
                
    # 3. Dynamic search in standard system directories if still missing
    system_font_dirs = ["/usr/share/fonts", "/usr/local/share/fonts", "~/.local/share/fonts"]
    for s_dir in system_font_dirs:
        expanded_dir = os.path.expanduser(s_dir)
        if os.path.exists(expanded_dir):
            for root, _, files in os.walk(expanded_dir):
                for file in files:
                    if file.lower().endswith((".ttf", ".otf")):
                        full_path = os.path.join(root, file)
                        if any(x in file.lower() for x in ["serif", "times", "georgia", "liberation", "dejavu"]):
                            try:
                                return ImageFont.truetype(full_path, size)
                            except Exception:
                                pass
                                
    # Grab literally any scalable font we can find if no serifs exist
    for s_dir in system_font_dirs:
        expanded_dir = os.path.expanduser(s_dir)
        if os.path.exists(expanded_dir):
            for root, _, files in os.walk(expanded_dir):
                for file in files:
                    if file.lower().endswith((".ttf", ".otf")):
                        try:
                            return ImageFont.truetype(os.path.join(root, file), size)
                        except Exception:
                            pass

    # Safe fallback if no system fonts are installed (this default font will not scale!)
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def get_autoscaled_font(text: str, max_width: int, max_height: int) -> ImageFont.ImageFont:
    """
    Dynamically scales the book-style serif font down from a maximum size
    until the rendered text bounding box fits within the specified limits.
    """
    temp_img = Image.new("RGB", (1, 1))
    temp_draw = ImageDraw.Draw(temp_img)
    
    # Start with a prominent headline size and scale down
    size = 100
    while size > 12:
        font = find_serif_font(size)
        if not font:
            size -= 2
            continue
            
        # If we had to fall back to the built-in unscalable Pillow font, stop trying to scale
        if font.__class__.__name__ == "ImageDefaultFont":
            print("[!] Warning: No TrueType system fonts detected on VPS! Text scaling is disabled.")
            return font
            
        # Compute dimensions of text under current font size
        if hasattr(temp_draw, "textbbox"):
            bbox = temp_draw.textbbox((0, 0), text, font=font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
        else:
            text_w, text_h = temp_draw.textsize(text, font=font)
            
        # If it fits within our padding limits, return this font size
        if text_w <= max_width and text_h <= max_height:
            return font
            
        size -= 2
        
    return find_serif_font(12)


def wrap_text(text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    """
    Utility helper to split a line of text into wrapped segments that cleanly
    fit within a given width limit in pixels.
    """
    lines = []
    words = text.split(" ")
    current_line = []
    
    # Create temporary measurement context
    temp_img = Image.new("RGB", (1, 1))
    temp_draw = ImageDraw.Draw(temp_img)
    
    for word in words:
        test_line = " ".join(current_line + [word]) if current_line else word
        
        if hasattr(temp_draw, "textbbox"):
            bbox = temp_draw.textbbox((0, 0), test_line, font=font)
            w = bbox[2] - bbox[0]
        else:
            w, _ = temp_draw.textsize(test_line, font=font)
            
        if w <= max_width:
            current_line.append(word)
        else:
            if current_line:
                lines.append(" ".join(current_line))
            current_line = [word]
            
    if current_line:
        lines.append(" ".join(current_line))
        
    return lines


def generate_based_meme(
    image_bytes: Optional[bytes], 
    caption: str,
    is_image_mode: bool = False,
    sender_id: Optional[str] = None,
    message_body: Optional[str] = None
) -> io.BytesIO:
    """
    Generates a single-frame demotivational style black-box image.
    
    - Image dimensions: 800x600
    - If is_image_mode is True, draws the image directly in the inner box frame.
    - If is_image_mode is False, renders a beautiful, clean chat card with circular PFP,
      the user ID, and the full text message beautifully wrapped.
    - Only text at the bottom is the input caption, centered, capitalized.
    """
    # Create black canvas
    width, height = 800, 600
    img = Image.new("RGB", (width, height), color="black")
    draw = ImageDraw.Draw(img)
    
    # Define a single inner frame box for the target (Enlarged to fill the box layout better)
    box_x1, box_y1 = 80, 40
    box_x2, box_y2 = 720, 450
    
    # Draw the white frame boundary outline
    draw.rectangle([box_x1, box_y1, box_x2, box_y2], outline="white", width=4)
    
    # Coordinates inside the frame boundary (offset slightly inside the frame line)
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
                
                # Fit/contain the image inside the frame box (avoids cropping crucial portions)
                pfp_ratio = pfp_img.width / pfp_img.height
                target_ratio = img_w / img_h
                
                if pfp_ratio > target_ratio:
                    # Image is wider than target frame: fit to width, pad top/bottom
                    new_w = img_w
                    new_h = int(img_w / pfp_ratio)
                    offset_x = 0
                    offset_y = (img_h - new_h) // 2
                else:
                    # Image is taller than target frame: fit to height, pad sides
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
        
        # -------------------------------------------------------------
        # Proportional Dynamic Layout Algorithm
        # -------------------------------------------------------------
        # Start with a very large text size and scale everything (avatar size,
        # margins, and paddings) down together until the whole composition
        # fits and fills the maximum size of the box beautifully.
        body_font_size = 36
        while body_font_size > 12:
            body_font = find_serif_font(body_font_size)
            if not body_font:
                body_font_size -= 2
                continue
                
            user_font_size = int(body_font_size * 1.1)
            user_font = find_serif_font(user_font_size)
            
            pfp_size = int(body_font_size * 3.2)
            pfp_padding = int(body_font_size * 0.9)
            
            pfp_x = img_x1 + pfp_padding
            pfp_y = img_y1 + pfp_padding
            text_x = pfp_x + pfp_size + pfp_padding
            text_y = pfp_y
            
            available_width = img_x2 - text_x - pfp_padding
            
            # Wrap text inside the dynamic bounds
            wrapped_lines = wrap_text(body_text, body_font, available_width)
            
            # Calculate the final combined height of Username and Body lines
            line_height = body_font_size + int(body_font_size * 0.3)
            total_text_height = user_font_size + 16 + (len(wrapped_lines) * line_height)
            
            # Limit height to the actual inner bounds minus layout padding
            max_available_height = img_h - (pfp_padding * 2)
            
            if total_text_height <= max_available_height:
                break
                
            body_font_size -= 2

        # Draw circular profile picture avatar on the top-left using calculated proportions
        avatar_rendered = False
        if image_bytes:
            try:
                pfp_raw = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
                pfp_resized = pfp_raw.resize((pfp_size, pfp_size), Image.Resampling.LANCZOS)
                
                # Create mask for circular avatar cropping
                mask = Image.new("L", (pfp_size, pfp_size), 0)
                mask_draw = ImageDraw.Draw(mask)
                mask_draw.ellipse([0, 0, pfp_size, pfp_size], fill=255)
                
                # Composite avatar beautifully with card background
                pfp_canvas = Image.new("RGBA", (pfp_size, pfp_size), card_bg)
                pfp_canvas.paste(pfp_resized, (0, 0), mask)
                img.paste(pfp_canvas.convert("RGB"), (pfp_x, pfp_y))
                avatar_rendered = True
            except Exception as e:
                print(f"[Meme Gen] Avatar compositing failed: {e}")
                
        if not avatar_rendered:
            # Draw the standard "Blank User" silhouette
            draw_blank_avatar(draw, pfp_x, pfp_y, pfp_size, bg_color="#1e1e1e", fg_color="#4a4f56")
            
        # Render sender username/id
        username = sender_id if sender_id else "UNKNOWN USER"
        draw.text((text_x, text_y), username, font=user_font, fill="#e4e6eb")
        
        # Draw wrapped lines onto the canvas card context
        current_y = text_y + user_font_size + 16
        for line in wrapped_lines:
            # Check if text is overflowing the bottom card bounds
            if current_y + body_font_size > img_y2 - 12:
                # Append ellipsis marker to denote overflow
                draw.text((text_x, current_y), "... [TRUNCATED]", font=body_font, fill="#888a8e")
                break
            draw.text((text_x, current_y), line, font=body_font, fill="#b9bbbe")
            current_y += line_height

    # -------------------------------------------------------------
    # Render Bottom Typography Frame (Capitalized Caption)
    # -------------------------------------------------------------
    text_content = caption.upper()
    
    # Safe boundaries for bottom text to prevent running off the frame
    max_text_width = 720  # 800 - 80px total padding (40px on left/right)
    max_text_height = 80  # Height boundary for the footer area
    
    # Fetch font dynamically scaled to fit
    font = get_autoscaled_font(text_content, max_text_width, max_text_height)
    
    # Position text centering below the box (centered between y=450 and y=600)
    text_y_center = 525
    draw_centered_text(draw, text_content, font, width // 2, text_y_center)
    
    # Output image buffer
    output = io.BytesIO()
    img.save(output, format="PNG")
    output.seek(0)
    return output


def draw_placeholder(draw: ImageDraw.ImageDraw, x1: int, y1: int, x2: int, y2: int):
    """Draws a 'No Image / Broken Image' icon centered in the frame."""
    # Colors
    bg_col = "#1e1e1e"
    frame_col = "#4a4f56"
    icon_col = "#6b7078"
    accent_col = "#8a8f96"
    slash_col = "#ff4444"
    
    cx = (x1 + x2) // 2
    cy = (y1 + y2) // 2
    w = x2 - x1
    h = y2 - y1
    
    # Scale factor
    s = min(w, h) * 0.35
    
    # 1. Background
    draw.rectangle([x1, y1, x2, y2], fill=bg_col)
    
    # 2. Photo Frame
    fw, fh = s * 1.6, s
    fx1, fy1 = cx - fw // 2, cy - fh // 2
    fx2, fy2 = fx1 + fw, fy1 + fh
    line_w = max(2, int(s * 0.03))
    draw.rectangle([fx1, fy1, fx2, fy2], outline=frame_col, width=line_w)
    
    # 3. Mountain Silhouette
    pad = max(2, int(s * 0.05))
    mountain_points = [
        (fx2 - pad, fy2 - pad),                    # Bottom Right
        (fx1 + pad, fy2 - pad),                    # Bottom Left
        (cx - int(fw * 0.2), fy1 + int(fh * 0.4)), # Left Peak
        (cx, fy1 + pad),                           # Center High Peak
        (cx + int(fw * 0.25), fy1 + int(fh * 0.35)),# Right Peak
    ]
    draw.polygon(mountain_points, fill=icon_col, outline=frame_col)
    
    # 4. Sun
    sun_r = int(s * 0.15)
    sun_x = fx2 - pad - sun_r
    sun_y = fy1 + pad + sun_r
    draw.ellipse([sun_x - sun_r, sun_y - sun_r, sun_x + sun_r, sun_y + sun_r], fill=accent_col, outline=frame_col)
    
    # 5. Red Diagonal Slash (Broken Image Indicator)
    slash_w = max(3, int(s * 0.04))
    draw.line([fx1, fy2, fx2, fy1], fill=slash_col, width=slash_w)
    
def draw_blank_avatar(draw: ImageDraw.ImageDraw, x: int, y: int, size: int, bg_color: str = "#1e1e1e", fg_color: str = "#4a4f56"):
    """
    Draws a standard 'blank profile' silhouette (head + shoulders) 
    centered inside a circular boundary at (x, y) with diameter `size`.
    """
    cx = x + size // 2
    cy = y + size // 2
    radius = size // 2
    
    # 1. Draw the circular background "frame"
    draw.ellipse([x, y, x + size, y + size], fill=bg_color, outline="#3a3f44", width=2)
    
    # 2. Draw Head (Circle) - Centered horizontally, slightly above vertical center
    head_radius = int(size * 0.25)
    head_cy = cy - int(size * 0.1)
    draw.ellipse(
        [cx - head_radius, head_cy - head_radius, cx + head_radius, head_cy + head_radius], 
        fill=fg_color
    )
    
    # 3. Draw Shoulders (Rounded Rectangle / Trapezoid approximation using chords)
    # Shoulders span ~70% of avatar width, start below head
    shoulder_width = int(size * 0.65)
    shoulder_top = head_cy + head_radius + int(size * 0.02)
    shoulder_bottom = y + size - int(size * 0.1) # Near bottom of circle
    
    # We draw a polygon for the shoulders: top-left, top-right, bottom-right, bottom-left
    # Bottom corners tucked inside the circle boundary
    margin_bottom = int(size * 0.1)
    points = [
        (cx - shoulder_width // 2, shoulder_top),      # Top Left
        (cx + shoulder_width // 2, shoulder_top),      # Top Right
        (cx + shoulder_width // 2 - int(size*0.05), y + size - margin_bottom), # Bottom Right (tucked in)
        (cx - shoulder_width // 2 + int(size*0.05), y + size - margin_bottom), # Bottom Left (tucked in)
    ]
    draw.polygon(points, fill=fg_color)

def draw_centered_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, cx: int, cy: int):
    """Positions text centered perfectly on (cx, cy)."""
    if hasattr(draw, "textbbox"):
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
    else:
        text_w, text_h = draw.textsize(text, font=font)
        
    x = cx - (text_w // 2)
    y = cy - (text_h // 2)
    draw.text((x, y), text, font=font, fill="white")


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
        # Ensure the invite is directed at our bot's user ID
        if event.state_key == self.client.user_id:
            print(f"[*] Received room invitation for {room.room_id} from {event.sender}")
            
            # Try to join the room with simple backoff retry logic
            for attempt in range(3):
                print(f"[*] Joining room {room.room_id} (Attempt {attempt + 1}/3)...")
                response = await self.client.join(room.room_id)
                
                # JoinResponse has a room_id attribute on success
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
            # Parse mxc://domain/media_id
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
            import markdown
            
            # Convert the markdown string to HTML for the Matrix client to render
            html_body = markdown.markdown(message_md)
            
            content = {
                "body": message_md,               # Plain text fallback
                "msgtype": "m.notice",
                "format": "org.matrix.custom.html",
                "formatted_body": html_body,      # This enables the bold/icons/etc
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
        # Matrix clients prepend standard reply content with a blockquote block (lines starting with '>')
        # We strip those lines out to retrieve only the actual command text sent by the user
        lines = body.split("\n")
        command_lines = [line.strip() for line in lines if not line.strip().startswith(">")]
        clean_body = " ".join(command_lines).strip()
        
        # Command recognition: Checks if the parsed command line begins with !based
        if not clean_body.lower().startswith("!based"):
            return

        print(f"[*] Command event detected from {event.sender} in room {room.room_id}")

        # -------------------------------------------------------------
        # Startup Replay Guard: Ignore old commands during startup catch-up
        # -------------------------------------------------------------
        now_ms = int(time.time() * 1000)
        # We set this to 120,000ms (2 minutes) to tolerate federation delays on matrix.org
        if now_ms - event.server_timestamp > 120000:
            print(f"[*] Skipping old catch-up message from {event.sender} (timestamp delta: {now_ms - event.server_timestamp}ms)")
            return

        # Parse command query text from our parsed clean body
        match = re.match(r"^!based\s+(.+)$", clean_body, re.IGNORECASE)
        if not match:
            # Command was started but has wrong or missing text args
            print(f"[-] Incorrect command syntax from {event.sender}. Sending help...")
            await self.send_help_reply(room.room_id, event.event_id, HELP_MESSAGE)
            return
            
        query_text = match.group(1).strip()
        
        # Check if the incoming message is actually a reply
        relates_to = event.source.get("content", {}).get("m.relates_to", {})
        reply_to = relates_to.get("m.in_reply_to", {})
        parent_event_id = reply_to.get("event_id")
        
        if not parent_event_id:
            # Attempted to use bot without replying to a message
            print(f"[-] Command used without a reply context. Sending help...")
            await self.send_help_reply(room.room_id, event.event_id, HELP_MESSAGE)
            return
            
        print(f"[*] Processing command !based with text: '{query_text}' in room {room.room_id}")
        
        # -------------------------------------------------------------
        # Duplicate Prevention: Check past messages in room timeline history
        # -------------------------------------------------------------
        already_replied = False
        start_token = getattr(room, "prev_batch", None)
        if start_token:
            try:
                # Retrieve the last 20 messages safely via client API
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
            print(f"[-] Already processed or replied to parent event {parent_event_id} in recent messages. Skipping.")
            return

        parent_event = None
        try:
            # Safely query the server directly for the parent event structure
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
            
        # Get content structure of parent event safely
        parent_content = getattr(parent_event, "content", {}) or parent_event.source.get("content", {})
        msgtype = parent_content.get("msgtype")
        target_user_id = parent_event.sender

        image_bytes = None
        is_image_mode = False
        parent_clean_body = ""

        # -------------------------------------------------------------
        # IMAGE MODE: If the replied-to message is an image
        # -------------------------------------------------------------
        if msgtype == "m.image":
            is_image_mode = True
            mxc_url = parent_content.get("url")
            if mxc_url:
                print(f"[+] Parent message is an image! Bypassing text check. Fetching image...")
                image_bytes = await self.download_mxc(mxc_url)
            
            if not image_bytes:
                print("[-] Failed to download the replied-to image. Sending fallback placeholder.")
        
        # -------------------------------------------------------------
        # TEXT MODE: If the replied-to message is a standard text message
        # -------------------------------------------------------------
        else:
            parent_body = getattr(parent_event, "body", "") or parent_content.get("body", "")
            if not parent_body:
                print("[-] Replied-to text event has no body.")
                await self.send_help_reply(room.room_id, event.event_id, HELP_MESSAGE)
                return

            # Strip possible quote fallback lines in parent message so they don't render inside the card
            parent_lines = parent_body.split("\n")
            parent_clean_lines = [line.strip() for line in parent_lines if not line.strip().startswith(">")]
            parent_clean_body = " ".join(parent_clean_lines).strip()

            # Case-insensitively match the search query to the target message text
            if query_text.lower() not in parent_clean_body.lower():
                print(f"[-] Match failed! '{query_text}' is not inside '{parent_clean_body}' (case-insensitive).")
                mismatch_reply = (
                    f"⚠️ **Text Match Failed!**\n\n"
                    f"Your caption `\"{query_text}\"` was not found in the message you replied to.\n"
                    f"Please make sure your caption matches the text case-insensitively!"
                )
                await self.send_help_reply(room.room_id, event.event_id, mismatch_reply)
                return
                
            print(f"[+] Match successful! Fetching profile picture for {target_user_id}.")
            # Download profile picture to use in the card mock
            image_bytes = await self.get_target_avatar(target_user_id)

        # Generate the meme image in a non-blocking threadpool context
        loop = asyncio.get_running_loop()
        meme_buffer = await loop.run_in_executor(
            None, 
            generate_based_meme, 
            image_bytes, 
            query_text, 
            is_image_mode, 
            target_user_id, 
            parent_clean_body
        )
        
        # Upload the image to the homeserver
        print("[*] Uploading generated image...")
        try:
            # Calculate the total file size from our memory buffer
            data_bytes = meme_buffer.getvalue()
            filesize = len(data_bytes)
            
            # Reset buffer seek position
            meme_buffer.seek(0)
            
            # Using client.upload(...) as per the matrix-nio documentation
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
            
            # Send the image as a standard message reply to the !based event
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
                # Explicitly thread as a reply to the original !based invocation
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
            
            # Log exact failure reasons to standard output if matrix homeserver rejects the post
            if isinstance(resp, RoomSendResponse):
                print("[+] Meme sent successfully!")
            else:
                # resp is of type RoomSendError
                print(f"[!] Error posting meme to room {room.room_id}:")
                print(f"    - Message: {getattr(resp, 'message', 'No details provided')}")
                print(f"    - Status Code: {getattr(resp, 'status_code', 'N/A')}")
                print(f"    - Error Code: {getattr(resp, 'transport_status_code', 'N/A')}")
            
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
    # Ensure font is downloaded and available locally
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
