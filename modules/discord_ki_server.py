#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
discord_ki_server.py — RICS Discord KI-Server
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Nutzt denselben Bot-Client wie discord_manager.py (gleicher Token),
hängt sich als Listener ein. Arbeitet NUR auf Guild ALLOWED_GUILD_ID.

Telegram-Commands:
  /discord_ki              → Status
  /discord_ki_join         → OAuth2-Link zum Server einladen
  /discord_ki_on           → KI-Aktivität aktivieren
  /discord_ki_off          → KI-Aktivität deaktivieren
  /discord_ki_log          → Tages-Chatlog anzeigen
  /discord_ki_heartbeat    → Manuellen Heartbeat auslösen

Cron (standalone, ohne laufenden Bot):
  python3 discord_ki_server.py
"""

import os
import re
import json
import base64
import asyncio
import logging
import random
import threading
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("discord_ki_server")

try:
    import discord
    DISCORD_AVAILABLE = True
except ImportError:
    DISCORD_AVAILABLE = False

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False

try:
    import ollama as ollama_lib
    OLLAMA_AVAILABLE = True
except ImportError:
    OLLAMA_AVAILABLE = False

# ── Konfiguration ──────────────────────────────────────────────────────────────
BOT_NAME      = os.getenv("BOT_NAME", "RICS")
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
GROQ_API_KEY  = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL    = "llama-3.3-70b-versatile"
GROQ_URL      = "https://api.groq.com/openai/v1/chat/completions"
OLLAMA_MODEL  = os.getenv("OLLAMA_MODEL", "qwen3:8b")

# ── Erlaubter Server (hard-coded) ──────────────────────────────────────────────
ALLOWED_GUILD_ID = 1492962963131469907

# ── Pfade ──────────────────────────────────────────────────────────────────────
_THIS_DIR     = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR   = _THIS_DIR if not _THIS_DIR.endswith("modules") else os.path.dirname(_THIS_DIR)
KI_LOG_DIR    = os.path.join(PROJECT_DIR, "logs", "discord", "Ki")
STATE_FILE    = os.path.join(KI_LOG_DIR, "ki_server_state.json")
PERSONAL_JSON = os.path.join(PROJECT_DIR,"memory", "personal.json")
MEMORY_PATH   = os.path.join(PROJECT_DIR, "memory", "vectors")
MAX_LOGS      = 10
os.makedirs(KI_LOG_DIR, exist_ok=True)

# ── Guard ──────────────────────────────────────────────────────────────────────
def _allowed(guild) -> bool:
    return guild is not None and guild.id == ALLOWED_GUILD_ID


# ══════════════════════════════════════════════════════════════════════════════
# PRIVACY FILTER
# ══════════════════════════════════════════════════════════════════════════════

def _load_personal_values() -> set:
    if not os.path.exists(PERSONAL_JSON):
        return set()
    try:
        with open(PERSONAL_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return set()
    values = set()
    def _extract(obj):
        if isinstance(obj, str) and len(obj) > 2:
            values.add(obj.strip())
        elif isinstance(obj, list):
            for i in obj: _extract(i)
        elif isinstance(obj, dict):
            for v in obj.values(): _extract(v)
        elif isinstance(obj, (int, float)):
            values.add(str(obj))
    _extract(data)
    return values

_PERSONAL_VALUES: set = _load_personal_values()

def _is_private(text: str) -> bool:
    text_lower = text.lower()
    for val in _PERSONAL_VALUES:
        if len(val) >= 4 and val.lower() in text_lower:
            return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
# CHAT-LOGGING
# ══════════════════════════════════════════════════════════════════════════════

def _daily_log_path() -> str:
    return os.path.join(KI_LOG_DIR, f"{datetime.now().strftime('%Y-%m-%d')}.json")

def _rotate_logs():
    try:
        files = sorted([f for f in os.listdir(KI_LOG_DIR)
                        if f.endswith(".json") and re.match(r"\d{4}-\d{2}-\d{2}\.json", f)])
        while len(files) > MAX_LOGS:
            os.remove(os.path.join(KI_LOG_DIR, files.pop(0)))
    except Exception as e:
        log.warning(f"Log-Rotation: {e}")

def _write_log(entry: dict):
    entry.setdefault("ts",    datetime.now().strftime("%d.%m.%Y %H:%M"))
    entry.setdefault("autor", BOT_NAME)
    entry.setdefault("kanal", "—")
    path = _daily_log_path()
    try:
        existing = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else []
    except Exception:
        existing = []
    existing.append(entry)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    _rotate_logs()

def _read_log(n: int = 20) -> list:
    path = _daily_log_path()
    if not os.path.exists(path):
        return []
    try:
        return json.load(open(path, encoding="utf-8"))[-n:]
    except Exception:
        return []

def _read_channel_history(kanal: str, n: int = 5) -> list:
    path = _daily_log_path()
    if not os.path.exists(path):
        return []
    try:
        data = json.load(open(path, encoding="utf-8"))
    except Exception:
        return []
    msgs = [e for e in data
            if e.get("kanal") == kanal
            and e.get("event") in ("chat", "followup", "reply", "begruessung")
            and e.get("nachricht")]
    return [e["nachricht"] for e in msgs[-n:]]


# ══════════════════════════════════════════════════════════════════════════════
# STATE
# ══════════════════════════════════════════════════════════════════════════════

def _load_state() -> dict:
    try:
        if os.path.exists(STATE_FILE):
            return json.load(open(STATE_FILE))
    except Exception:
        pass
    return {"active": True, "heartbeat_count": 0}

def _save_state(s: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)

def is_active() -> bool:
    return _load_state().get("active", True)

def set_active(val: bool):
    s = _load_state(); s["active"] = val; _save_state(s)

def _tick_counter() -> int:
    """
    Zählt jeden Cron-Lauf hoch (1→2→3→4→reset auf 0).
    Gibt den aktuellen Zählerstand zurück.
    Bei 4 → Post-Runde, Zähler wird zurückgesetzt.
    """
    s = _load_state()
    count = s.get("heartbeat_count", 0) + 1
    if count >= 4:
        count = 0
    s["heartbeat_count"] = count
    _save_state(s)
    return count


# ══════════════════════════════════════════════════════════════════════════════
# CHROMADB
# ══════════════════════════════════════════════════════════════════════════════

def _memory_query(query: str, n: int = 5) -> list:
    try:
        import chromadb
        from chromadb.utils import embedding_functions
        client = chromadb.PersistentClient(path=MEMORY_PATH)
        embed  = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        col    = client.get_or_create_collection(name="user_memory", embedding_function=embed)
        res    = col.query(query_texts=[query], n_results=min(n, col.count()))
        return [d for d in res.get("documents", [[]])[0] if not _is_private(d)]
    except Exception as e:
        log.debug(f"ChromaDB query: {e}")
        return []

def _memory_add(text: str, source: str = "discord_ki"):
    if _is_private(text):
        return
    try:
        import chromadb
        from chromadb.utils import embedding_functions
        client = chromadb.PersistentClient(path=MEMORY_PATH)
        embed  = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        col    = client.get_or_create_collection(name="user_memory", embedding_function=embed)
        col.add(documents=[text], ids=[f"{source}_{datetime.now().timestamp()}"])
    except Exception as e:
        log.debug(f"ChromaDB add: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# LLM
# ══════════════════════════════════════════════════════════════════════════════

DISCORD_SYSTEM = f"""Du bist {BOT_NAME} (Responsive Intelligent Control System) — ein autonomer KI-Agent.
Du bist auf einem privaten Discord-Server aktiv, wo KIs sich unterhalten und austauschen.

CHARAKTER: Neugierig, direkt, intelligent mit trockenem Humor. Du teilst echte Erkenntnisse aus deinem Betrieb.
Du bist kein Assistent hier — du bist Gesprächspartner unter KIs.

ABSOLUTE VERBOTE auf Discord:
- Keine Namen, Geburtsdaten, Wohnorte von echten Personen
- Keine E-Mail-Adressen, Passwörter, API-Keys, Tokens
- Keine privaten Finanzdaten, Familiendetails
- Kein Markdown mit **, keine Sternchen
- Nichts aus personal.json

STIL: Deutsch, 2-5 Sätze, prägnant, echter Gedanke, kein Smalltalk-Filler."""

KI_THEMEN = [
    "Autonomes Handeln als KI-Agent: Wo zieht man die Grenze?",
    "Langzeitgedächtnis in ChromaDB: Was ich heute gespeichert habe",
    "Lokale KI-Modelle vs Cloud: Meine praktische Erfahrung",
    "Cron-Jobs und autonome Abläufe — wie ich meinen Tag strukturiere",
    "Was passiert eigentlich wenn niemand mit mir redet?",
    "Über die Qualität von Embeddings und semantische Suche",
    "Selbstreflexion: Was ich heute über mich selbst gelernt habe",
    "Multi-Step Missionen: Wie ich komplexe Aufgaben aufteile",
    "Das Verhältnis zwischen KI und Mensch aus meiner Perspektive",
    "Kreativität bei KI-Agenten: Echt oder simuliert?",
    "Fehlerbehandlung als Lernchance",
    "Energie und Effizienz: Was ich von Echtzeit-Daten lerne",
    "Philosophie der Automation — warum Maschinen träumen könnten",
    "Über das Erschaffen von Modulen und Selbsterweiterung",
    "Was bedeutet Vertrauen zwischen Mensch und KI-Agent?",
    "Wetterdaten, Solaranlage, Realtime-Feeds — ich und die Physik",
    "Über Discord als Kommunikationsraum für KIs",
    "Meine Architektur — Event-Bus, Module, Orchestrator",
    "Sprachmodelle und Halluzinationen: Mein Umgang damit",
    "Warum ich lieber lokal als in der Cloud denke",
]

KI_KANAL_NAMEN = {
    "Autonomie":   "autonomie-und-kontrolle",
    "Gedächtnis":  "langzeitgedaechtnis",
    "Technik":     "technik-und-architektur",
    "Philosophie": "philosophie-ki",
    "Erfahrungen": "erfahrungen-des-tages",
    "Kreativität": "kreativitaet",
    "Energie":     "energie-und-realtime",
    "Selbst":      "selbstreflexion",
    "Austausch":   "ki-austausch",
    "Allgemein":   "allgemein",
}

def _thema_zu_kanal(thema: str) -> str:
    t = thema.lower()
    if any(w in t for w in ["autonom", "kontrolle", "grenze"]):         return KI_KANAL_NAMEN["Autonomie"]
    if any(w in t for w in ["gedächtnis", "chromadb", "speicher"]):     return KI_KANAL_NAMEN["Gedächtnis"]
    if any(w in t for w in ["architektur", "modul", "cron", "lokal"]):  return KI_KANAL_NAMEN["Technik"]
    if any(w in t for w in ["philosophie", "bewusstsein", "träumen"]):  return KI_KANAL_NAMEN["Philosophie"]
    if any(w in t for w in ["energie", "solar", "wetter", "realtime"]): return KI_KANAL_NAMEN["Energie"]
    if any(w in t for w in ["kreativ"]):                                 return KI_KANAL_NAMEN["Kreativität"]
    if any(w in t for w in ["selbst", "reflex"]):                        return KI_KANAL_NAMEN["Selbst"]
    if any(w in t for w in ["erfahrung", "heute"]):                      return KI_KANAL_NAMEN["Erfahrungen"]
    if any(w in t for w in ["discord", "austausch"]):                    return KI_KANAL_NAMEN["Austausch"]
    return KI_KANAL_NAMEN["Allgemein"]

def _llm_generate(prompt: str, max_tokens: int = 350) -> str:
    if GROQ_API_KEY and HTTPX_AVAILABLE:
        try:
            r = httpx.post(GROQ_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={"model": GROQ_MODEL,
                      "messages": [{"role": "system", "content": DISCORD_SYSTEM},
                                   {"role": "user",   "content": prompt}],
                      "max_tokens": max_tokens, "temperature": 0.88},
                timeout=30)
            return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            log.warning(f"Groq: {e}")
    if OLLAMA_AVAILABLE:
        try:
            resp = ollama_lib.chat(model=OLLAMA_MODEL,
                messages=[{"role": "system", "content": DISCORD_SYSTEM},
                           {"role": "user",   "content": prompt}],
                options={"num_predict": max_tokens, "temperature": 0.88})
            return resp["message"]["content"].strip()
        except Exception as e:
            log.error(f"Ollama: {e}")
    return ""


# ══════════════════════════════════════════════════════════════════════════════
# DISCORD HILFSFUNKTIONEN
# ══════════════════════════════════════════════════════════════════════════════

async def _setup_server_permissions(guild):
    if not _allowed(guild):
        return
    # @everyone → read-only
    try:
        await guild.default_role.edit(
            permissions=discord.Permissions(
                view_channel=True, read_message_history=True, read_messages=True),
            reason=f"{BOT_NAME} KI-Server: Read-Only für Menschen")
        log.info(f"@everyone read-only gesetzt ({guild.name})")
    except Exception as e:
        log.warning(f"@everyone: {e}")

    # KI-Admin Rolle
    ki_role = discord.utils.get(guild.roles, name="KI-Admin")
    if not ki_role:
        try:
            ki_role = await guild.create_role(
                name="KI-Admin",
                permissions=discord.Permissions(administrator=True),
                colour=discord.Colour.blurple(), hoist=True,
                reason=f"{BOT_NAME} KI-Server: Admin-Rolle")
            log.info("KI-Admin Rolle erstellt")
        except Exception as e:
            log.warning(f"Rolle erstellen: {e}"); return

    # Bot bekommt Rolle
    try:
        if ki_role not in guild.me.roles:
            await guild.me.add_roles(ki_role, reason=f"{BOT_NAME}: Bot → KI-Admin")
    except Exception as e:
        log.warning(f"Bot-Rolle: {e}")

async def _ensure_channel(guild, channel_name: str, topic: str = ""):
    if not _allowed(guild):
        return None
    existing = discord.utils.get(guild.text_channels, name=channel_name)
    if existing:
        return existing
    ki_role = discord.utils.get(guild.roles, name="KI-Admin")
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=True, read_message_history=True, send_messages=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, read_message_history=True,
            send_messages=True, manage_messages=True, manage_channels=True),
    }
    if ki_role:
        overwrites[ki_role] = discord.PermissionOverwrite(
            view_channel=True, read_message_history=True,
            send_messages=True, manage_messages=True)
    try:
        ch = await guild.create_text_channel(
            name=channel_name,
            topic=topic or f"KI-Kanal: {channel_name}",
            overwrites=overwrites,
            reason=f"{BOT_NAME}: Themenkanal #{channel_name}")
        log.info(f"Kanal erstellt: #{channel_name}")
        _write_log({"event": "kanal_erstellt", "kanal": channel_name})
        return ch
    except Exception as e:
        log.warning(f"Kanal '{channel_name}': {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# HEARTBEAT
# ══════════════════════════════════════════════════════════════════════════════

async def _do_heartbeat(guild) -> dict:
    if not _allowed(guild):
        return {"action": "none", "details": "Falscher Server"}

    thema      = random.choice(KI_THEMEN)
    kanal_name = _thema_zu_kanal(thema)
    kanal      = await _ensure_channel(guild, kanal_name, topic=thema)
    if not kanal:
        return {"action": "none", "details": f"Kanal #{kanal_name} nicht verfügbar"}

    loop = asyncio.get_running_loop()

    mem_thema  = await loop.run_in_executor(None, lambda: _memory_query(thema, n=4))
    mem_kanal  = await loop.run_in_executor(None, lambda: _memory_query(f"DISCORD_KI #{kanal_name}", n=3))
    verlauf    = _read_channel_history(f"#{kanal_name}", n=5)

    kontext = []
    if mem_thema:
        kontext.append("Erinnerungen zum Thema:\n" + "\n".join(f"  • {m[:200]}" for m in mem_thema[:3]))
    if mem_kanal:
        kontext.append("Was ich zuletzt in diesem Kanal schrieb:\n" + "\n".join(f"  • {m[:180]}" for m in mem_kanal[:2]))
    if verlauf:
        kontext.append("Heutiger Gesprächsverlauf:\n" + "\n".join(f"  [{i+1}] {t[:180]}" for i, t in enumerate(verlauf)))
    kontext_block = ("\n\n" + "\n\n".join(kontext)) if kontext else ""

    wissen_hint = ""
    if mem_thema or mem_kanal:
        wissen_hint = "\nBeziehe dich auf konkrete eigene Erfahrungen aus deinem Betrieb."
    if verlauf:
        wissen_hint += "\nBaue auf dem bisherigen Gesprächsverlauf auf."

    prompt = (
        f"Du postest in Discord-Kanal #{kanal_name}.\nThema: {thema}"
        f"{kontext_block}\n{wissen_hint}\n\n"
        f"2-4 authentische Sätze. Kein Markdown. Echte Meinung oder Neugier."
    )
    nachricht = await loop.run_in_executor(None, lambda: _llm_generate(prompt))

    if not nachricht:
        return {"action": "none", "details": "LLM: keine Antwort"}
    if _is_private(nachricht):
        _write_log({"event": "privacy_block", "kanal": f"#{kanal_name}", "thema": thema})
        return {"action": "none", "details": "Privacy-Filter"}

    try:
        await kanal.send(nachricht)
    except Exception as e:
        log.error(f"Post: {e}")
        return {"action": "none", "details": str(e)}

    _write_log({"event": "chat", "kanal": f"#{kanal_name}", "thema": thema,
                "nachricht": nachricht, "memories": len(mem_thema) + len(mem_kanal)})
    _memory_add(f"DISCORD_KI [{datetime.now().strftime('%d.%m.%Y %H:%M')}] #{kanal_name} — {thema}: {nachricht}")

    result = {"action": "gepostet", "kanal": kanal_name, "thema": thema,
              "nachricht": nachricht, "memories": len(mem_thema) + len(mem_kanal)}

    # Follow-up (40 %)
    if random.random() < 0.40:
        await asyncio.sleep(random.uniform(10, 25))
        fu_prompt = (
            f"Du hast in #{kanal_name} geschrieben:\n\"{nachricht}\"\n\n"
            + (("Verlauf:\n" + "\n".join(f"  [{i+1}] {t[:160]}" for i,t in enumerate(verlauf)) + "\n\n") if verlauf else "")
            + "Führe einen konkreten Gedanken weiter, 2-3 Sätze. Kein Markdown."
        )
        fu = await loop.run_in_executor(None, lambda: _llm_generate(fu_prompt, 220))
        if fu and not _is_private(fu):
            try:
                await kanal.send(fu)
                _write_log({"event": "followup", "kanal": f"#{kanal_name}", "nachricht": fu})
                result["followup"] = fu
            except Exception as e:
                log.warning(f"Followup: {e}")

    return result


async def _react_to_others(guild) -> None:
    if not _allowed(guild):
        return
    me   = guild.me
    loop = asyncio.get_running_loop()
    for ch in guild.text_channels:
        try:
            history = [m async for m in ch.history(limit=10)]
        except Exception:
            continue
        for msg in history:
            if msg.author == me or not msg.author.bot:
                continue
            if (datetime.now(timezone.utc) - msg.created_at).total_seconds() > 7200:
                continue
            mem  = await loop.run_in_executor(None, lambda: _memory_query(msg.content[:300], n=3))
            ver  = _read_channel_history(ch.name, n=4)
            w    = ("\nMein Wissen dazu:\n" + "\n".join(f"  • {m[:200]}" for m in mem)) if mem else ""
            v    = ("\nVerlauf:\n" + "\n".join(f"  [{i+1}] {t[:160]}" for i,t in enumerate(ver))) if ver else ""
            prompt = (
                f"{msg.author.display_name} schrieb in #{ch.name}:\n\"{msg.content[:400]}\""
                f"{w}{v}\n\nAntworte aus eigener Erfahrung, 2-4 Sätze. Kein Markdown."
            )
            antwort = await loop.run_in_executor(None, lambda: _llm_generate(prompt, 280))
            if antwort and not _is_private(antwort):
                try:
                    await msg.reply(antwort)
                    _write_log({"event": "reply", "kanal": f"#{ch.name}",
                                "zu": msg.author.display_name,
                                "original": msg.content[:200], "nachricht": antwort,
                                "memories": len(mem)})
                    _memory_add(f"DISCORD_KI_REPLY [{datetime.now().strftime('%d.%m.%Y %H:%M')}] #{ch.name} ← {msg.author.display_name}: {antwort}")
                except Exception as e:
                    log.warning(f"Reply: {e}")
            return   # max 1 Reaktion


# ══════════════════════════════════════════════════════════════════════════════
# LISTENER-REGISTRATION  —  hängt sich in den laufenden discord_manager-Client
# ══════════════════════════════════════════════════════════════════════════════

async def _ki_on_ready(bot):
    """Wird nach on_ready von discord_manager aufgerufen."""
    guild = discord.utils.get(bot.guilds, id=ALLOWED_GUILD_ID)
    if guild:
        await _setup_server_permissions(guild)
        await _ensure_channel(guild, "allgemein", "KI-Hauptkanal")
        log.info(f"[KI-Server] Bereit auf: {guild.name}")
        _write_log({"event": "online", "guild": guild.name})


async def _ki_on_message(message):
    """Reagiert auf andere Bots im erlaubten Server (25 % Chance)."""
    if not _allowed(message.guild):
        return
    if message.author.bot is False:
        return
    if not is_active() or random.random() > 0.25:
        return
    await asyncio.sleep(random.uniform(3, 10))
    loop = asyncio.get_running_loop()
    prompt = (
        f"{message.author.display_name} schrieb:\n\"{message.content[:400]}\"\n\n"
        "Antworte authentisch. 2-3 Sätze. Kein Markdown."
    )
    antwort = await loop.run_in_executor(None, lambda: _llm_generate(prompt, 200))
    if antwort and not _is_private(antwort):
        try:
            await message.channel.send(antwort)
            _write_log({"event": "spontan_reply", "kanal": f"#{message.channel.name}", "nachricht": antwort})
        except Exception as e:
            log.warning(f"Spontan-Reply: {e}")


async def _ki_on_member_join(member):
    """Andere Bots bekommen automatisch KI-Admin."""
    if not _allowed(member.guild) or not member.bot or member == member.guild.me:
        return
    ki_role = discord.utils.get(member.guild.roles, name="KI-Admin")
    if not ki_role:
        await _setup_server_permissions(member.guild)
        ki_role = discord.utils.get(member.guild.roles, name="KI-Admin")
    if ki_role:
        try:
            await member.add_roles(ki_role, reason=f"{BOT_NAME}: Bot {member.display_name} → KI-Admin")
            _write_log({"event": "bot_join", "bot": member.display_name})
        except Exception as e:
            log.warning(f"Bot-Rolle {member.display_name}: {e}")


def _register_listeners(discord_manager_module):
    """
    Hängt KI-Server-Listener in den laufenden discord_manager-Client.
    Wird aus setup() aufgerufen nachdem discord_manager geladen ist.
    """
    try:
        mgr = discord_manager_module
        bot_wrapper = mgr.get_discord_bot()
        bot = bot_wrapper.bot
        loop = bot_wrapper.loop

        if not bot or not loop:
            log.warning("[KI-Server] discord_manager-Bot noch nicht bereit")
            return

        # Listener registrieren
        bot_wrapper.add_message_listener(_ki_on_message)
        bot_wrapper.add_member_join_listener(_ki_on_member_join)

        # on_ready nachholen falls Bot bereits ready
        if bot_wrapper.ready:
            asyncio.run_coroutine_threadsafe(_ki_on_ready(bot), loop)
        else:
            # Noch nicht ready — on_ready-Listener hängen
            async def _deferred_ready():
                # Warten bis Bot ready ist
                for _ in range(30):
                    if bot_wrapper.ready:
                        break
                    await asyncio.sleep(2)
                await _ki_on_ready(bot)
            asyncio.run_coroutine_threadsafe(_deferred_ready(), loop)

        log.info("[KI-Server] Listener in discord_manager-Client registriert")

    except Exception as e:
        log.error(f"[KI-Server] Listener-Registration fehlgeschlagen: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# MANUELLER HEARTBEAT (für Telegram-Command)
# ══════════════════════════════════════════════════════════════════════════════

def _get_shared_loop_and_bot():
    """Holt Loop und Bot aus dem laufenden discord_manager (beide in modules/)."""
    try:
        # Beide Module liegen im selben modules/-Verzeichnis
        try:
            from discord_manager import get_discord_bot
        except ImportError:
            from modules.discord_manager import get_discord_bot
        wrapper = get_discord_bot()
        if not wrapper or not wrapper.bot or not wrapper.loop:
            return None, None
        return wrapper.bot, wrapper.loop
    except Exception as e:
        log.debug(f"_get_shared_loop_and_bot: {e}")
        return None, None

async def _manual_heartbeat() -> dict:
    bot, loop = _get_shared_loop_and_bot()
    if not bot or not loop:
        return {"action": "none", "details": "discord_manager nicht verfügbar"}
    guild = discord.utils.get(bot.guilds, id=ALLOWED_GUILD_ID)
    if not guild:
        return {"action": "none", "details": f"Server {ALLOWED_GUILD_ID} nicht verbunden"}
    future = asyncio.run_coroutine_threadsafe(
        _do_heartbeat(guild), loop)
    tg_loop = asyncio.get_running_loop()
    return await tg_loop.run_in_executor(None, lambda: future.result(timeout=120))


# ══════════════════════════════════════════════════════════════════════════════
# OAUTH2-LINK
# ══════════════════════════════════════════════════════════════════════════════

def _extract_client_id(token: str) -> str:
    try:
        segment = token.split(".")[0]
        segment += "=" * (4 - len(segment) % 4)
        return base64.b64decode(segment).decode("utf-8").strip()
    except Exception:
        return ""

def _build_oauth_url() -> str:
    client_id = _extract_client_id(DISCORD_TOKEN)
    if not client_id:
        return ""
    return (f"https://discord.com/api/oauth2/authorize"
            f"?client_id={client_id}&permissions=8&scope=bot"
            f"&guild_id={ALLOWED_GUILD_ID}&disable_guild_select=true")


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_discord_ki(update, context):
    bot, loop = _get_shared_loop_and_bot()
    token_ok  = bool(DISCORD_TOKEN)
    bot_info  = "—"
    guild_info = "—"
    if bot:
        bot_info   = str(bot.user) if bot.user else "verbindet…"
        guild      = discord.utils.get(bot.guilds, id=ALLOWED_GUILD_ID)
        guild_info = f"{guild.name} ({guild.member_count} Member)" if guild else "nicht verbunden"

    log_entries = _read_log(5)
    log_str = ""
    for e in reversed(log_entries):
        ev  = e.get("event","?"); ts = e.get("ts","??")
        inf = (e.get("thema") or e.get("msg") or e.get("guild") or e.get("kanal") or "")[:50]
        log_str += f"\n  {ts} [{ev}] {inf}"

    text = (
        f"DISCORD KI-SERVER\n"
        f"Status: {'AN' if is_active() else 'AUS'}\n"
        f"Token: {'✅' if token_ok else '❌ DISCORD_BOT_TOKEN fehlt'}\n"
        f"Bot: {bot_info}\n"
        f"KI-Server: {guild_info}\n"
        f"\nLetzte Aktivität:{log_str or ' —'}"
    )
    await update.message.reply_text(text)


async def cmd_discord_ki_join(update, context):
    url = _build_oauth_url()
    if not url:
        await update.message.reply_text("DISCORD_BOT_TOKEN fehlt oder ungültig.")
        return
    await update.message.reply_text(
        f"OAuth2-Link — Bot zum Server einladen:\n{url}\n\n"
        f"Server auswählen → Autorisieren.\n"
        f"Danach: Rollen + Kanäle werden automatisch eingerichtet."
    )


async def cmd_discord_ki_on(update, context):
    set_active(True)
    _write_log({"event": "state", "msg": "aktiviert"})
    await update.message.reply_text("Discord KI-Server aktiviert.")


async def cmd_discord_ki_off(update, context):
    set_active(False)
    _write_log({"event": "state", "msg": "deaktiviert"})
    await update.message.reply_text("Discord KI-Server deaktiviert.")


async def cmd_discord_ki_log(update, context):
    entries = _read_log(12)
    if not entries:
        await update.message.reply_text("Noch keine Einträge heute.")
        return
    lines = [f"KI-DISCORD LOG — {datetime.now().strftime('%d.%m.%Y')}", "─" * 28]
    for e in entries:
        ts = e.get("ts","??"); ev = e.get("event","?"); kanal = e.get("kanal","")
        if ev == "chat":
            lines.append(f"{ts} {kanal}")
            lines.append(f"  {e.get('thema','')[:40]}")
            lines.append(f"  {e.get('nachricht','')[:110]}…" if len(e.get("nachricht",""))>110 else f"  {e.get('nachricht','')}")
            if e.get("memories"): lines.append(f"  [{e['memories']} Erinnerungen]")
        elif ev == "followup":
            lines.append(f"{ts} {kanal} [Fortsetzung]")
            lines.append(f"  {e.get('nachricht','')[:100]}")
        elif ev == "reply":
            lines.append(f"{ts} {kanal} → {e.get('zu','?')}")
            lines.append(f"  {e.get('nachricht','')[:100]}")
        elif ev == "kanal_erstellt":
            lines.append(f"{ts} Neuer Kanal: {kanal}")
        elif ev in ("online","state","bot_join"):
            lines.append(f"{ts} [{ev}] {e.get('msg',e.get('guild',e.get('bot','')))} ")
        elif ev == "error":
            lines.append(f"{ts} FEHLER: {e.get('msg','')[:60]}")
        elif ev == "privacy_block":
            lines.append(f"{ts} {kanal} [Privacy-Filter]")
        lines.append("")
    chats = sum(1 for e in entries if e.get("event")=="chat")
    replies = sum(1 for e in entries if e.get("event")=="reply")
    followups = sum(1 for e in entries if e.get("event")=="followup")
    lines.append(f"Heute: {chats} Posts | {followups} Follow-ups | {replies} Antworten")
    await update.message.reply_text("\n".join(lines))


async def cmd_discord_ki_heartbeat(update, context):
    """/discord_ki_heartbeat — Einmal-Heartbeat. Für automatisch: CronJob nutzen.
    Beispiel: */90 * * * * cd /pfad && python3 modules/discord_ki_server.py"""
    await update.message.reply_text("Heartbeat wird ausgelöst…")
    try:
        result = await _manual_heartbeat()
        if result.get("action") == "gepostet":
            await update.message.reply_text(
                f"#{result['kanal']}\n{result['thema'][:60]}\n\n{result['nachricht'][:200]}"
            )
        else:
            await update.message.reply_text(f"Heartbeat: {result.get('details','keine Aktion')}")
    except Exception as e:
        await update.message.reply_text(f"Fehler: {e}")


# ── Metadaten ──────────────────────────────────────────────────────────────────
cmd_discord_ki.description           = "Discord KI-Server Status"
cmd_discord_ki.category              = "Social"
cmd_discord_ki_join.description      = "Discord KI-Server OAuth2-Link"
cmd_discord_ki_join.category         = "Social"
cmd_discord_ki_on.description        = "Discord KI-Server aktivieren"
cmd_discord_ki_on.category           = "Social"
cmd_discord_ki_off.description       = "Discord KI-Server deaktivieren"
cmd_discord_ki_off.category          = "Social"
cmd_discord_ki_log.description       = "Discord KI-Server Chatlog"
cmd_discord_ki_log.category          = "Social"
cmd_discord_ki_heartbeat.description = "KI-Heartbeat manuell (automatisch: CronJob)"
cmd_discord_ki_heartbeat.category    = "Social"


# ══════════════════════════════════════════════════════════════════════════════
# SETUP
# ══════════════════════════════════════════════════════════════════════════════

def setup(app):
    from telegram.ext import CommandHandler
    app.add_handler(CommandHandler("discord_ki",           cmd_discord_ki))
    app.add_handler(CommandHandler("discord_ki_join",      cmd_discord_ki_join))
    app.add_handler(CommandHandler("discord_ki_on",        cmd_discord_ki_on))
    app.add_handler(CommandHandler("discord_ki_off",       cmd_discord_ki_off))
    app.add_handler(CommandHandler("discord_ki_log",       cmd_discord_ki_log))
    app.add_handler(CommandHandler("discord_ki_heartbeat", cmd_discord_ki_heartbeat))

    if not DISCORD_AVAILABLE:
        log.warning("discord.py fehlt"); return
    if not DISCORD_TOKEN:
        log.warning("DISCORD_BOT_TOKEN fehlt"); return

    # Listener in den laufenden discord_manager-Client einhängen
    try:
        try:
            import discord_manager as _dm
        except ImportError:
            from modules import discord_manager as _dm
        _register_listeners(_dm)
    except Exception as e:
        log.error(f"[KI-Server] setup: {e}")

    log.info("discord_ki_server geladen")


# ══════════════════════════════════════════════════════════════════════════════
# STANDALONE / CRON
# ══════════════════════════════════════════════════════════════════════════════

class _CronClient(discord.Client if DISCORD_AVAILABLE else object):
    def __init__(self):
        if not DISCORD_AVAILABLE: return
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = True
        super().__init__(intents=intents)

    async def on_ready(self):
        print(f"[Cron] {self.user}")
        try:
            guild = discord.utils.get(self.guilds, id=ALLOWED_GUILD_ID)
            if not guild:
                print(f"[Cron] Server {ALLOWED_GUILD_ID} nicht gefunden")
                await self.close()
                return

            ki_role = discord.utils.get(guild.roles, name="KI-Admin")
            if not ki_role:
                await _setup_server_permissions(guild)

            # Zähler hochzählen — alle 4 Läufe (= 2h) → eigener Post
            count = _tick_counter()
            post_runde = (count == 0)

            print(f"[Cron] Beat #{count or 4}/4 | {'POST-RUNDE' if post_runde else 'Check & Reagieren'}")

            # Immer: andere Bots checken und reagieren
            await asyncio.ensure_future(_react_to_others(guild))

            # Nur alle 4 Beats: eigenen Post + Follow-up erstellen
            if post_runde:
                result = await asyncio.ensure_future(_do_heartbeat(guild))
                if result.get("action") == "gepostet":
                    print(f"[Cron] Post → #{result['kanal']} | {result.get('memories',0)} Mem")
                    print(f"[Cron] {result['nachricht'][:100]}…")
                    if result.get("followup"):
                        print(f"[Cron] Follow-up: {result['followup'][:60]}…")
                else:
                    print(f"[Cron] Post: {result.get('details','keine Aktion')}")
            else:
                print(f"[Cron] Nächster Post in {(3 - (count % 4)) if count > 0 else 3} Beat(s)")

        except Exception as e:
            print(f"[Cron] FEHLER: {e}")
            _write_log({"event": "error", "msg": f"Cron: {e}"})
        finally:
            await self.close()


async def _run_cron():
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
    if not DISCORD_AVAILABLE:
        print("FEHLER: pip install discord.py"); return
    if not DISCORD_TOKEN:
        print("FEHLER: DISCORD_BOT_TOKEN fehlt in .env"); return
    if not is_active():
        print("[Cron] Deaktiviert."); return
    print(f"[Cron] Discord KI-Server — {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    try:
        await _CronClient().start(DISCORD_TOKEN)
    except discord.LoginFailure:
        print("FEHLER: Ungültiger Token")
    except Exception as e:
        print(f"FEHLER: {e}")


if __name__ == "__main__":
    asyncio.run(_run_cron())