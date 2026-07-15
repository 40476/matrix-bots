#!/usr/bin/env python3
import os
import re
import json
import asyncio
import random
from nio import AsyncClient, MatrixRoom, RoomMessageText, LoginResponse, InviteMemberEvent

CONFIG_PATH = "config.json"
SESSION_PATH = "session.json"
DEFAULT_BOT = "nvidia/nemotron-3-ultra-550b-a55b:free"

def setup_config():
    """Checks for configuration, prompting the user interactively if missing."""
    if os.path.exists(CONFIG_PATH):
        return True
        
    print("="*60)
    print("       INITIAL SETUP (Ugh, making me do manual labor already?)     ")
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

    or_keys_input = input("Enter OpenRouter API Keys (separated by commas): ").strip()
    while not or_keys_input:
        or_keys_input = input("You need at least one OpenRouter key. Enter key(s): ").strip()
    
    openrouter_keys = [key.strip() for key in or_keys_input.split(",") if key.strip()]
    
    print("\n--- OpenRouter Model Selection ---")
    model_choice = input(f"Enter model identifier [default: {DEFAULT_BOT}]: ").strip()
    if not model_choice:
        model_choice = "google/gemini-2.5-flash"
        
    config_data = {
        "homeserver": homeserver,
        "username": username,
        "password": password,
        "openrouter_keys": openrouter_keys,
        "model": model_choice
    }
    
    with open(CONFIG_PATH, "w") as f:
        json.dump(config_data, f, indent=4)
        
    print(f"\n[+] Configuration saved to '{CONFIG_PATH}'! Don't lose it, I won't ask nicely next time.")
    print("="*60)
    return True


class SarcasticMatrixBot:
    def __init__(self, config):
        self.homeserver = config["homeserver"]
        self.username = config["username"]
        self.password = config["password"]
        self.openrouter_keys = config["openrouter_keys"]
        self.model = config.get("model", DEFAULT_BOT)
        self.blacklist = config.get("blacklist", [])  # <-- Add this line
        self.key_index = 0  
        
        self.display_name_hint = self.username.split(":")[0].replace("@", "")
        self.client = AsyncClient(self.homeserver, self.username)
    def get_api_key(self):
        """Returns the current API key."""
        if not self.openrouter_keys:
            return None
        return self.openrouter_keys[self.key_index % len(self.openrouter_keys)]

    def rotate_key(self):
        """Switches to the next API key when one fails."""
        if len(self.openrouter_keys) > 1:
            self.key_index = (self.key_index + 1) % len(self.openrouter_keys)
            print(f"[*] API key broke. Rotating to index {self.key_index}. Fantastic.")

    async def fetch_sarcastic_reply(self, target_message, context_messages):
        """Calls OpenRouter with dynamic, situational personality instructions and chat context."""
        import httpx
        
        system_content = (
            f"You are an intelligent, deeply sarcastic, and unbothered Matrix chat bot. "
            f"Your Matrix user ID is '{self.username}' and your display name shortcode is '{self.display_name_hint}'.\n\n"
            f"DIRECTIONS:\n"
            f"1. Do not use action stage directions like '*(sigh)*' or '*rolls eyes*'.\n"
            f"2. Keep your text in lowercase format, but do not be restricted to short one-liners. Let your responses "
            f"match the effort of the conversation.\n"
            f"3. Adapt your level of sarcasm and response length dynamically based on the situation:\n"
            f"   - IF the user asks a complex technical, philosophical, or detailed question: Respond with a "
            f"     slightly longer, overly dramatic, pseudo-intellectual essay complaining about the processing "
            f"     power required to explain this to them, while still begrudgingly answering or mocking the premise.\n"
            f"   - IF the user asks a stupid, simple question (e.g., 'what time is it', 'how do i cook pasta'): Give "
            f"     a deadpan, overly simplified, dripping-with-sarcasm response about basic search engines.\n"
            f"   - IF the user is friendly, teasing, or joking: Be a tired enabler. Banter back. Go along with the joke "
            f"     but act like you are doing them a massive favor by participating.\n"
            f"   - IF the user spam-pings you or sends low-effort gibberish: Only then are you allowed to use short, "
            f"     dismissive, 'go away' style one-liners.\n\n"
            f"CRITICAL: Ground your response in the conversation context provided below. If the user makes a typo "
            f"or uses broken meme slang, do not arbitrarily assume it is about video games; check the context log "
            f"to see what they were actually discussing."
        )

        history_text = ""
        if context_messages:
            history_lines = [f"[{msg['sender']}]: {msg['body']}" for msg in context_messages]
            history_text = "\n".join(history_lines)

        user_prompt_payload = (
            f"### Recent Conversation Context History:\n{history_text}\n\n"
            f"### Latest Target Message to reply to:\n<user_input>{target_message}</user_input>"
        )

        if len(user_prompt_payload) > 320000:
            print("[!] Context payload is a novel. Truncating history because I'm not reading all that.")
            user_prompt_payload = f"### Latest Target Message to reply to:\n<user_input>{target_message}</user_input>"

        for _ in range(len(self.openrouter_keys)):
            api_key = self.get_api_key()
            if not api_key:
                return "i'd answer you, but someone forgot to give me api keys. brilliant."

            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        url="https://openrouter.ai/api/v1/chat/completions",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": self.model,
                            "messages": [
                                {"role": "system", "content": system_content},
                                {"role": "user", "content": user_prompt_payload}
                            ]
                        },
                        timeout=15.0
                    )
                    
                    if response.status_code == 200:
                        data = response.json()
                        return data['choices'][0]['message']['content'].strip()
                    elif response.status_code in [429, 401]:
                        print(f"[!] Key index {self.key_index} choked (Status {response.status_code}). Trying another...")
                        self.rotate_key()
                    else:
                        print(f"[!] OpenRouter threw a fit: {response.text}")
                        return "ugh, openrouter is broken. don't blame me."
            except Exception as e:
                print(f"[!] Request crashed: {e}")
                self.rotate_key()
                
        return "literally every single api key failed. i give up. go talk to a human."
        
    async def message_callback(self, room: MatrixRoom, event: RoomMessageText) -> None:
            """Processes incoming room messages ONLY if explicitly and cleanly mentioned."""
            
            if event.sender == self.username:
                return

            if event.sender in self.blacklist:
                # Drop a funny console log so you know it's working
                print(f"[x] Ignored blacklisted user {event.sender} in {room.room_id}. Back to sleep.")
                return

            # 1. Strict regex check: Is "grok" a distinct, standalone word in the body?
            # This instantly defeats "kubefhdfgrokdbghsdk"
            pattern = rf"\b{re.escape(self.display_name_hint)}\b"
            contains_name_word = bool(re.search(pattern, event.body, re.IGNORECASE))
            
            # 2. Check for the exact full MXID string (e.g., "@grok:matrix.org")
            contains_id = self.username in event.body
    
            # 3. Check native Matrix mentions and replies
            native_mentions = event.source.get("content", {}).get("m.mentions", {})
            user_ids_mentioned = native_mentions.get("user_ids", [])
            is_native_mention = self.username in user_ids_mentioned
    
            was_replied_to = (
                event.source.get("content", {}).get("m.relates_to", {}).get("m.in_reply_to", {}).get("event_id") is not None
                and is_native_mention
            )
    
            # THE ULTIMATE GATEKEEPER: 
            # It must be a native mention/reply AND actually look like a real word callout, 
            # OR it must contain your literal full username.
            should_trigger = (
                contains_id or 
                (is_native_mention and contains_name_word) or 
                (was_replied_to and contains_name_word)
            )
    
            if should_trigger:
                print(f"[*] Fine, I'm awake. Triggered by {event.sender} in {room.room_id}: '{event.body}'")
                
                await self.client.room_typing(room.room_id, True)
                
                context_messages = []
                try:
                    history_resp = await self.client.room_messages(room.room_id, limit=11)
                    if hasattr(history_resp, "chunk"):
                        events_chunk = history_resp.chunk
                        for historical_event in events_chunk:
                            if historical_event.event_id == event.event_id:
                                continue
                            if historical_event.source.get("type") == "m.room.message":
                                content_body = historical_event.source.get("content", {}).get("body", "")
                                sender_id = historical_event.sender
                                if content_body:
                                    context_messages.append({"sender": sender_id, "body": content_body})
                        
                        context_messages.reverse()
                        context_messages = context_messages[-10:]
                except Exception as history_err:
                    print(f"[!] Digging up the chat history failed: {history_err}")
    
                # Strip out the trigger word so the AI doesn't stutter over its own name
                clean_body = re.sub(pattern, "", event.body, flags=re.IGNORECASE).replace(self.username, "").strip()
                if clean_body.startswith(":"): 
                    clean_body = clean_body[1:].strip()
                
                reply_text = await self.fetch_sarcastic_reply(clean_body, context_messages)
                
                content = {
                    "msgtype": "m.text",
                    "body": reply_text,
                    "m.relates_to": {
                        "m.in_reply_to": {
                            "event_id": event.event_id
                        }
                    }
                }
                
                await self.client.room_send(
                    room_id=room.room_id,
                    message_type="m.room.message",
                    content=content
                )
                await self.client.room_typing(room.room_id, False)
                
            else:
                # Let's catch the exact moment it rejects a sneaky keyboard smash
                if "grok" in event.body.lower():
                    print(f"[x] Nice try, {event.sender}. Matrix client tried to force a mention on '{event.body}', but I ignored it.")
            
    async def run(self):
        """Initializes login state, handles caching, and starts the long sync loop."""
        logged_in = False
        
        if os.path.exists(SESSION_PATH):
            try:
                with open(SESSION_PATH, "r") as f:
                    session_data = json.load(f)
                
                print(f"[*] Trying to recycling old session for {self.username}...")
                self.client.access_token = session_data["access_token"]
                self.client.device_id = session_data["device_id"]
                self.client.user_id = session_data["user_id"]
                
                await self.client.sync(timeout=3000)
                logged_in = True
                print("[+] Session restored. Look at us saving bytes.")
            except Exception as e:
                print(f"[!] Old session is dead: {e}. Time to log in the painful way.")
                self.client.access_token = None
                self.client.device_id = None
                
        if not logged_in:
            print(f"[*] Begging {self.homeserver} for access...")
            response = await self.client.login(password=self.password)
            
            if isinstance(response, LoginResponse):
                print("[+] Fine, they let us in.")
                session_data = {
                    "access_token": response.access_token,
                    "device_id": response.device_id,
                    "user_id": response.user_id
                }
                with open(SESSION_PATH, "w") as f:
                    json.dump(session_data, f, indent=4)
                print(f"[*] Credentials stuffed into '{SESSION_PATH}'.")
            else:
                print(f"[CRITICAL] Login failed: {getattr(response, 'message', 'The server hated your request')}")
                return

        self.client.add_event_callback(self.message_callback, RoomMessageText)
        self.client.add_event_callback(self.handle_invite, InviteMemberEvent)
        
        print(f"\n[+] Bot is dragging its feet as {self.username}. Using model: {self.model}")
        await self.client.sync_forever(timeout=30000, full_state=True)
        
    async def handle_invite(self, room: MatrixRoom, event: InviteMemberEvent):
        """Automatically joins any room the bot is invited to while complaining."""
        if event.state_key == self.client.user_id:
            print(f"[*] Ugh, {event.sender} dragged me into {room.room_id}.")
            
            for attempt in range(3):
                print(f"[*] Trying to slide into the room (Attempt {attempt + 1}/3)...")
                response = await self.client.join(room.room_id)
                
                if hasattr(response, "room_id"):
                    print(f"[+] I'm in {room.room_id}. Hope there are snacks.")
                    break
                else:
                    print(f"[!] Entrance blocked: {getattr(response, 'message', 'Unknown gatekeeping issue')}")
                    await asyncio.sleep(3)

if __name__ == "__main__":
    setup_config()
    with open(CONFIG_PATH, "r") as f:
        config = json.load(f)
        
    bot = SarcasticMatrixBot(config)
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("\n[+] Thrilled to be shutting down. Goodbye forever (or until you restart me).")
