import os
import json
import asyncio
import random
from nio import AsyncClient, MatrixRoom, RoomMessageText, LoginResponse, InviteMemberEvent

CONFIG_PATH = "config.json"
SESSION_PATH = "session.json"
DEFAULT_BOT = "openai/gpt-oss-120b:free"

def setup_config():
    """Checks for configuration, prompting the user interactively if missing."""
    if os.path.exists(CONFIG_PATH):
        return True
        
    print("="*60)
    print("                        INITIAL SETUP                       ")
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
        
    print(f"\n[+] Configuration file saved successfully to '{CONFIG_PATH}'!")
    print("="*60)
    return True


class SarcasticMatrixBot:
    def __init__(self, config):
        self.homeserver = config["homeserver"]
        self.username = config["username"]
        self.password = config["password"]
        self.openrouter_keys = config["openrouter_keys"]
        self.model = config.get("model", DEFAULT_BOT)
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
            print(f"[*] Rotating to API key index {self.key_index}...")

    async def fetch_sarcastic_reply(self, target_message, context_messages):
        """Calls OpenRouter with personality instructions, room context history, and bot identity details."""
        import httpx
        
        # Build payload system prompt passing identity dynamically
        system_content = (
            f"You are a deeply sarcastic, lazy, and completely unbothered Matrix chat bot. "
            f"Your Matrix user ID is '{self.username}' and your display name shortcode is '{self.display_name_hint}'. "
            f"You find every interaction tedious. Respond to the user's latest input in a brief, witty, "
            f"and sigh-heavy manner using lowercase format. Do not use action stage directions like '*(sigh)*'.\n\n"
            f"CRITICAL: Ground your response in the conversation context provided below. If the user makes a typo "
            f"or uses broken meme slang, do not arbitrarily assume it is about video games; check the context log "
            f"to see what they were actually discussing."
        )

        # Build context history block safely
        history_text = ""
        if context_messages:
            history_lines = [f"[{msg['sender']}]: {msg['body']}" for msg in context_messages]
            history_text = "\n".join(history_lines)

        # Construct final targeted prompt payload wrapped in clean structure
        user_prompt_payload = (
            f"### Recent Conversation Context History:\n{history_text}\n\n"
            f"### Latest Target Message to reply to:\n<user_input>{target_message}</user_input>"
        )

        # Quick structural check: 1 token is roughly ~4 characters. 80,000 tokens ≈ 320,000 characters.
        if len(user_prompt_payload) > 320000:
            print("[!] Warning: Context context payload exceeds safety token limits. Truncating history...")
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
                        print(f"[!] Key index {self.key_index} failed with status {response.status_code}.")
                        self.rotate_key()
                    else:
                        print(f"[!] OpenRouter API error: {response.text}")
                        return "ugh, openrouter is broken. don't blame me."
            except Exception as e:
                print(f"[!] Request exception: {e}")
                self.rotate_key()
                
        return "literally every single api key failed. i give up."

    async def message_callback(self, room: MatrixRoom, event: RoomMessageText) -> None:
        """Processes incoming room messages and fetches conversational context history structures."""
        if event.sender == self.username:
            return

        contains_id_or_name = (self.username in event.body) or (self.display_name_hint in event.body)
        native_mentions = event.source.get("content", {}).get("m.mentions", {})
        user_ids_mentioned = native_mentions.get("user_ids", [])
        is_native_mention = self.username in user_ids_mentioned

        was_replied_to = (
            event.source.get("content", {}).get("m.relates_to", {}).get("m.in_reply_to", {}).get("event_id") is not None
            and is_native_mention
        )

        if contains_id_or_name or is_native_mention or was_replied_to:
            print(f"[*] Target match triggered by {event.sender} in {room.room_id}: '{event.body}'")
            
            await self.client.room_typing(room.room_id, True)
            
            # Fetch context message history chunk natively from homeserver logs
            context_messages = []
            try:
                history_resp = await self.client.room_messages(room.room_id, limit=11)
                if hasattr(history_resp, "chunk"):
                    # Filter and parse chunk array chronological logs backwards (skipping current event)
                    events_chunk = history_resp.chunk
                    for historical_event in events_chunk:
                        if historical_event.event_id == event.event_id:
                            continue
                        # Gather valid room text messages to build structure
                        if historical_event.source.get("type") == "m.room.message":
                            content_body = historical_event.source.get("content", {}).get("body", "")
                            sender_id = historical_event.sender
                            if content_body:
                                context_messages.append({"sender": sender_id, "body": content_body})
                    
                    # Flip order so history reads sequentially from oldest to newest
                    context_messages.reverse()
                    # Grab exactly up to 10 context messages
                    context_messages = context_messages[-10:]
            except Exception as history_err:
                print(f"[!] Could not look up room context history logs: {history_err}")

            clean_body = event.body.replace(self.username, "").replace(self.display_name_hint, "").strip()
            if clean_body.startswith(":"): 
                clean_body = clean_body[1:].strip()
            
            # Request reply tracking history array data context structures
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
            
    async def run(self):
        """Initializes login state, handles caching, and starts the long sync loop."""
        logged_in = False
        
        if os.path.exists(SESSION_PATH):
            try:
                with open(SESSION_PATH, "r") as f:
                    session_data = json.load(f)
                
                print(f"[*] Attempting to restore session for {self.username}...")
                self.client.access_token = session_data["access_token"]
                self.client.device_id = session_data["device_id"]
                self.client.user_id = session_data["user_id"]
                
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
                print(f"[CRITICAL] Login failed: {getattr(response, 'message', 'Unknown Error')}")
                return

        self.client.add_event_callback(self.message_callback, RoomMessageText)
        self.client.add_event_callback(self.handle_invite, InviteMemberEvent)
        
        print(f"\n[+] Bot is running as {self.username}. Model: {self.model}")
        await self.client.sync_forever(timeout=30000, full_state=True)
        
    async def handle_invite(self, room: MatrixRoom, event: InviteMemberEvent):
        """Automatically joins any room the bot is invited to."""
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

if __name__ == "__main__":
    setup_config()
    with open(CONFIG_PATH, "r") as f:
        config = json.load(f)
        
    bot = SarcasticMatrixBot(config)
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("\n[+] Fine, I'll stop working. Goodbye.")
