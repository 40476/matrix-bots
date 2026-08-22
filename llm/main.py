#!/usr/bin/env python3
"""
A configurable, tool-using Matrix chatbot.

Persona, model choice, tool-calling mode, memory limits, effects, and TTS
settings all live in config.json - nothing persona-related is hardcoded here.

Key features:
- Works with tool-calling-incapable models via a simulated ReAct-style loop
  (set "supports_native_tools": false in config.json).
- Weather via Open-Meteo (no API key required).
- Web search / news research via searxng instances + DuckDuckGo fallback,
  with a multi-query "research" mode for news/current-events questions.
- Rolling conversation summarization so long-running rooms don't blow the
  context window.
- Learns stylistic patterns from other bots in the room (configurable list)
  and can reference them without copying verbatim.
- Can trigger Element's native message effects (confetti, fireworks, etc.)
- Optional TTS voice replies via edge-tts (no signup/API key).
"""

import os
import re
import json
import asyncio
import base64
import time
import random
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
    InviteMemberEvent,
    UploadResponse,
    UnknownEvent,
)

CONFIG_PATH = "config.json"
SESSION_PATH = "session.json"
MEMORY_PATH = "memory.json"
DEFAULT_MODEL = "google/gemini-2.5-flash"

DEFAULT_CONFIG = {
    "supports_native_tools": False,
    "max_tool_iterations": 4,
    "max_output_tokens": 800,
    "blacklist": [],
    "watched_bots": [],
    "voice_replies": False,
    "tts_voice": "en-US-AvaNeural",
    "tts_rate": "+0%",
    "tts_pitch": "+0Hz",
    "persona": {
        "name": "Assistant",
        "shortcode": "bot",
        "system_prompt": "You are a helpful assistant.",
        "backstory": "",
        "lore": "",
        "signoff_style": "",
        "never_do": [],
    },
    "memory": {
        "max_global_memories": 150,
        "max_bot_style_examples_per_bot": 15,
        "max_bot_profile_facts": 40,
        "max_effective_banter_examples": 40,
        "summary_trigger_messages": 30,
        "keep_recent_raw_messages": 12,
    },
    "effects": {
        "confetti": "\U0001F389",
        "fireworks": "\U0001F386",
        "snow": "\u2744\ufe0f",
        "rain": "\u2614",
        "hearts": "\U0001F496",
        "aliens": "\U0001F47E",
    },
    "search_instances": ["https://searx.be", "https://searx.tiekoetter.com"],
}


def deep_merge(base, override):
    result = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def setup_config():
    """Checks for configuration, prompting the user interactively if missing."""
    if os.path.exists(CONFIG_PATH):
        return True

    print("=" * 60)
    print("                  MATRIX BOT INITIALIZATION")
    print("=" * 60)
    print("Configuration file 'config.json' not found.")
    print("Copy config.example.json to config.json and edit it, or answer")
    print("the prompts below for a minimal setup (you can add persona /")
    print("feature settings to config.json afterwards).\n")

    homeserver = input("Enter homeserver URL (default: https://matrix.org): ").strip() or "https://matrix.org"
    if not homeserver.startswith(("http://", "https://")):
        homeserver = "https://" + homeserver

    username = input("Enter bot username (e.g., @mybot:matrix.org): ").strip()
    while not username or not username.startswith("@"):
        username = input("Please enter a valid username starting with '@': ").strip()

    password = input("Enter password: ").strip()
    while not password:
        password = input("Password cannot be blank. Enter password: ").strip()

    or_keys_input = input("Enter OpenRouter API Keys (comma separated): ").strip()
    while not or_keys_input:
        or_keys_input = input("You need at least one OpenRouter key. Enter key(s): ").strip()
    openrouter_keys = [k.strip() for k in or_keys_input.split(",") if k.strip()]

    tavily_key = input("Enter Tavily API key (optional, blank to skip): ").strip()

    model_choice = input(f"Enter model identifier [default: {DEFAULT_MODEL}]: ").strip() or DEFAULT_MODEL

    native_tools = input("Does this model support native OpenRouter tool calling? (y/N): ").strip().lower() == "y"

    config_data = deep_merge(DEFAULT_CONFIG, {
        "homeserver": homeserver,
        "username": username,
        "password": password,
        "openrouter_keys": openrouter_keys,
        "tavily_key": tavily_key,
        "model": model_choice,
        "supports_native_tools": native_tools,
    })

    with open(CONFIG_PATH, "w") as f:
        json.dump(config_data, f, indent=4)

    print(f"\n[+] Configuration saved to '{CONFIG_PATH}'. Edit it any time to")
    print("    change persona, effects, memory limits, or TTS settings.")
    print("=" * 60)
    return True


class MatrixLLMBot:
    def __init__(self, config):
        config = deep_merge(DEFAULT_CONFIG, config)
        self.config = config

        self.homeserver = config["homeserver"]
        self.username = config["username"]
        self.password = config["password"]
        self.openrouter_keys = config["openrouter_keys"]
        self.tavily_key = config.get("tavily_key", "")
        self.model = config.get("model", DEFAULT_MODEL)
        self.supports_native_tools = config.get("supports_native_tools", False)
        self.max_tool_iterations = config.get("max_tool_iterations", 4)
        self.max_output_tokens = config.get("max_output_tokens", 800)
        self.blacklist = config.get("blacklist", [])
        self.watched_bots = config.get("watched_bots", [])
        self.voice_replies = config.get("voice_replies", False)
        self.tts_voice = config.get("tts_voice", "en-US-AvaNeural")
        self.tts_rate = config.get("tts_rate", "+0%")
        self.tts_pitch = config.get("tts_pitch", "+0Hz")
        self.persona = config.get("persona", DEFAULT_CONFIG["persona"])
        self.mem_cfg = config.get("memory", DEFAULT_CONFIG["memory"])
        self.effects_map = config.get("effects", DEFAULT_CONFIG["effects"])
        self.search_instances = config.get("search_instances", DEFAULT_CONFIG["search_instances"])

        self.display_name_hint = self.persona.get("shortcode") or self.username.split(":")[0].replace("@", "")
        self.bot_display_name = self.display_name_hint

        self.key_index = 0
        self.processed_events = set()
        self.message_history = {}     # (room_id, sender) -> [timestamps]  (rate limiting)
        self.spam_warnings = {}
        self.room_raw_history = {}    # room_id -> [{"sender":..,"body":..}, ...] recent raw turns
        self.sent_messages = {}       # event_id -> text, for correlating reactions back to lines

        self.client = AsyncClient(self.homeserver, self.username)

        self.load_memory()

    # ---------------------------------------------------------------- memory

    def load_memory(self):
        if os.path.exists(MEMORY_PATH):
            try:
                with open(MEMORY_PATH, "r") as f:
                    self.memory_data = json.load(f)
            except Exception as e:
                print(f"[!] Memory file corrupted ({e}). Resetting.")
                self.memory_data = {}
        else:
            self.memory_data = {}

        self.memory_data.setdefault("global_memories", [])
        self.memory_data.setdefault("user_profiles", {})
        self.memory_data.setdefault("bot_profiles", {})     # other bots' Matrix IDs -> facts dict
        self.memory_data.setdefault("room_summaries", {})   # room_id -> summary text
        self.memory_data.setdefault("bot_style_examples", {})  # sender -> [messages]
        self.memory_data.setdefault("effective_banter", [])    # [{text, reaction}] - what landed well
        self.save_memory()

    def save_memory(self):
        try:
            with open(MEMORY_PATH, "w") as f:
                json.dump(self.memory_data, f, indent=4)
        except Exception as e:
            print(f"[!] Failed to write memory file: {e}")

    def add_global_memory(self, fact):
        memories = self.memory_data.setdefault("global_memories", [])
        if fact not in memories:
            memories.append(fact)
            limit = self.mem_cfg.get("max_global_memories", 20)
            if len(memories) > limit:
                memories.pop(0)
            self.save_memory()

    def update_user_profile(self, user_id, profile_dict):
        profiles = self.memory_data.setdefault("user_profiles", {})
        existing = profiles.setdefault(user_id, {})
        existing.update(profile_dict)
        self.save_memory()

    def update_bot_profile(self, bot_id, facts_dict):
        """Stores what Nira has learned about another bot (capabilities, quirks,
        how to invoke it) separately from human user profiles, so she can
        reference or @-mention it accurately later."""
        profiles = self.memory_data.setdefault("bot_profiles", {})
        existing = profiles.setdefault(bot_id, {})
        existing.update(facts_dict)
        limit = self.mem_cfg.get("max_bot_profile_facts", 40)
        if len(existing) > limit:
            # trim oldest keys (dicts preserve insertion order in py3.7+)
            for k in list(existing.keys())[: len(existing) - limit]:
                existing.pop(k, None)
        self.save_memory()

    def record_effective_banter(self, text, reaction_emoji):
        """Logs a line of Nira's that got a positive reaction, as a general
        style example - NOT tied to which specific user reacted, so this
        stays 'what kind of banter works' rather than a per-person profile."""
        bucket = self.memory_data.setdefault("effective_banter", [])
        bucket.append({"text": text, "reaction": reaction_emoji})
        limit = self.mem_cfg.get("max_effective_banter_examples", 40)
        if len(bucket) > limit:
            bucket.pop(0)
        self.save_memory()

    def record_bot_style_example(self, sender, body):
        """Logs a message from a watched peer bot as a stylistic reference."""
        examples = self.memory_data.setdefault("bot_style_examples", {})
        bucket = examples.setdefault(sender, [])
        bucket.append(body)
        limit = self.mem_cfg.get("max_bot_style_examples_per_bot", 15)
        if len(bucket) > limit:
            bucket.pop(0)
        self.save_memory()

    def get_room_summary(self, room_id):
        return self.memory_data.get("room_summaries", {}).get(room_id, "")

    def set_room_summary(self, room_id, summary_text):
        self.memory_data.setdefault("room_summaries", {})[room_id] = summary_text
        self.save_memory()

    # ------------------------------------------------------------- API keys

    def get_api_key(self):
        if not self.openrouter_keys:
            return None
        return self.openrouter_keys[self.key_index % len(self.openrouter_keys)]

    def rotate_key(self):
        if len(self.openrouter_keys) > 1:
            self.key_index = (self.key_index + 1) % len(self.openrouter_keys)
            print(f"[*] Rotating to API key index {self.key_index}.")

    # -------------------------------------------------------------- tools

    async def scrape_searxng(self, query: str) -> str:
        """Queries multiple searxng instances and returns merged search results."""
        if not query or not query.strip():
            return "No search query was provided."
        query = query.strip()
        print(f"[*] Searching: '{query}'...")

        def parse_searx_html(html: str, source: str) -> list:
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
                    parsed.append({"title": title, "url": href, "snippet": snippet, "source": source})
            return parsed

        async def fetch_instance(base_url: str) -> list:
            base_url = base_url.rstrip("/")
            url = f"{base_url}/search"
            params = {"q": query, "format": "json", "language": "en", "pageno": 1, "safesearch": 1}
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    response = await client.get(url, params=params)
                    if response.status_code != 200:
                        return []
                    content_type = response.headers.get("content-type", "")
                    if "application/json" in content_type:
                        data = response.json()
                        results = data.get("results") or []
                        return [{
                            "title": item.get("title", "No Title"),
                            "url": item.get("url") or item.get("url_raw") or item.get("link", "No URL"),
                            "snippet": item.get("content") or item.get("snippet") or item.get("description", "No Context"),
                            "source": base_url,
                        } for item in results[:3]]
                    return parse_searx_html(response.text, base_url)
            except Exception as e:
                print(f"[!] Search endpoint failure: {base_url}: {e}")
                return []

        tasks = [fetch_instance(endpoint) for endpoint in self.search_instances]
        all_results = await asyncio.gather(*tasks, return_exceptions=True)

        merged, seen_urls = [], set()
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

        lines = [f"Source: {i['source']}\nTitle: {i['title']}\nURL: {i['url']}\nSummary: {i['snippet']}" for i in merged[:6]]
        return "\n\n".join(lines)

    async def fallback_search(self, query: str) -> str:
        print(f"[*] Falling back to DuckDuckGo for: '{query}'...")
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
                return "No results found."
            lines = []
            for idx, item in enumerate(results_list, start=1):
                lines.append(
                    f"Result {idx}: {item.get('title', 'No Title')}\n"
                    f"URL: {item.get('href', 'No URL')}\nSummary: {item.get('body', 'No Context')}"
                )
            return "\n\n".join(lines)
        except Exception as e:
            return f"Search fallback failed: {e}"

    async def web_search_tool(self, query: str) -> str:
        """Prefers Tavily (if configured) then falls back to searxng/DuckDuckGo."""
        if self.tavily_key:
            try:
                from tavily import TavilyClient
                loop = asyncio.get_event_loop()
                client = TavilyClient(api_key=self.tavily_key)
                result = await loop.run_in_executor(None, lambda: client.search(query))
                items = result.get("results", []) if isinstance(result, dict) else []
                if items:
                    lines = [f"Title: {i.get('title')}\nURL: {i.get('url')}\nSummary: {i.get('content')}" for i in items[:6]]
                    return "\n\n".join(lines)
            except Exception as e:
                print(f"[!] Tavily search failed, falling back: {e}")
        return await self.scrape_searxng(query)

    async def research_tool(self, topic: str) -> str:
        """Deeper multi-angle research for news / current-events questions:
        fires several related queries and returns the merged evidence for the
        model to synthesize into an explanation."""
        angles = [topic, f"{topic} latest news", f"{topic} explained background", f"{topic} reactions analysis"]
        results = await asyncio.gather(*[self.web_search_tool(a) for a in angles], return_exceptions=True)
        chunks = []
        for angle, res in zip(angles, results):
            if isinstance(res, Exception):
                continue
            chunks.append(f"=== Query: {angle} ===\n{res}")
        return "\n\n".join(chunks) if chunks else "No research results found."

    async def get_weather_tool(self, location: str) -> str:
        """Free weather lookup via Open-Meteo - no API key required."""
        if not location or not location.strip():
            return "No location was provided."
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                geo = await client.get(
                    "https://geocoding-api.open-meteo.com/v1/search",
                    params={"name": location.strip(), "count": 1, "language": "en", "format": "json"},
                )
                geo_data = geo.json()
                results = geo_data.get("results")
                if not results:
                    return f"Could not resolve a location for '{location}'."
                place = results[0]
                lat, lon = place["latitude"], place["longitude"]
                place_label = f"{place.get('name')}, {place.get('country', '')}".strip(", ")

                forecast = await client.get(
                    "https://api.open-meteo.com/v1/forecast",
                    params={
                        "latitude": lat,
                        "longitude": lon,
                        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m",
                        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                        "timezone": "auto",
                        "forecast_days": 1,
                    },
                )
                fc = forecast.json()
                cur = fc.get("current", {})
                daily = fc.get("daily", {})
                summary = (
                    f"Weather for {place_label}: currently {cur.get('temperature_2m')}\u00b0C "
                    f"(feels like {cur.get('apparent_temperature')}\u00b0C), "
                    f"humidity {cur.get('relative_humidity_2m')}%, "
                    f"wind {cur.get('wind_speed_10m')} km/h, "
                    f"precipitation {cur.get('precipitation')} mm. "
                    f"Today's range: {daily.get('temperature_2m_min', ['?'])[0]}\u2013"
                    f"{daily.get('temperature_2m_max', ['?'])[0]}\u00b0C, "
                    f"precipitation chance {daily.get('precipitation_probability_max', ['?'])[0]}%."
                )
                return summary
        except Exception as e:
            return f"Weather lookup failed: {e}"

    async def get_current_time(self) -> str:
        now = datetime.now()
        return f"Current timestamp: {now.strftime('%A, %B %d, %Y, %H:%M:%S')}."

    async def execute_tool(self, name: str, args: dict) -> str:
        print(f"[*] Executing tool: {name} with args {args}")
        if name == "web_search":
            return await self.web_search_tool(args.get("query", ""))
        elif name == "research_topic":
            return await self.research_tool(args.get("topic", ""))
        elif name == "get_weather":
            return await self.get_weather_tool(args.get("location", ""))
        elif name == "get_current_time":
            return await self.get_current_time()
        return f"Error: unknown tool '{name}'."

    def tool_definitions(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "General web search for current facts, articles, or verification.",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string", "description": "Search keywords."}},
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "research_topic",
                    "description": "Deeper multi-query research for news or current-events questions the user wants explained.",
                    "parameters": {
                        "type": "object",
                        "properties": {"topic": {"type": "string", "description": "The news topic or event to research."}},
                        "required": ["topic"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Current weather conditions for a location.",
                    "parameters": {
                        "type": "object",
                        "properties": {"location": {"type": "string", "description": "City, region, or place name."}},
                        "required": ["location"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_current_time",
                    "description": "The current date and time.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]

    # --------------------------------------------------------- prompt build

    def build_system_prompt(self, sender_id, buffer_left, tool_mode_hint=True):
        sender_profile = self.memory_data["user_profiles"].get(sender_id, {})
        global_facts = self.memory_data.get("global_memories", [])

        buffer_notice = ""
        if buffer_left is not None:
            buffer_notice = (
                f"\nNOTE: {sender_id} is messaging unusually fast. {buffer_left} warnings remain "
                f"before they're rate-limited. Mention this once, briefly, in character."
            )

        never_do = self.persona.get("never_do", [])
        never_do_txt = "\n".join(f"- {x}" for x in never_do) if never_do else ""

        backstory = self.persona.get("backstory", "")
        lore = self.persona.get("lore", "")
        backstory_block = ""
        if backstory or lore:
            backstory_block = (
                f"\nBACKSTORY:\n{backstory}\n" if backstory else ""
            ) + (
                f"\nWORLD LORE (draw on this naturally, don't recite it):\n{lore}\n" if lore else ""
            )

        bot_profiles = self.memory_data.get("bot_profiles", {})
        bot_profiles_txt = ""
        if bot_profiles:
            bot_profiles_txt = (
                "\nKNOWN BOTS ON THIS SERVER (facts you've learned about them - use their exact "
                "Matrix ID to @-mention one if it's genuinely useful to loop them in):\n"
                + json.dumps(bot_profiles, indent=2)
            )

        banter_examples = self.memory_data.get("effective_banter", [])
        banter_txt = ""
        if banter_examples:
            sample = random.sample(banter_examples, min(5, len(banter_examples)))
            banter_txt = (
                "\nBANTER THAT HAS LANDED WELL BEFORE (people reacted positively - use as a general "
                "style reference for what tone/jokes work, don't repeat these lines verbatim):\n"
                + "\n".join(f"- \"{b['text']}\" (got: {b['reaction']})" for b in sample)
            )

        style_examples_txt = ""
        if self.watched_bots:
            examples = self.memory_data.get("bot_style_examples", {})
            snippets = []
            for bot_id in self.watched_bots:
                bucket = examples.get(bot_id, [])
                if bucket:
                    snippets.extend(random.sample(bucket, min(2, len(bucket))))
            if snippets:
                style_examples_txt = (
                    "\nOBSERVED PEER TRANSMISSIONS (for stylistic inspiration only - "
                    "never copy these verbatim, just let them inform phrasing/rhythm):\n"
                    + "\n".join(f"- {s}" for s in snippets[:6])
                )

        tool_instructions = ""
        if tool_mode_hint and not self.supports_native_tools:
            tool_instructions = (
                "\nTOOLS (simulated - this model has no native function calling):\n"
                "You may use tools by responding with ONLY this JSON and nothing else:\n"
                '{"tool_call": {"name": "<tool_name>", "arguments": {...}}}\n'
                "Available tools: web_search(query), research_topic(topic), "
                "get_weather(location), get_current_time().\n"
                "Only call a tool when the request genuinely needs current/external facts "
                "(news, weather, verification) - not for general knowledge or conversation.\n"
                "After a tool result is given back to you, either call another tool or give "
                "your final answer using the JSON schema below.\n"
            )
        elif tool_mode_hint:
            tool_instructions = (
                "\nYou have native function-calling tools available: web_search, research_topic, "
                "get_weather, get_current_time. Only call one when the request needs current or "
                "external facts.\n"
            )

        effect_keys = ", ".join(self.effects_map.keys())

        system_content = (
            f"{self.persona.get('system_prompt', '')}\n"
            f"{self.persona.get('signoff_style', '')}\n"
            f"{never_do_txt}\n"
            f"{backstory_block}\n"
            f"Your Matrix user ID is '{self.username}', name shortcode '{self.display_name_hint}'.\n\n"
            f"MEMORY BANK (persistent facts you've learned, not tied to one person):\n{json.dumps(global_facts, indent=2)}\n\n"
            f"USER PROFILE (Subject: {sender_id}):\n{json.dumps(sender_profile, indent=2)}\n"
            f"{buffer_notice}\n"
            f"{style_examples_txt}\n"
            f"{bot_profiles_txt}\n"
            f"{banter_txt}\n"
            f"{tool_instructions}\n"
            f"MEMORY UPDATES: if the user shares a durable personal fact, include it in new_user_facts. "
            f"If you learn something about ANOTHER BOT on this server (its purpose, how to invoke it, "
            f"its quirks), put its exact Matrix ID and the facts in new_bot_facts. If something worth "
            f"remembering long-term and not tied to one person happens, use new_global_fact.\n\n"
            f"BOT MENTIONS: if it's genuinely useful to loop in another known bot right now, set "
            f"\"mention_bot\" to its exact Matrix ID (e.g. \"@otherbot:matrix.org\"), otherwise null. "
            f"Only do this when it actually helps - not as a bit.\n\n"
            f"EFFECTS: you may optionally trigger a Matrix visual effect by setting \"effect\" to "
            f"one of: {effect_keys}, or null for none. Use sparingly, only when it truly fits the moment.\n\n"
            f"VOICE: you may set \"speak\": true if this reply would land better read aloud "
            f"(e.g. the user asked to hear it, or it's a short punchy line worth voicing).\n\n"
            f"Respond with ONLY a single raw JSON object (no markdown fences) matching:\n"
            f"{{\n"
            f'  "response": "your in-character text reply",\n'
            f'  "new_user_facts": {{}},\n'
            f'  "new_global_fact": null,\n'
            f'  "new_bot_facts": {{}},\n'
            f'  "mention_bot": null,\n'
            f'  "effect": null,\n'
            f'  "speak": false\n'
            f"}}"
        )
        return system_content

    # ------------------------------------------------------- summarization

    async def maybe_summarize_room(self, room_id):
        """Compresses older turns into a rolling summary once history grows long."""
        history = self.room_raw_history.get(room_id, [])
        trigger = self.mem_cfg.get("summary_trigger_messages", 30)
        keep = self.mem_cfg.get("keep_recent_raw_messages", 12)
        if len(history) < trigger:
            return

        to_compress = history[:-keep]
        remaining = history[-keep:]
        if not to_compress:
            return

        transcript = "\n".join(f"[{m['sender']}]: {m['body']}" for m in to_compress)
        prior_summary = self.get_room_summary(room_id)

        prompt = (
            "Summarize the key facts, decisions, running jokes, and unresolved threads from this "
            "conversation excerpt in 5-10 bullet points. Merge with the prior summary if given; "
            "keep it dense and factual, no fluff.\n\n"
            f"PRIOR SUMMARY:\n{prior_summary or '(none)'}\n\nNEW EXCERPT:\n{transcript}"
        )

        api_key = self.get_api_key()
        if not api_key:
            self.room_raw_history[room_id] = remaining
            return

        try:
            async with httpx.AsyncClient(timeout=35.0) as client:
                resp = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "model": self.model,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 400,
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    new_summary = data["choices"][0]["message"]["content"].strip()
                    self.set_room_summary(room_id, new_summary)
                    print(f"[*] Compressed {len(to_compress)} turns into rolling summary for {room_id}.")
        except Exception as e:
            print(f"[!] Summarization failed: {e}")

        self.room_raw_history[room_id] = remaining

    # -------------------------------------------------------------- LLM call

    @staticmethod
    def _extract_json(raw: str):
        """Best-effort JSON extraction in case the model wraps output in prose/fences."""
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
            raw = re.sub(r"```$", "", raw).strip()
        try:
            return json.loads(raw)
        except Exception:
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(0))
                except Exception:
                    return None
        return None

    async def fetch_reply(self, target_message, context_messages, sender_id, image_data=None, buffer_left=None):
        """Calls OpenRouter with persona rules, context, memory, and tool access
        (native tool-calling if supported, simulated ReAct loop otherwise)."""
        api_key = self.get_api_key()
        if not api_key:
            return {"response": "I can't reach my language centers right now - no API key is configured.",
                     "new_user_facts": {}, "new_global_fact": None, "effect": None, "speak": False}

        system_content = self.build_system_prompt(sender_id, buffer_left)

        history_text = "\n".join(f"[{m['sender']}]: {m['body']}" for m in context_messages)
        room_summary = ""
        user_content = [{
            "type": "text",
            "text": f"### Conversation so far:\n{history_text}\n\n### New message from {sender_id}:\n{target_message}",
        }]

        if image_data:
            user_content.append({
                "type": "text",
                "text": (f"An image is attached. MIME: {image_data.get('mimetype')}, "
                         f"{image_data.get('width')}x{image_data.get('height')}."),
            })
            user_content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{image_data['mimetype']};base64,{image_data['base64']}"},
            })

        messages = [{"role": "system", "content": system_content}, {"role": "user", "content": user_content}]
        tools = self.tool_definitions() if self.supports_native_tools else None

        max_tokens = self.max_output_tokens

        for iteration in range(self.max_tool_iterations + 1):
            payload = {"model": self.model, "messages": messages, "max_tokens": max_tokens}
            if tools:
                payload["tools"] = tools
            if not self.supports_native_tools:
                payload["response_format"] = {"type": "json_object"}

            for attempt in range(len(self.openrouter_keys) * 2):
                try:
                    async with httpx.AsyncClient() as client:
                        resp = await client.post(
                            "https://openrouter.ai/api/v1/chat/completions",
                            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                            json=payload,
                            timeout=35.0,
                        )

                        if resp.status_code == 200:
                            data = resp.json()
                            message = data["choices"][0]["message"]

                            # --- native tool calling path ---
                            if message.get("tool_calls"):
                                messages.append(message)
                                for tc in message["tool_calls"]:
                                    fname = tc["function"]["name"]
                                    fargs = json.loads(tc["function"]["arguments"] or "{}")
                                    result_str = await self.execute_tool(fname, fargs)
                                    messages.append({
                                        "role": "tool", "tool_call_id": tc["id"],
                                        "name": fname, "content": result_str,
                                    })
                                break  # go to next outer iteration to re-call the model

                            raw_content = (message.get("content") or "").strip()

                            # --- simulated tool calling path ---
                            if not self.supports_native_tools:
                                parsed = self._extract_json(raw_content)
                                if parsed and "tool_call" in parsed and "response" not in parsed:
                                    call = parsed["tool_call"]
                                    fname = call.get("name")
                                    fargs = call.get("arguments", {})
                                    result_str = await self.execute_tool(fname, fargs)
                                    messages.append({"role": "assistant", "content": raw_content})
                                    messages.append({
                                        "role": "user",
                                        "content": f"TOOL RESULT for {fname}: {result_str}\n\n"
                                                    f"Now continue: call another tool if needed, or give your "
                                                    f"final answer in the required JSON schema.",
                                    })
                                    break  # go to next outer iteration

                            # --- final answer ---
                            parsed = self._extract_json(raw_content) or {"response": raw_content}
                            parsed.setdefault("new_user_facts", {})
                            parsed.setdefault("new_global_fact", None)
                            parsed.setdefault("new_bot_facts", {})
                            parsed.setdefault("mention_bot", None)
                            parsed.setdefault("effect", None)
                            parsed.setdefault("speak", False)
                            parsed.setdefault("response", "...")

                            if parsed["new_user_facts"]:
                                self.update_user_profile(sender_id, parsed["new_user_facts"])
                            if parsed["new_global_fact"]:
                                self.add_global_memory(parsed["new_global_fact"])
                            if parsed["new_bot_facts"]:
                                for bot_id, facts in parsed["new_bot_facts"].items():
                                    if isinstance(facts, dict):
                                        self.update_bot_profile(bot_id, facts)

                            return parsed

                        elif resp.status_code == 402:
                            error_data = resp.json()
                            error_msg = error_data.get("error", {}).get("message", "")
                            match = re.search(r"can only afford (\d+)", error_msg)
                            if match:
                                max_tokens = max(100, int(int(match.group(1)) * 0.75))
                                payload["max_tokens"] = max_tokens
                                continue
                            max_tokens = 300
                            payload["max_tokens"] = max_tokens
                            continue

                        elif resp.status_code in (429, 401):
                            self.rotate_key()
                            api_key = self.get_api_key()
                        else:
                            print(f"[!] API error {resp.status_code}: {resp.text[:300]}")
                            return {"response": "Something went wrong reaching my language model.",
                                     "new_user_facts": {}, "new_global_fact": None, "effect": None, "speak": False}
                except Exception as e:
                    print(f"[!] Request failed: {e}")
                    self.rotate_key()
                    api_key = self.get_api_key()
            else:
                # exhausted retries in inner loop without success/break
                return {"response": "All API keys failed - I can't respond right now.",
                         "new_user_facts": {}, "new_global_fact": None, "effect": None, "speak": False}
        return {"response": "I got stuck in a thought loop and ran out of tool-call attempts.",
                 "new_user_facts": {}, "new_global_fact": None, "effect": None, "speak": False}

    # ------------------------------------------------------------------ TTS

    async def synthesize_speech(self, text: str) -> bytes:
        """Free, no-signup TTS via edge-tts (Microsoft Edge's read-aloud voices)."""
        import edge_tts
        communicate = edge_tts.Communicate(text, voice=self.tts_voice, rate=self.tts_rate, pitch=self.tts_pitch)
        buf = BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        return buf.getvalue()

    async def send_voice_message(self, room_id, text):
        try:
            audio_bytes = await self.synthesize_speech(text)
        except Exception as e:
            print(f"[!] TTS synthesis failed: {e}")
            return
        try:
            upload_resp, _ = await self.client.upload(
                lambda *_: BytesIO(audio_bytes), content_type="audio/mpeg", filesize=len(audio_bytes)
            )
            if not isinstance(upload_resp, UploadResponse):
                print(f"[!] TTS upload failed: {upload_resp}")
                return
            await self.client.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content={
                    "msgtype": "m.audio",
                    "body": "voice-reply.mp3",
                    "url": upload_resp.content_uri,
                    "info": {"mimetype": "audio/mpeg", "size": len(audio_bytes)},
                },
            )
        except Exception as e:
            print(f"[!] Sending voice message failed: {e}")

    # -------------------------------------------------------------- effects

    def apply_effect(self, text, effect_key):
        """Element's client detects certain emoji in a plain-text message body and
        auto-plays a visual effect (confetti, fireworks, snowfall, etc). We just
        need the relevant emoji present in the sent body."""
        emoji = self.effects_map.get(effect_key)
        if not emoji:
            return text
        if emoji in text:
            return text
        return f"{text} {emoji}"

    # --------------------------------------------------------------- media

    async def download_matrix_image(self, mxc_url):
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
                    "mimetype": "image/jpeg", "width": img.width, "height": img.height, "mode": img.mode,
                }
        return None

    # ---------------------------------------------------------- msg handler

    async def message_callback(self, room: MatrixRoom, event) -> None:
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

        # Track raw history per room for context + summarization + bot learning
        if body_text:
            hist = self.room_raw_history.setdefault(room.room_id, [])
            hist.append({"sender": event.sender, "body": body_text})
            if len(hist) > 200:
                hist.pop(0)
            if event.sender in self.watched_bots:
                self.record_bot_style_example(event.sender, body_text)
            await self.maybe_summarize_room(room.room_id)

        pattern = rf"\b{re.escape(self.display_name_hint)}\b"
        contains_name_word = bool(re.search(pattern, body_text, re.IGNORECASE)) if body_text else False
        contains_id = self.username in body_text if body_text else False

        content_dict = event.source.get("content", {})
        native_mentions = content_dict.get("m.mentions", {})
        is_native_mention = self.username in native_mentions.get("user_ids", [])

        relates_to = content_dict.get("m.relates_to", {})
        in_reply_to_event = relates_to.get("m.in_reply_to", {}).get("event_id")
        is_replying_to_me = False
        if in_reply_to_event:
            try:
                parent_event = await self.client.room_event(room.room_id, in_reply_to_event)
                if parent_event.event.sender == self.username:
                    is_replying_to_me = True
            except Exception:
                pass

        should_trigger = contains_id or contains_name_word or is_native_mention or is_replying_to_me
        if not should_trigger:
            return

        print(f"[*] Triggered by {event.sender} in {room.room_id}.")

        current_time = time.time()
        tracker_key = (room.room_id, event.sender)
        self.message_history.setdefault(tracker_key, []).append(current_time)
        self.message_history[tracker_key] = [t for t in self.message_history[tracker_key] if current_time - t <= 120]
        recent_msg_count = len(self.message_history[tracker_key])

        buffer_left = None
        if recent_msg_count > 12:
            self.spam_warnings.setdefault(tracker_key, 3)
            buffer_left = self.spam_warnings[tracker_key]
            if buffer_left <= 0:
                print(f"[!] Rate limit hit for {event.sender}, ignoring.")
                return
            self.spam_warnings[tracker_key] -= 1
        else:
            self.spam_warnings.pop(tracker_key, None)

        await self.client.room_typing(room.room_id, True)

        room_summary = self.get_room_summary(room.room_id)
        recent_ctx = self.room_raw_history.get(room.room_id, [])[-self.mem_cfg.get("keep_recent_raw_messages", 12):]
        context_messages = list(recent_ctx)
        if room_summary:
            context_messages.insert(0, {"sender": "system-summary", "body": room_summary})

        image_payload = None
        if is_image_event:
            mxc_url = content_dict.get("url")
            if mxc_url:
                image_payload = await self.download_matrix_image(mxc_url)

        clean_body = body_text
        if clean_body:
            clean_body = re.sub(pattern, "", clean_body, flags=re.IGNORECASE).replace(self.username, "").strip()
            if clean_body.startswith(":"):
                clean_body = clean_body[1:].strip()
        else:
            clean_body = "[Image attached, no caption]"

        result = await self.fetch_reply(clean_body, context_messages, event.sender, image_payload, buffer_left)

        reply_text = result.get("response", "...")
        effect_key = result.get("effect")
        if effect_key:
            reply_text = self.apply_effect(reply_text, effect_key)

        mention_bot = result.get("mention_bot")
        if mention_bot and mention_bot not in reply_text:
            reply_text = f"{reply_text} ({mention_bot})"

        mentioned_users = set(re.findall(r"@[a-zA-Z0-9._=-]+:[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", reply_text))
        if mention_bot:
            mentioned_users.add(mention_bot)

        content = {
            "msgtype": "m.text",
            "body": reply_text,
            "m.relates_to": {"m.in_reply_to": {"event_id": event.event_id}},
        }
        if mentioned_users:
            content["m.mentions"] = {"user_ids": list(mentioned_users)}

        send_resp = await self.client.room_send(room_id=room.room_id, message_type="m.room.message", content=content)
        sent_event_id = getattr(send_resp, "event_id", None)
        if sent_event_id:
            self.sent_messages[sent_event_id] = result.get("response", reply_text)
            if len(self.sent_messages) > 300:
                self.sent_messages.pop(next(iter(self.sent_messages)))

        await self.client.room_typing(room.room_id, False)

        if self.voice_replies or result.get("speak"):
            asyncio.create_task(self.send_voice_message(room.room_id, result.get("response", "")))

    async def reaction_callback(self, room: MatrixRoom, event) -> None:
        """Watches for emoji reactions to Nira's own messages and logs which
        lines landed well, as a general (not per-user) banter style signal."""
        if event.source.get("type") != "m.reaction" or event.sender == self.username:
            return
        content = event.source.get("content", {})
        relates = content.get("m.relates_to", {})
        if relates.get("rel_type") != "m.annotation":
            return
        target_event_id = relates.get("event_id")
        emoji = relates.get("key", "")
        original_text = self.sent_messages.get(target_event_id)
        if original_text:
            self.record_effective_banter(original_text, emoji)

    # --------------------------------------------------------------- run

    async def run(self):
        logged_in = False
        if os.path.exists(SESSION_PATH):
            try:
                with open(SESSION_PATH, "r") as f:
                    session_data = json.load(f)
                self.client.access_token = session_data["access_token"]
                self.client.device_id = session_data["device_id"]
                self.client.user_id = session_data["user_id"]
                await self.client.sync(timeout=3000)
                logged_in = True
                print("[+] Restored existing session.")
            except Exception as e:
                print(f"[!] Session restore failed: {e}. Logging in fresh.")
                self.client.access_token = None
                self.client.device_id = None

        if not logged_in:
            print(f"[*] Logging in to {self.homeserver}...")
            response = await self.client.login(password=self.password)
            if isinstance(response, LoginResponse):
                print("[+] Login successful.")
                with open(SESSION_PATH, "w") as f:
                    json.dump({
                        "access_token": response.access_token,
                        "device_id": response.device_id,
                        "user_id": response.user_id,
                    }, f, indent=4)
            else:
                print(f"[CRITICAL] Login failed: {getattr(response, 'message', 'unknown error')}")
                return

        from nio import RoomMessage
        self.client.add_event_callback(self.message_callback, RoomMessage)
        self.client.add_event_callback(self.handle_invite, InviteMemberEvent)
        self.client.add_event_callback(self.reaction_callback, UnknownEvent)

        print(f"\n[+] Online as {self.username}. Model: {self.model}. Persona: {self.persona.get('name')}")
        await self.client.sync_forever(timeout=30000, full_state=True)

    async def handle_invite(self, room: MatrixRoom, event: InviteMemberEvent):
        if event.state_key == self.client.user_id:
            print(f"[*] Invited to {room.room_id} by {event.sender}.")
            for attempt in range(3):
                response = await self.client.join(room.room_id)
                if hasattr(response, "room_id"):
                    print(f"[+] Joined {room.room_id}.")
                    break
                await asyncio.sleep(3)


async def main():
    with open(CONFIG_PATH, "r") as f:
        config = json.load(f)
    bot = MatrixLLMBot(config)
    try:
        await bot.run()
    finally:
        print("[*] Shutting down.")
        await bot.client.close()


if __name__ == "__main__":
    setup_config()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[+] Offline.")
