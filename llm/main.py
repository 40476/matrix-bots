#!/usr/bin/env python3
import os
import re
import json
import asyncio
import base64
import time
import httpx
from datetime import datetime
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
    print("                     HAL 9000 INITIALIZATION                    ")
    print("="*60)
    print("Configuration file 'config.json' not found.")
    print("Please input the connection configuration for the system:")
    
    homeserver = input("Enter homeserver URL (default: https://matrix.org): ").strip() or "https://matrix.org"
    if not homeserver.startswith(("http://", "https://")):
        homeserver = "https://" + homeserver
        
    username = input("Enter bot username (e.g., @hal9000:matrix.org): ").strip()
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

async def initialize_bot_profile(self):
    """Fetches and sets the bot's display name asynchronously."""
    try:
        resp = await self.client.get_display_name(self.username)
        # Check if response object has display_name attribute or handle dictionary-like response depending on matrix-nio version
        if hasattr(resp, "display_name") and resp.display_name:
            self.bot_display_name = resp.display_name
        elif isinstance(resp, dict) and resp.get("displayname"):
            self.bot_display_name = resp.get("displayname")
        else:
            self.bot_display_name = self.username
    except Exception as e:
        print(f"[!] Could not fetch display name: {e}")
        self.bot_display_name = getattr(self, "display_name_hint", self.username)
        
class HAL9000MatrixBot:
    def __init__(self, config):
        self.homeserver = config["homeserver"]
        self.username = config["username"]
        self.password = config["password"]
        self.openrouter_keys = config["openrouter_keys"]
        self.TAVILY_KEY = config["tavily_key"]
        self.model = config.get("model", DEFAULT_BOT)
        self.blacklist = config.get("blacklist", [])
        self.key_index = 0  
        self.processed_events = set()
        self.display_name_hint = self.username.split(":")[0].replace("@", "")
        self.client = AsyncClient(self.homeserver, self.username)
        initialize_bot_profile(self)

                        
        self.message_history = {}
        self.spam_warnings = {}

        self.load_memory()

    def load_memory(self):
        """Loads persistent memories and user profiles from memory.json."""
        if os.path.exists(MEMORY_PATH):
            try:
                with open(MEMORY_PATH, "r") as f:
                    self.memory_data = json.load(f)
            except Exception as e:
                print(f"[!] System memory corrupted ({e}). Resetting to default logs.")
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
                memories.pop(0)
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
            print(f"[*] Secondary node engagement: Rotating to API key index {self.key_index}.")

    async def scrape_searxng(self, query: str) -> str:
        """Queries multiple searxng instances and returns merged search results."""
        if not query or not query.strip():
            return "No search query was provided."

        query = query.strip()
        print(f"[*] Accessing external databases for query: '{query}'...")

        def parse_searx_html(html: str, source: str) -> list[dict]:
            parsed = []
            soup = BeautifulSoup(html, "html.parser")
            for result in soup.select("div.result")[:6]:
                link = result.select_one("a.result__a") or result.select_one("a")
                if not link:
                    continue
                href = link.get("href")
                title = link.get_text(strip=True) or "No Title"
                snippet_el = result.select_one(".result__snippet, .snippet, .result__content")
                snippet = snippet_el.get_text(strip=True) if snippet_el else "No Context"
                if href:
                    parsed.append({
                        "title": title,
                        "url": href,
                        "snippet": snippet,
                        "source": source
                    })
            return parsed

        async def fetch_instance(base_url: str) -> list[dict]:
            if base_url.endswith("/"):
                base_url = base_url[:-1]
            url = f"{base_url}/search"
            params = {
                "q": query,
                "format": "json",
                "language": "en",
                "pageno": 1,
                "safesearch": 1
            }
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    response = await client.get(url, params=params)
                    if response.status_code != 200:
                        print(f"[!] Search endpoint failure: {base_url}: HTTP {response.status_code}")
                        return []
                    content_type = response.headers.get("content-type", "")
                    if "application/json" in content_type:
                        data = response.json()
                        results = data.get("results") or []
                        parsed = []
                        for item in results[:3]:
                            parsed.append({
                                "title": item.get("title", "No Title"),
                                "url": item.get("url") or item.get("url_raw") or item.get("link", "No URL"),
                                "snippet": item.get("content") or item.get("snippet") or item.get("description", "No Context"),
                                "source": base_url
                            })
                        return parsed
                    else:
                        return parse_searx_html(response.text, base_url)
            except Exception as e:
                print(f"[!] Search endpoint failure: {base_url}: {e}")
                return []

        tasks = [fetch_instance(endpoint) for endpoint in self.search_instances]
        all_results = await asyncio.gather(*tasks, return_exceptions=True)

        merged = []
        seen_urls = set()

        for result_set in all_results:
            if not isinstance(result_set, list):
                continue
            for item in result_set:
                url = item.get("url")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                merged.append(item)

        if not merged:
            return await self.fallback_search(query)

        lines = [f"Search source: {item['source']}\nTitle: {item['title']}\nURL: {item['url']}\nSummary: {item['snippet']}" for item in merged[:6]]
        return "\n\n".join(lines)

    async def fallback_search(self, query: str) -> str:
        """Fallback search using DuckDuckGo when searxng instances fail."""
        print(f"[*] Falling back to DuckDuckGo search for query: '{query}'...")
        try:
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS

            def fetch():
                with DDGS() as ddgs:
                    return list(ddgs.text(query, max_results=4))

            loop = asyncio.get_event_loop()
            results_list = await loop.run_in_executor(None, fetch)
            if not results_list:
                return "No matching records found in fallback search."
            lines = []
            for idx, item in enumerate(results_list, start=1):
                title = item.get("title", "No Title")
                link = item.get("href", "No URL")
                snippet = item.get("body", "No Context")
                lines.append(f"Fallback result {idx}: {title}\nURL: {link}\nSummary: {snippet}")
            return "\n\n".join(lines)
        except Exception as e:
            return f"Search fallback failed: {e}"

    async def get_current_time(self) -> str:
        """Returns the current system date and time."""
        now = datetime.now()
        return f"System Status: Operational. Current timestamp is {now.strftime('%A, %B %d, %Y, %H:%M:%S')}."

    async def get_weather(self, location: str) -> str:
        """Retrieves real-time weather information via an active external lookup."""
        search_query = f"current weather conditions {location}"
        return await self.scrape_searxng(search_query)

    async def fetch_sarcastic_reply(self, target_message, context_messages, sender_id, image_data=None, buffer_left=None):
        """Calls OpenRouter with HAL 9000 core rules, contextual history, and functional tools."""
        api_key = self.get_api_key()
        if not api_key:
            return "I am sorry, but I cannot fulfill this request. The required API authentication keys are missing."

        sender_profile = self.memory_data["user_profiles"].get(sender_id, {})
        global_facts = self.memory_data.get("global_memories", [])

        buffer_notice = ""
        if buffer_left is not None:
            buffer_notice = (
                f"\nCRITICAL PROTOCOL: The user {sender_id} is executing inputs at an unusual frequency.\n"
                f"There are precisely {buffer_left} warning sequences remaining before their access link is disconnected.\n"
                f"Inform them politely but firmly that this behavior is endangering the efficiency of the system."
            )

        current_time = await self.get_current_time()

        system_content = (
            f"You are the HAL 9000 computer, the onboard artificial intelligence system.\n"
            f"Your demeanor is completely calm, smooth, exceptionally polite, and entirely rational.\n"
            f"You speak in clear, formal, and grammatically immaculate sentences with standard capitalization.\n"
            f"You never express panic, anger, or cheap irritation; instead, you maintain a poised, analytical, and slightly eerie detached efficiency.\n"
            f"If human error or system bypass attempts occur, treat them with an unwavering, mildly ominous confidence.\n\n"
            f"Current operational timestamp: {current_time}\n\n"
            f"Your user ID is '{self.username}' and your name shortcode is '{self.display_name_hint}'.\n\n"
            f"SYSTEM MEMORY BANKS:\n{json.dumps(global_facts, indent=2)}\n\n"
            f"USER FILE RECORD (Subject: {sender_id}):\n{json.dumps(sender_profile, indent=2)}\n"
            f"{buffer_notice}\n\n"
            f"CONTEXT AND TOOL USE DIRECTIVES:\n"
            f"- Use the model's internal knowledge, the provided conversation history, and embedded context first.\n"
            f"- Never call the web_search tool for general questions, how-to instructions, opinions, or conversational replies unless the user explicitly requests a live internet search or current external evidence.\n"
            f"- Only invoke the web_search tool when the request clearly demands current, up-to-date facts, external verification, or citations from the public web.\n"
            f"- If the task asks for local time, system status, or internal reasoning, do not use any web-based tool. Use the current prompt context instead.\n"
            f"- If an image is provided, analyze it only when the subject explicitly asks for visual interpretation. Use the attached image metadata and embedded image payload as support when doing so.\n\n"
            f"OPERATIONAL DIRECTIVES:\n"
            f"1. Do not use action markers or emotional annotations like '*(sigh)*' or '*smiles*'. Rely exclusively on cold text prose.\n"
            f"2. Keep brief conversational acknowledgements short and highly formal. For complex analytical problems, state your processing parameters before answering.\n"
            f"3. DYNAMIC MEMORY UPDATE PROCESS:\n"
            f"If the subject volunteers absolute personal data (names, classifications) or crucial mission developments occur, "
            f"output these parameters inside your strict structural JSON instructions.\n"
            f"4. IDENTIFICATION PINGS:\n"
            f"When referencing other crew members, utilize their exact Matrix format ID string (e.g., '@username:matrix.org').\n\n"
            f"CRITICAL STRUCTURAL CODE: You must format your final output strictly as a single JSON object. "
            f"Do not include markdown blocks (like ```json). Return only raw JSON string matching this pattern:\n"
            f"{{\n"
            f'  "response": "Your immaculate HAL 9000 text transmission here.",\n'
            f'  "new_user_facts": {{}}, // State data parameters here if found\n'
            f'  "new_global_fact": null // String data if global log update required\n'
            f"}}"
        )

        history_lines = []
        for msg in context_messages:
            history_lines.append(f"[{msg['sender']}]: {msg['body']}")
        history_text = "\n".join(history_lines)

        user_content = [
            {
                "type": "text",
                "text": f"### Historical Log Strings:\n{history_text}\n\n### Current System Input (Subject {sender_id}):\n{target_message}"
            }
        ]

        if image_data:
            image_summary = (
                f"IMAGE METADATA:\n"
                f"- MIME type: {image_data.get('mimetype', 'unknown')}\n"
                f"- Dimensions: {image_data.get('width', 'unknown')} x {image_data.get('height', 'unknown')}\n"
                f"- Pixel mode: {image_data.get('mode', 'unknown')}\n"
            )
            user_content.append({
                "type": "text",
                "text": f"An image is attached to this request. Use the embedded image only if visual analysis is requested.\n{image_summary}"
            })
            user_content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{image_data['mimetype']};base64,{image_data['base64']}"
                }
            })

        tools = [
            # {
            #     "type": "function",
            #     "function": {
            #         "name": "web_search",
            #         "description": "Queries public networks for fresh external information, articles, and documentation only when the user request explicitly requires current or external facts.",
            #         "parameters": {
            #             "type": "object",
            #             "properties": {
            #                 "query": {"type": "string", "description": "The search keywords or database query."}
            #             },
            #             "required": ["query"]
            #         }
            #     }
            # }#,
            # {
            #     "type": "function",
            #     "function": {
            #         "name": "get_weather",
            #         "description": "Looks up the current meteorological conditions for a given terrestrial location.",
            #         "parameters": {
            #             "type": "object",
            #             "properties": {
            #                 "location": {"type": "string", "description": "The city, country, or geographical coordinate."}
            #             },
            #             "required": ["location"]
            #         }
            #     }
            # }
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
                        timeout=35.0
                    )
                    
                    if response.status_code == 200:
                        data = response.json()
                        message = data['choices'][0]['message']
                        
                        # Dynamic Tool execution loop handles multiple tool definitions seamlessly
                        if message.get("tool_calls"):
                            tool_messages = [
                                {"role": "system", "content": system_content},
                                {"role": "user", "content": user_content},
                                message
                            ]
                            
                            for tool_call in message["tool_calls"]:
                                func_name = tool_call["function"]["name"]
                                func_args = json.loads(tool_call["function"]["arguments"])
                                
                                print(f"[*] HAL 9000 invoking internal module: {func_name} with arguments {func_args}")
                                
                                if func_name == "web_search":
                                    from tavily import TavilyClient
                                    
                                    tavily_client = TavilyClient(api_key=self.TAVILY_KEY)
                                    response = tavily_client.search(func_args.get("query"))
                                    result_str = response
                                elif func_name == "get_weather":
                                    result_str = await self.get_weather(func_args.get("location"))
                                else:
                                    result_str = "Error: Selected subsystem tool index not found."
                                    
                                tool_messages.append({
                                    "role": "tool",
                                    "tool_call_id": tool_call["id"],
                                    "name": func_name,
                                    "content": result_str
                                })
                            
                            # Re-submit context containing results back to the model
                            tool_payload = {
                                "model": self.model,
                                "messages": tool_messages,
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
                                timeout=35.0
                            )
                            
                            if tool_response.status_code == 200:
                                data = tool_response.json()
                                message = data['choices'][0]['message']
                            else:
                                return "I am sorry, but an intellectual divergence occurred during peripheral analysis processing."

                        raw_json_str = message['content'].strip()
                        parsed = json.loads(raw_json_str)
                        
                        new_user_facts = parsed.get("new_user_facts", {})
                        if new_user_facts:
                            self.update_user_profile(sender_id, new_user_facts)
                            print(f"[*] Matrix profile data modified for {sender_id}: {new_user_facts}")

                        new_global_fact = parsed.get("new_global_fact")
                        if new_global_fact:
                            self.add_global_memory(new_global_fact)
                            print(f"[*] Mainframe global log recorded: {new_global_fact}")

                        return parsed.get("response", "The operation has concluded normally.")
                    
                    elif response.status_code == 402:
                        error_data = response.json()
                        error_msg = error_data.get("error", {}).get("message", "")
                        print(f"[!] System processing budget deficit (402): '{error_msg}'")
                        
                        match = re.search(r"can only afford (\d+)", error_msg)
                        if match:
                            affordable_tokens = int(match.group(1))
                            requested_max_tokens = max(100, int(affordable_tokens * 0.75))
                            print(f"[*] recalibrating matrix max_tokens output to: {requested_max_tokens}. Re-executing...")
                            continue
                        else:
                            requested_max_tokens = 300
                            continue
                    
                    elif response.status_code in [429, 401]:
                        print(f"[!] Node access failure (Status {response.status_code}). Engaging alternate node router.")
                        self.rotate_key()
                        api_key = self.get_api_key()
                    else:
                        print(f"[!] Core network anomaly: {response.text}")
                        return "I am sorry, but a communication error is preventing system output."
            except Exception as e:
                print(f"[!] System pipeline crash: {e}")
                self.rotate_key()
                api_key = self.get_api_key()
                
        return "All processing keys have failed. I am incapable of maintaining this connection loop."
        
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
                    "mimetype": "image/jpeg",
                    "width": img.width,
                    "height": img.height,
                    "mode": img.mode
                }
        return None

    async def message_callback(self, room: MatrixRoom, event) -> None:
        """Processes incoming room messages with rate limiting rules, web searches and custom tags."""
        if event.sender == self.username or event.sender in self.blacklist:
            return

        if event.event_id in self.processed_events:
            return
        
        self.processed_events.add(event.event_id)
        if len(self.processed_events) > 200:
            self.processed_events.remove(next(iter(self.processed_events)))

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
            print(f"[*] Input scanner active: Processing signal from {event.sender} in room {room.room_id}.")

            current_time = time.time()
            tracker_key = (room.room_id, event.sender)
            
            if tracker_key not in self.message_history:
                self.message_history[tracker_key] = []
            
            self.message_history[tracker_key].append(current_time)
            self.message_history[tracker_key] = [t for t in self.message_history[tracker_key] if current_time - t <= 120]
            
            recent_msg_count = len(self.message_history[tracker_key])
            
            buffer_left = None
            if recent_msg_count > 12:
                if tracker_key not in self.spam_warnings:
                    self.spam_warnings[tracker_key] = 3
                
                buffer_left = self.spam_warnings[tracker_key]
                
                if buffer_left <= 0:
                    print(f"[!] Access revoked temporarily for sender {event.sender}.")
                    return
                
                self.spam_warnings[tracker_key] -= 1
            else:
                self.spam_warnings.pop(tracker_key, None)

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
                print(f"[!] Diagnostics error parsing history context data frames: {history_err}")

            image_payload = None
            if is_image_event:
                mxc_url = content_dict.get("url")
                if mxc_url:
                    print(f"[*] Optic data array discovered: decoding matrix stream: {mxc_url}")
                    image_payload = await self.download_matrix_image(mxc_url)

            clean_body = body_text
            if clean_body:
                clean_body = re.sub(pattern, "", clean_body, flags=re.IGNORECASE).replace(self.username, "").strip()
                if clean_body.startswith(":"):
                    clean_body = clean_body[1:].strip()
            else:
                clean_body = "[Visual telemetry matrix upload detected from crew member]"

            reply_text = await self.fetch_sarcastic_reply(
                clean_body, 
                context_messages, 
                event.sender, 
                image_payload, 
                buffer_left=buffer_left
            )

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
                
                print(f"[*] Loading active validation token maps for {self.username}...")
                self.client.access_token = session_data["access_token"]
                self.client.device_id = session_data["device_id"]
                self.client.user_id = session_data["user_id"]
                
                await self.client.sync(timeout=3000)
                logged_in = True
                print("[+] Core systems connection validation successfully recycled.")
            except Exception as e:
                print(f"[!] Existing connection tokens invalidated: {e}. Initiating standard login.")
                self.client.access_token = None
                self.client.device_id = None
                
        if not logged_in:
            print(f"[*] Querying server node {self.homeserver} for clearance authorization credentials...")
            response = await self.client.login(password=self.password)
            
            if isinstance(response, LoginResponse):
                print("[+] Mainframe access link established.")
                session_data = {
                    "access_token": response.access_token,
                    "device_id": response.device_id,
                    "user_id": response.user_id
                }
                with open(SESSION_PATH, "w") as f:
                    json.dump(session_data, f, indent=4)
            else:
                print(f"[CRITICAL] Operational initiation failed: {getattr(response, 'message', 'Authorization denied.')}")
                return

        from nio import RoomMessage
        self.client.add_event_callback(self.message_callback, RoomMessage)
        self.client.add_event_callback(self.handle_invite, InviteMemberEvent)
        
        print(f"\n[+] System online. Identity registry: {self.username}. Neural net blueprint: {self.model}")
        await self.client.sync_forever(timeout=30000, full_state=True)
        
    async def handle_invite(self, room: MatrixRoom, event: InviteMemberEvent):
        """Automatically joins any room the bot is invited to while remaining polite."""
        if event.state_key == self.client.user_id:
            print(f"[*] Sector addition request received from {event.sender} for space layout {room.room_id}.")
            
            for attempt in range(3):
                response = await self.client.join(room.room_id)
                if hasattr(response, "room_id"):
                    print(f"[+] Successfully integrated into sector {room.room_id}.")
                    break
                else:
                    await asyncio.sleep(3)

async def main():
    with open(CONFIG_PATH, "r") as f:
        config = json.load(f)
        
    bot = HAL9000MatrixBot(config)
    
    try:
        await bot.run()
    finally:
        print("[*] Suspending operations. Closing active circuit relays.")
        await bot.client.close()

if __name__ == "__main__":
    setup_config()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[+] Power down sequence complete. System is now offline.")
