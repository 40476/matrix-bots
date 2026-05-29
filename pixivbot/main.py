import asyncio
import re
import os
import json
import aiohttp
from nio import (
    AsyncClient, 
    RoomMessageText, 
    InviteMemberEvent, 
    LoginResponse, 
    MatrixRoom,
    UploadResponse
)

# --- Constants ---
CONFIG_PATH = "config.json"
SESSION_PATH = "session.json"

class PixivBot:
    def __init__(self, config):
        self.homeserver = config["homeserver"]
        self.username = config["username"]
        self.password = config["password"]
        self.client = AsyncClient(self.homeserver, self.username)

    async def handle_invite(self, room: MatrixRoom, event: InviteMemberEvent):
        if event.state_key == self.client.user_id:
            print(f"[*] Received room invitation for {room.room_id} from {event.sender}")
            for attempt in range(3):
                print(f"[*] Joining room {room.room_id} (Attempt {attempt + 1}/3)...")
                response = await self.client.join(room.room_id)
                if hasattr(response, "room_id"):
                    print(f"[+] Successfully joined room: {room.room_id}")
                    break
                else:
                    print(f"[!] Join failed: {getattr(response, 'message', 'Unknown Error')}")
                    await asyncio.sleep(3)

    async def handle_message(self, room: MatrixRoom, event: RoomMessageText):
        if event.sender == self.client.user_id:
            return

        match = re.search(r"pixiv\.net/(?:en/)?artworks/(\d+)", event.body)
        if not match:
            return

        illust_id = match.group(1)
        print(f"[*] Detected Pixiv ID: {illust_id}. Processing via Pixiv.cat...")

        async with aiohttp.ClientSession() as session:
            try:
                # 1. Scrape basic info
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                pixiv_url = f"https://www.pixiv.net/en/artworks/{illust_id}"
                
                title = f"Artwork {illust_id}"
                async with session.get(pixiv_url, headers=headers) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        title_match = re.search(r"<title>(.*?)</title>", text)
                        if title_match:
                            title = title_match.group(1).replace(" - pixiv", "")

                # 2. Download Image
                filename = f"thumb_{illust_id}.jpg"
                img_url = f"https://pixiv.cat/{illust_id}.jpg"
                
                async with session.get(img_url) as img_resp:
                    if img_resp.status == 200:
                        image_data = await img_resp.read()
                        with open(filename, "wb") as f:
                            f.write(image_data)
                    else:
                        print(f"[!] Pixiv.cat returned {img_resp.status}")
                        return

                # 3. Upload to Matrix
                with open(filename, "rb") as f:
                    resp, _ = await self.client.upload(
                        f, 
                        content_type="image/jpeg", 
                        filename=filename
                    )

                if not isinstance(resp, UploadResponse):
                    print(f"[!] Upload failed: {resp}")
                    return

                # 4. Sending as an Image Message (m.image)
                # This ensures the image actually renders in the chat
                await self.client.room_send(
                    room.room_id,
                    "m.room.message",
                    {
                        "msgtype": "m.image",
                        "body": f"{title}.jpg",
                        "url": resp.content_uri,
                        "info": {
                            "mimetype": "image/jpeg",
                            "size": os.path.getsize(filename)
                        },
                        # We still include the HTML for the "View on Pixiv" link below the image
                        "format": "org.matrix.custom.html",
                        "formatted_body": f'<b>{title}</b><br><a href="{pixiv_url}">View on Pixiv</a>'
                    }
                )
                
                os.remove(filename)
                print(f"[+] Successfully posted {illust_id}")

            except Exception as e:
                print(f"[!] Error: {e}")
    async def run(self):
        logged_in = False
        
        if os.path.exists(SESSION_PATH):
            try:
                with open(SESSION_PATH, "r") as f:
                    s = json.load(f)
                self.client.access_token = s["access_token"]
                self.client.device_id = s["device_id"]
                self.client.user_id = s["user_id"]
                
                await self.client.sync(timeout=3000)
                
                print(f"[*] Attempting to restore session for {self.username}...")
                print("[+] Successfully restored session from cache!")
                logged_in = True
            except:
                pass

        if not logged_in:
            print(f"[*] Authenticating with password to {self.homeserver}...")
            resp = await self.client.login(password=self.password)
            if isinstance(resp, LoginResponse):
                with open(SESSION_PATH, "w") as f:
                    json.dump({
                        "access_token": resp.access_token, 
                        "device_id": resp.device_id, 
                        "user_id": resp.user_id
                    }, f, indent=4)
                print("[+] Authentication successful!")
            else:
                print(f"[CRITICAL] Login failed: {resp.message}")
                return

        self.client.add_event_callback(self.handle_message, RoomMessageText)
        self.client.add_event_callback(self.handle_invite, InviteMemberEvent)
        print(f"\n[+] Bot is running as {self.username}. Listening for Pixiv links...")
        await self.client.sync_forever(timeout=30000, full_state=True)

def setup_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f: return json.load(f)
    print("="*60 + "\n PIXIV MATRIX BOT INITIAL SETUP \n" + "="*60)
    hs = input("Homeserver: ") or "https://matrix.org"
    user = input("Username: ")
    pw = input("Password: ")
    conf = {"homeserver": hs, "username": user, "password": pw}
    with open(CONFIG_PATH, "w") as f: json.dump(conf, f, indent=4)
    return conf

if __name__ == "__main__":
    print("[*] Starting the Matrix Pixiv Bot...")
    bot = PixivBot(setup_config())
    asyncio.run(bot.run())
