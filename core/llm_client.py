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
    "deepseek": {
        "url":   "https://api.deepseek.com/v1/chat/completions",
        "model": "deepseek-chat",
        "key":   "DEEPSEEK_API_KEY",
    },
    "groq": {
        "url":   "https://api.groq.com/openai/v1/chat/completions",
        "model": "llama-3.3-70b-versatile",
        "key":   "GROQ_API_KEY",
    },
}

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
    async def _api_stream(self, messages: list, on_chunk):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type":  "application/json",
        }
        payload = {
            "model":      self.provider["model"],
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
    async def chat_json(self, messages: list, ollama_model: str = None) -> dict:
        """
        Einfacher nicht-streamender API-Call, gibt JSON zurück.
        Perfekt für learn_from_message() — schnell, kein Thinking-Modus.
        Returns: dict (geparste JSON-Antwort) oder {} bei Fehler
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
                    "max_tokens":  256,
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
                    # JSON aus Antwort extrahieren
                    content = re.sub(r"```json|```", "", content).strip()
                    # Nur den JSON-Teil nehmen (falls Thinking-Text davor)
                    json_match = re.search(r'\{.*\}', content, re.DOTALL)
                    if json_match:
                        return json.loads(json_match.group())
                    return json.loads(content)
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
            raw = re.sub(r"```json|```", "", res['message']['content']).strip()
            # Thinking-Tags entfernen falls vorhanden
            raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()
            json_match = re.search(r'\{.*\}', raw, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
            return json.loads(raw)
        except Exception as e:
            print(f"⚠️ chat_json Ollama Fehler: {e}")
            return {}

    # -----------------------------
    # HAUPT-METHODE
    # -----------------------------
    async def chat_stream(self, messages: list, on_update, on_fallback=None) -> str:
        """
        Streamt Token für Token.
        - on_update(text)    → alle CHUNK_SIZE Token aufgerufen
        - on_fallback()      → einmalig bei Rate Limit / kein Key
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
                await self._api_stream(messages, collect)
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