#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import re
import asyncio
import httpx
import ollama
from dotenv import load_dotenv

load_dotenv()

# -----------------------------
# PROVIDER CONFIGS
# -----------------------------
PROVIDERS = {
    "deepseek": {                        # Flash / V3 — Standard für alle normalen Calls
        "url":   "https://api.deepseek.com/v1/chat/completions",
        "model": "deepseek-chat",        # = DeepSeek V3 Flash
        "key":   "DEEPSEEK_API_KEY",
    },
    "deepseek-reasoner": {               # Thinking / R1 — für Orchestrator & komplexe Aufgaben
        "url":   "https://api.deepseek.com/v1/chat/completions",
        "model": "deepseek-reasoner",    # = DeepSeek R1 (Chain-of-Thought)
        "key":   "DEEPSEEK_API_KEY",
    },
    "groq": {
        "url":   "https://api.groq.com/openai/v1/chat/completions",
        "model": "llama-3.3-70b-versatile",
        "key":   "GROQ_API_KEY",
    },
}

# Model-Shortcuts für externe Aufrufer
DS_FLASH    = "deepseek-chat"       # schnell, günstig — Standard
DS_THINKING = "deepseek-reasoner"  # Thinking/R1 — für Orchestrator

CHUNK_SIZE = 15  # Token zwischen Telegram-Updates

# -----------------------------
# EXCEPTIONS
# -----------------------------
class RateLimitError(Exception):
    pass

class NoKeyError(Exception):
    pass

# -----------------------------
# CLIENT
# -----------------------------
class LLMClient:
    def __init__(self):
        self.reload()

    def reload(self):
        """Provider + Key aus ENV laden (hot-reload möglich)."""
        self.provider_name  = os.getenv("LLM_PROVIDER", "deepseek").lower()
        self.provider       = PROVIDERS.get(self.provider_name, PROVIDERS["deepseek"])
        self.api_key        = os.getenv(self.provider["key"], "")
        self.ollama_model   = os.getenv("OLLAMA_MODEL", "qwen3:8b")
        self.using_fallback = False

    def is_available(self) -> bool:
        return bool(self.api_key)

    # -----------------------------
    # API STREAM (DeepSeek / Groq)
    # -----------------------------
    async def _api_stream(self, messages: list, on_chunk, model_override: str = None):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type":  "application/json",
        }
        model = model_override or self.provider["model"]
        payload = {
            "model":      model,
            "messages":   messages,
            "stream":     True,
            "max_tokens": 1024,
        }

        async with httpx.AsyncClient(timeout=60) as client:
            async with client.stream(
                "POST",
                self.provider["url"],
                headers=headers,
                json=payload
            ) as response:
                if response.status_code == 429:
                    raise RateLimitError(f"{self.provider_name} Rate Limit")
                if response.status_code == 402:
                    raise RateLimitError(f"{self.provider_name} Guthaben leer")
                if response.status_code != 200:
                    raise Exception(f"{self.provider_name} HTTP {response.status_code}")

                async for line in response.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        delta = json.loads(data)["choices"][0]["delta"].get("content", "")
                        if delta:
                            await on_chunk(delta)
                    except:
                        continue

    # -----------------------------
    # OLLAMA STREAM (Fallback)
    # -----------------------------
    async def _ollama_stream(self, messages: list, on_chunk):
        loop = asyncio.get_event_loop()
        stream = await loop.run_in_executor(
            None,
            lambda: ollama.chat(model=self.ollama_model, messages=messages, stream=True)
        )
        for chunk in stream:
            delta = chunk.get("message", {}).get("content", "")
            if delta:
                await on_chunk(delta)

    # -----------------------------
    # JSON CALL (kein Streaming)
    # Für Hintergrundaufgaben wie learn_from_message()
    # Nutzt Deepseek API falls verfügbar, sonst Ollama Fallback
    # -----------------------------
    @staticmethod
    def _extract_json(raw: str):
        """
        Extrahiert das erste gültige JSON-Objekt oder Array aus einem String.
        Nutzt raw_decode → stoppt nach dem ersten vollständigen JSON-Wert,
        ignoriert Trailing-Text oder mehrere JSON-Blöcke sauber.
        """
        raw = re.sub(r"```json|```", "", raw).strip()
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        decoder = json.JSONDecoder()
        # Erstes { oder [ suchen und von dort raw_decode aufrufen
        # Frühestes { oder [ gewinnt — korrekt für Arrays und Objekte
        candidates = sorted(
            [(raw.find(c), c) for c in ("{", "[") if raw.find(c) != -1]
        )
        for start, _ in candidates:
            if start == -1:
                continue
            try:
                obj, _ = decoder.raw_decode(raw, start)
                return obj
            except json.JSONDecodeError:
                continue
        # Letzter Versuch: direkt parsen
        return json.loads(raw)

    async def chat_json(self, messages: list, ollama_model: str = None, max_tokens: int = 256):
        """
        Nicht-streamender API-Call, gibt geparste JSON-Antwort zurück.
        Returns: dict oder list (je nach LLM-Antwort), {} bei Fehler.
        """
        self.reload()

        # --- Versuch: Deepseek / Groq API ---
        if self.is_available():
            try:
                headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type":  "application/json",
                }
                payload = {
                    "model":       self.provider["model"],
                    "messages":    messages,
                    "stream":      False,
                    "max_tokens":  max_tokens,
                    "temperature": 0,
                }
                async with httpx.AsyncClient(timeout=30) as client:
                    response = await client.post(
                        self.provider["url"],
                        headers=headers,
                        json=payload
                    )
                if response.status_code == 200:
                    content = response.json()["choices"][0]["message"]["content"].strip()
                    return self._extract_json(content)
            except Exception as e:
                print(f"⚠️ chat_json API Fehler: {e} → Ollama Fallback")

        # --- Fallback: Ollama lokal ---
        try:
            model = ollama_model or self.ollama_model
            loop  = asyncio.get_event_loop()
            res   = await loop.run_in_executor(
                None,
                lambda: ollama.chat(
                    model=model,
                    messages=messages,
                    format="json",
                    options={"temperature": 0}
                )
            )
            raw = res["message"]["content"]
            return self._extract_json(raw)
        except Exception as e:
            print(f"⚠️ chat_json Ollama Fehler: {e}")
            return {}

    # -----------------------------
    # HAUPT-METHODE
    # -----------------------------
    async def chat_stream(self, messages: list, on_update, on_fallback=None,
                          model_override: str = None) -> str:
        """
        Streamt Token für Token.
        - on_update(text)       → alle CHUNK_SIZE Token aufgerufen
        - on_fallback()         → einmalig bei Rate Limit / kein Key
        - model_override (str)  → überschreibt Provider-Modell (z.B. DS_THINKING)
        Returns: vollständiger Antworttext
        """
        self.reload()
        full_text   = ""
        token_count = 0
        self.using_fallback = False

        async def collect(delta: str):
            nonlocal full_text, token_count
            full_text   += delta
            token_count += 1
            if token_count % CHUNK_SIZE == 0:
                await on_update(full_text)

        # --- Versuch: API Provider ---
        if self.is_available():
            try:
                await self._api_stream(messages, collect, model_override=model_override)
            except (RateLimitError, NoKeyError) as e:
                print(f"⚡ {e} → Fallback auf Ollama")
                self.using_fallback = True
                if on_fallback:
                    await on_fallback()
                full_text   = ""
                token_count = 0
                await self._ollama_stream(messages, collect)
            except Exception as e:
                print(f"❌ API Fehler: {e} → Fallback auf Ollama")
                self.using_fallback = True
                if on_fallback:
                    await on_fallback()
                full_text   = ""
                token_count = 0
                await self._ollama_stream(messages, collect)
        else:
            # Kein Key → direkt Ollama
            self.using_fallback = True
            await self._ollama_stream(messages, collect)

        # Finales Update
        await on_update(full_text)
        return full_text


# -----------------------------
# SINGLETON
# -----------------------------
_client = None

def get_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client