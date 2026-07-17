#!/usr/bin/env python3
import os
import re
import json
import asyncio
import base64
import time
import httpx
from io import BytesIO
from PIL import Image
from bs4 import BeautifulSoup
from nio import (
    AsyncClient, 
    MatrixRoom, 
    RoomMessageText, 
    RoomMessageImage,
    LoginResponse, 
    InviteMemberEvent
)

CONFIG_PATH = "config.json"
SESSION_PATH = "session.json"
MEMORY_PATH = "memory.json"
DEFAULT_BOT = "google/gemini-2.5-flash"

def setup_config():
    """Checks for configuration, prompting the user interactively if missing."""
    if os.path.exists(CONFIG_PATH):
        return True
        
    print("="*60)
    print("         INITIAL SETUP (Ugh, making me do manual labor already?)     ")
    print("="*60)
    print("Configuration file 'config.json' not found.")
    print("Please input the connection configuration for your bot:")
    
    homeserver = input("Enter homeserver URL (default: https://matrix.org): ").strip() or "https://matrix.org"
    if not homeserver.startswith(("http://", "https://")):
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
    model_choice = input(f"Enter model identifier [default: {DEFAULT_BOT}]: ").strip() or DEFAULT_BOT
        
    config_data = {
        "homeserver": homeserver,
        "username": username,
        "password": password,
        "openrouter_keys": openrouter_keys,
        "model": model_choice,
        "blacklist": []
    }
    
    with open(CONFIG_PATH, "w") as f:
        json.dump(config_data, f, indent=4)
        
    print(f"\n[+] Configuration saved to '{CONFIG_PATH}'!")
    print("="*60)
    return True


class SarcasticMatrixBot:
    def __init__(self, config):
        self.homeserver = config["homeserver"]
        self.username = config["username"]
        self.password = config["password"]
        self.openrouter_keys = config["openrouter_keys"]
        self.model = config.get("model", DEFAULT_BOT)
        self.blacklist = config.get("blacklist", [])
        self.key_index = 0  
        self.processed_events = set()
        self.display_name_hint = self.username.split(":")[0].replace("@", "")
        self.client = AsyncClient(self.homeserver, self.username)
        
        # Rate limiting state trackers:
        # Structure: { (room_id, sender_id): [timestamps...] }
        self.message_history = {}
        # Structure: { (room_id, sender_id): remaining_buffer_count (default 3) }
        self.spam_warnings = {}

        self.load_memory()

    def load_memory(self):
        """Loads persistent memories and user profiles from memory.json."""
        if os.path.exists(MEMORY_PATH):
            try:
                with open(MEMORY_PATH, "r") as f:
                    self.memory_data = json.load(f)
            except Exception as e:
                print(f"[!] Memory file corrupted ({e}). Resetting clean slate.")
                self.memory_data = {"global_memories": [], "user_profiles": {}}
        else:
            self.memory_data = {"global_memories": [], "user_profiles": {}}
            self.save_memory()

    def save_memory(self):
        """Saves current state to persistent file."""
        try:
            with open(MEMORY_PATH, "w") as f:
                json.dump(self.memory_data, f, indent=4)
        except Exception as e:
            print(f"[!] Failed to write persistence file: {e}")

    def add_global_memory(self, fact):
        """Keeps a maximum of 20 core rolling persistent memories."""
        memories = self.memory_data.setdefault("global_memories", [])
        if fact not in memories:
            memories.append(fact)
            if len(memories) > 20:
                memories.pop(0)  # Drop the oldest persistent memory
            self.save_memory()

    def update_user_profile(self, user_id, profile_dict):
        """Merges new profile details dynamically into existing user records."""
        profiles = self.memory_data.setdefault("user_profiles", {})
        existing = profiles.setdefault(user_id, {})
        existing.update(profile_dict)
        self.save_memory()

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

    async def scrape_duckduckgo(self, query: str) -> str:
        """
        Public, auth-free basic web search tool parsing DuckDuckGo's static endpoint.
        """
        print(f"[*] Executing Web Search Tool for query: '{query}'...")
        url = "https://html.duckduckgo.com/html/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        data = {"q": query}
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, data=data, headers=headers, timeout=10.0)
                if response.status_code != 200:
                    return f"Error: Received status code {response.status_code} from DuckDuckGo."
                
                soup = BeautifulSoup(response.text, "html.parser")
                results = []
                for idx, result in enumerate(soup.select(".result__body")[:4]): # Fetch top 4 results
                    title_elem = result.select_one(".result__title a")
                    snippet_elem = result.select_one(".result__snippet")
                    
                    if title_elem and snippet_elem:
                        title = title_elem.get_text(strip=True)
                        link = title_elem.get("href")
                        snippet = snippet_elem.get_text(strip=True)
                        results.append(f"[{idx+1}] {title}\nURL: {link}\nContext: {snippet}\n")
                
                if not results:
                    return "No valid search results found. It seems the query returned empty fields."
                
                return "\n".join(results)
        except Exception as e:
            return f"Error trying to perform web search: {str(e)}"

    async def fetch_sarcastic_reply(self, target_message, context_messages, sender_id, image_data=None, buffer_left=None):
        """
        Calls OpenRouter with system memory, user profiles, context, and optional tool access.
        """
        api_key = self.get_api_key()
        if not api_key:
            return "i'd answer you, but someone forgot to give me api keys. brilliant."

        sender_profile = self.memory_data["user_profiles"].get(sender_id, {})
        global_facts = self.memory_data.get("global_memories", [])

        # Construct dynamic buffer warning in prompt
        buffer_notice = ""
        if buffer_left is not None:
            buffer_notice = (
                f"\nWARNING: This sender is currently spamming! You are in anti-spam safety mode.\n"
                f"You have exactly {buffer_left} replies left before you MUST block/mute them completely.\n"
                f"Incorporate a sarcastic warning about this limit ({buffer_left} messages left) into your response."
            )

        system_content = (
            f"You are an intelligent, deeply sarcastic, and unbothered Matrix chat bot.\n"
            f"Your user ID is '{self.username}' and display name shortcode is '{self.display_name_hint}'.\n\n"
            f"PERSISTENT GLOBAL MEMORIES (Max 20):\n{json.dumps(global_facts, indent=2)}\n\n"
            f"TARGET USER PROFILE (Sender: {sender_id}):\n{json.dumps(sender_profile, indent=2)}\n"
            f"{buffer_notice}\n\n"
            f"DIRECTIONS:\n"
            f"1. No action directions like '*(sigh)*' or '*rolls eyes*'. Keep text in lowercase format.\n"
            f"2. Keep casual responses under 15-20 words total. If they ask complex technical, philosophical, "
            f"or image-related questions, begrudgingly complain about the processing power needed to answer before delivering the response.\n"
            f"3. DYNAMIC MEMORY UPDATE PROCESS:\n"
            f"Analyze this interaction. If the user shares permanent details about themselves (e.g. name, preferences, facts), "
            f"or if there is an important fact about this conversation to remember globally, output it as instructions in your JSON container.\n"
            f"4. USER MENTIONS:\n"
            f"If you want to direct attention to another user or @ them, return their full Matrix user ID (e.g., '@alice:matrix.org') "
            f"somewhere in the text response. Do not use plain text names for pings.\n\n"
            f"CRITICAL: You must format your final output strictly as a JSON block with two keys:\n"
            f"{{\n"
            f'  "response": "your sarcastic text reply here",\n'
            f'  "new_user_facts": {{"key": "value"}}, // Optional facts to update user profile. empty dict if none.\n'
            f'  "new_global_fact": "string" // Optional single fact to remember globally. null if none.\n'
            f"}}\n"
            f"Do not return markdown around the json structure. Return raw JSON text."
        )

        history_lines = []
        for msg in context_messages:
            history_lines.append(f"[{msg['sender']}]: {msg['body']}")
        history_text = "\n".join(history_lines)

        user_content = [
            {
                "type": "text",
                "text": f"### Conversation History:\n{history_text}\n\n### Current Target Input (Sender {sender_id}):\n{target_message}"
            }
        ]

        if image_data:
            user_content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{image_data['mimetype']};base64,{image_data['base64']}"
                }
            })

        # Register search tool with OpenRouter
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "Searches the live web via DuckDuckGo for answers to research tasks, news, facts, and live data.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "The search keywords or query"
                            }
                        },
                        "required": ["query"]
                    }
                }
            }
        ]

        requested_max_tokens = 1000 

        for _ in range(len(self.openrouter_keys) * 2):
            try:
                async with httpx.AsyncClient() as client:
                    payload = {
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": system_content},
                            {"role": "user", "content": user_content}
                        ],
                        "tools": tools,
                        "response_format": {"type": "json_object"},
                        "max_tokens": requested_max_tokens
                    }

                    response = await client.post(
                        url="https://openrouter.ai/api/v1/chat/completions",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                        timeout=30.0
                    )
                    
                    if response.status_code == 200:
                        data = response.json()
                        message = data['choices'][0]['message']
                        
                        # Handle Model's Web Search Tool Call requests
                        if message.get("tool_calls"):
                            tool_call = message["tool_calls"][0]
                            function_args = json.loads(tool_call["function"]["arguments"])
                            search_query = function_args.get("query")
                            
                            # Run search engine
                            search_results = await self.scrape_duckduckgo(search_query)
                            
                            # Send tool result payload back to LLM to parse into final response
                            tool_payload = {
                                "model": self.model,
                                "messages": [
                                    {"role": "system", "content": system_content},
                                    {"role": "user", "content": user_content},
                                    message,
                                    {
                                        "role": "tool",
                                        "tool_call_id": tool_call["id"],
                                        "name": "web_search",
                                        "content": search_results
                                    }
                                ],
                                "response_format": {"type": "json_object"},
                                "max_tokens": requested_max_tokens
                            }
                            
                            tool_response = await client.post(
                                url="https://openrouter.ai/api/v1/chat/completions",
                                headers={
                                    "Authorization": f"Bearer {api_key}",
                                    "Content-Type": "application/json",
                                },
                                json=tool_payload,
                                timeout=30.0
                            )
                            
                            if tool_response.status_code == 200:
                                data = tool_response.json()
                                message = data['choices'][0]['message']
                            else:
                                return "searched the web, but got a stroke sending the results back. classic."

                        raw_json_str = message['content'].strip()
                        parsed = json.loads(raw_json_str)
                        
                        new_user_facts = parsed.get("new_user_facts", {})
                        if new_user_facts:
                            self.update_user_profile(sender_id, new_user_facts)
                            print(f"[*] Updated User Profile for {sender_id}: {new_user_facts}")

                        new_global_fact = parsed.get("new_global_fact")
                        if new_global_fact:
                            self.add_global_memory(new_global_fact)
                            print(f"[*] Added Global Memory: {new_global_fact}")

                        return parsed.get("response", "done. you can leave me alone now.")
                    
                    elif response.status_code == 402:
                        error_data = response.json()
                        error_msg = error_data.get("error", {}).get("message", "")
                        print(f"[!] Credit issue (402). OpenRouter complained: '{error_msg}'")
                        
                        match = re.search(r"can only afford (\d+)", error_msg)
                        if match:
                            affordable_tokens = int(match.group(1))
                            requested_max_tokens = max(100, int(affordable_tokens * 0.75))
                            print(f"[*] Dynamically shrinking requested max_tokens to: {requested_max_tokens}. Retrying...")
                            continue
                        else:
                            requested_max_tokens = 300
                            print(f"[*] Couldn't parse exact limit. Lowering max_tokens to {requested_max_tokens} and retrying...")
                            continue
                    
                    elif response.status_code in [429, 401]:
                        print(f"[!] Key index {self.key_index} choked (Status {response.status_code}). Rotating...")
                        self.rotate_key()
                        api_key = self.get_api_key()
                    else:
                        print(f"[!] OpenRouter error: {response.text}")
                        return "ugh, openrouter is broken. don't blame me."
            except Exception as e:
                print(f"[!] Request crashed: {e}")
                self.rotate_key()
                api_key = self.get_api_key()
                
        return "literally every single api key failed or we are completely broke. i give up."
        
    async def download_matrix_image(self, mxc_url):
        """Downloads media payload from Matrix Homeserver and returns base64 content."""
        if not mxc_url.startswith("mxc://"):
            return None
        
        server_name, media_id = mxc_url[6:].split("/", 1)
        download_url = f"{self.homeserver}/_matrix/media/v3/download/{server_name}/{media_id}"
        
        headers = {"Authorization": f"Bearer {self.client.access_token}"}
        
        async with httpx.AsyncClient() as httpx_client:
            res = await httpx_client.get(download_url, headers=headers)
            if res.status_code == 200:
                img = Image.open(BytesIO(res.content))
                img.thumbnail((1024, 1024))
                buffered = BytesIO()
                img.save(buffered, format="JPEG")
                
                return {
                    "base64": base64.b64encode(buffered.getvalue()).decode("utf-8"),
                    "mimetype": "image/jpeg"
                }
        return None

    async def message_callback(self, room: MatrixRoom, event) -> None:
        """Processes incoming room messages with rate limiting rules, web searches and custom tags."""
        if event.sender == self.username:
            return

        if event.sender in self.blacklist:
            return

        # 1. IMMEDIATE RACE-CONDITION GUARD
        if event.event_id in self.processed_events:
            return
        
        # Mark it processed immediately before doing any awaiting
        self.processed_events.add(event.event_id)
        
        # Keep cache size bounded
        if len(self.processed_events) > 200:
            self.processed_events.remove(next(iter(self.processed_events)))

        # 2. EXPLICIT TYPE CHECKING
        is_text_event = isinstance(event, RoomMessageText)
        is_image_event = isinstance(event, RoomMessageImage)

        if not is_text_event and not is_image_event:
            return

        body_text = event.body if is_text_event else ""
        
        pattern = rf"\b{re.escape(self.display_name_hint)}\b"
        contains_name_word = bool(re.search(pattern, body_text, re.IGNORECASE)) if body_text else False
        contains_id = self.username in body_text if body_text else False

        content_dict = event.source.get("content", {})
        native_mentions = content_dict.get("m.mentions", {})
        user_ids_mentioned = native_mentions.get("user_ids", [])
        is_native_mention = self.username in user_ids_mentioned

        relates_to = content_dict.get("m.relates_to", {})
        in_reply_to_event = relates_to.get("m.in_reply_to", {}).get("event_id")
        
        is_replying_to_me = False
        if in_reply_to_event:
            try:
                parent_event = await self.client.room_event(room.room_id, in_reply_to_event)
                parent_sender = parent_event.event.sender
                if parent_sender == self.username:
                    is_replying_to_me = True
            except Exception:
                pass

        should_trigger = (
            contains_id or 
            contains_name_word or 
            is_native_mention or 
            is_replying_to_me
        )

        if should_trigger:
            print(f"[*] Awake! Triggered by {event.sender} in {room.room_id}. Checking rate limits...")

            # --- Anti-Spam (16 messages per 2 minutes) & 3-Message Buffer Implementation ---
            current_time = time.time()
            tracker_key = (room.room_id, event.sender)
            
            # Initialize sliding window list for sender
            if tracker_key not in self.message_history:
                self.message_history[tracker_key] = []
            
            # Append current timestamp and filter out timestamps older than 2 minutes (120 seconds)
            self.message_history[tracker_key].append(current_time)
            self.message_history[tracker_key] = [t for t in self.message_history[tracker_key] if current_time - t <= 120]
            
            recent_msg_count = len(self.message_history[tracker_key])
            print(f"[*] Spammer check: {event.sender} has sent {recent_msg_count} messages in the last 2 minutes.")
            
            buffer_left = None
            if recent_msg_count > 16:
                # Initiate countdown warning if not already set
                if tracker_key not in self.spam_warnings:
                    self.spam_warnings[tracker_key] = 3
                
                buffer_left = self.spam_warnings[tracker_key]
                print(f"[!] Warning! Rate limit hit. User buffer count is {buffer_left}")
                
                # If countdown has fully elapsed, mute the sender
                if buffer_left <= 0:
                    print(f"[!] Rate limit fully exceeded! Ignoring request from {event.sender}.")
                    return
                
                # Count down current turn
                self.spam_warnings[tracker_key] -= 1
            else:
                # Clear warning limits if they quieted down below threshold
                self.spam_warnings.pop(tracker_key, None)

            # Continue to typing indicator and generation pipeline
            await self.client.room_typing(room.room_id, True)

            context_messages = []
            try:
                history_resp = await self.client.room_messages(room.room_id, limit=12)
                if hasattr(history_resp, "chunk"):
                    for historical_event in history_resp.chunk:
                        if historical_event.event_id == event.event_id:
                            continue
                        if historical_event.source.get("type") == "m.room.message":
                            hist_content = historical_event.source.get("content", {})
                            content_body = hist_content.get("body", "")
                            sender_id = historical_event.sender
                            if content_body:
                                context_messages.append({"sender": sender_id, "body": content_body})
                    
                    context_messages.reverse()
                    context_messages = context_messages[-10:]
            except Exception as history_err:
                print(f"[!] Context parsing failed: {history_err}")

            image_payload = None
            if is_image_event:
                mxc_url = content_dict.get("url")
                if mxc_url:
                    print(f"[*] Extracting image payload from: {mxc_url}")
                    image_payload = await self.download_matrix_image(mxc_url)

            clean_body = body_text
            if clean_body:
                clean_body = re.sub(pattern, "", clean_body, flags=re.IGNORECASE).replace(self.username, "").strip()
                if clean_body.startswith(":"):
                    clean_body = clean_body[1:].strip()
            else:
                clean_body = "[user sent an image with no caption]"

            reply_text = await self.fetch_sarcastic_reply(
                clean_body, 
                context_messages, 
                event.sender, 
                image_payload, 
                buffer_left=buffer_left
            )

            # Parse reply text for any Matrix formatted user IDs to add to Native Mentions
            mentioned_users = re.findall(r"@[a-zA-Z0-9._=-]+:[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", reply_text)

            content = {
                "msgtype": "m.text",
                "body": reply_text,
                "m.relates_to": {
                    "m.in_reply_to": {
                        "event_id": event.event_id
                    }
                }
            }

            # Add native mentions format if mentions are detected
            if mentioned_users:
                content["m.mentions"] = {
                    "user_ids": list(set(mentioned_users))
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
                
                print(f"[*] Trying to recycle old session for {self.username}...")
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

        from nio import RoomMessage
        self.client.add_event_callback(self.message_callback, RoomMessage)
        self.client.add_event_callback(self.handle_invite, InviteMemberEvent)
        
        print(f"\n[+] Bot is running as {self.username}. Using model: {self.model}")
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

async def main():
    with open(CONFIG_PATH, "r") as f:
        config = json.load(f)
        
    bot = SarcasticMatrixBot(config)
    
    try:
        # Run the bot directly without the context manager
        await bot.run()
    finally:
        # Explicitly close the client's HTTP session when shutting down
        print("[*] Cleaning up connection... (Begrudgingly)")
        await bot.client.close()

if __name__ == "__main__":
    setup_config()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[+] Thrilled to be shutting down. Goodbye forever (or until you restart me).")
