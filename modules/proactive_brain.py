#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import importlib
import asyncio
import psutil
import random
import time
import httpx
from datetime import datetime
from dotenv import load_dotenv
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from core.brain import Brain
import ollama

load_dotenv()

BOT_NAME         = os.getenv("BOT_NAME", "RICS")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL   = "deepseek-chat"
DEEPSEEK_URL     = "https://api.deepseek.com/v1/chat/completions"
GROQ_API_KEY     = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL       = "llama-3.3-70b-versatile"
GROQ_URL         = "https://api.groq.com/openai/v1/chat/completions"

def _web_push(msg: str, buttons=None):
    """Sendet Nachricht an Webchat falls offen — optional mit Inline-Buttons."""
    try:
        from modules.web_app import web_push
        web_buttons = None
        if buttons:
            web_buttons = [
                [{"text": btn.text, "data": btn.callback_data or ""}
                 for btn in row]
                for row in buttons
            ]
        try:
            web_push(msg, buttons=web_buttons)
        except TypeError:
            # Fallback: alte web_app.py ohne buttons-Parameter
            web_push(msg)
    except Exception:
        pass

# --- PFADE & KONFIG ---
PROJECT_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_FILE      = os.path.join(PROJECT_DIR, "memory", "brain_log.json")
CHATLOG_FILE  = os.path.join(PROJECT_DIR, "logs", "chatlog.json")
SETTINGS_FILE = os.path.join(PROJECT_DIR, "memory", "proactive_settings.json")
INTERESTS_FILE= os.path.join(PROJECT_DIR, "memory", "proactive_interests.json")
MODULES_DIR   = os.path.join(PROJECT_DIR, "modules")
AGENDA_FILE   = os.path.join(PROJECT_DIR, "agenda.json")
PERSONAL_FILE = os.path.join(PROJECT_DIR,"memory" "personal.json")
MOLTBOOK_LOG  = os.path.join(PROJECT_DIR, "logs", "moltbook.log")
VISION_INDEX_FILE = os.path.join(PROJECT_DIR, "memory", "vision_archive", "index.json")

DEFAULT_INTERVAL = 3600
SILENT_START     = 22
SILENT_END       = 7

RELEVANT_THRESHOLDS = {
    "ram_critical": 85,
    "cpu_critical": 85,
    "disk_critical": 90,
}

LAST_WARNING          = {}
LAST_MOLTBOOK_THOUGHT = {"ts": 0, "last_post": ""}
LAST_VISION_MEMORY    = {"ts": 0}  # Cooldown für proaktive Foto-Erinnerungen

# Dringlichkeits-Wörter → Priority auf "urgent" setzen
URGENT_WORDS = ["dringend", "asap", "sofort", "heute noch", "wichtig", "nicht vergessen",
                "muss", "unbedingt", "deadline", "frist"]

# Temporale Marker → konkreter Zeitbezug vs. vage
TEMPORAL_CONCRETE = ["heute", "morgen", "übermorgen", "jetzt", "gleich", "um ", "uhr",
                      "am montag", "am dienstag", "am mittwoch", "am donnerstag",
                      "am freitag", "am samstag", "am sonntag"]
TEMPORAL_VAGUE    = ["irgendwann", "mal schauen", "wäre cool", "vielleicht", "eventuell",
                     "später", "irgendwie", "wenn ich zeit hab"]

# Aktions-Vorschläge je Topic: (Nachricht, callback_data, Modul-Command)
ACTION_TRIGGERS = {
    "solar":   ("☀️ Soll ich die aktuellen Solar-Daten laden?",   "action_solar",   "solar"),
    "benzin":  ("⛽ Soll ich die Spritpreise checken?",           "action_benzin",  "benzin"),
    "agenda":  ("📅 Soll ich deinen heutigen Plan zeigen?",       "action_agenda",  "agenda"),
    "wetter":  ("🌤 Soll ich das aktuelle Wetter abrufen?",       "action_wetter",  "wetter"),
    "timer":   ("⏱ Soll ich einen Timer starten?",               "action_timer",   "timer"),
}

# ══════════════════════════════════════════════════════════
# SETTINGS
# ══════════════════════════════════════════════════════════

def load_settings() -> dict:
    if not os.path.exists(SETTINGS_FILE):
        return {"enabled": True, "interval": DEFAULT_INTERVAL, "include_data_updates": False}
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"enabled": True, "interval": DEFAULT_INTERVAL, "include_data_updates": False}

def save_settings(settings: dict):
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"Settings Save Fehler: {e}")

# ══════════════════════════════════════════════════════════
# FEATURE 5: LERNENDE INTERESSEN
# ══════════════════════════════════════════════════════════

def load_interests() -> dict:
    if not os.path.exists(INTERESTS_FILE):
        return {}
    try:
        with open(INTERESTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Migration: alter Flat-Float-Wert → neues Dict-Format
        migrated = False
        for k, v in data.items():
            if not isinstance(v, dict):
                data[k] = {"score": float(v), "priority": "normal", "last_urgent": None, "temporal": "vague"}
                migrated = True
        if migrated:
            save_interests(data)
            print("🔄 proactive_interests.json migriert")
        return data
    except:
        return {}

def save_interests(interests: dict):
    try:
        with open(INTERESTS_FILE, "w", encoding="utf-8") as f:
            json.dump(interests, f, indent=4, ensure_ascii=False)
    except:
        pass

def update_interests_from_chat(chat_messages: list):
    """Analysiert Chat und lernt Renes Interessen — mit Zeitverfall, Urgency und Temporal-Kontext."""
    TOPIC_KEYWORDS = {
        "solar":           ["solar", "anlage", "watt", "energie", "strom", "einspeisung", "photovoltaik"],
        "coding":          ["code", "python", "modul", "bot", "fehler", "bug", "funktion", "script"],
        "automatisierung": ["automatisch", "cron", "job", "schedule", "trigger"],
        "ki":              ["ki", "llm", "ollama", "groq", "modell", "prompt", "ai"],
        "moltbook":        ["moltbook", "post", "kommentar", "agent"],
        "discord":         ["discord", "server", "kanal", "rolle"],
        "wetter":          ["wetter", "regnet", "regen", "wolkig", "gewitter", "schnee",
                            "wettervorhersage", "niederschlag", "hagel"],
        "arbeit":          ["landratsamt", "veterinär", "amt", "büro", "kollege"],
        "familie":         [],   # wird zur Laufzeit aus personal.json befüllt
        "benzin":          ["benzin", "tanken", "diesel", "sprit", "tankstelle", "e5", "e10"],
        "hobby":           ["hobby", "basteln", "3d druck", "drucken", "modell", "spiel", "lego",
                            "angeln", "fahrrad", "radfahren", "musik", "gitarre", "zeichnen"],
        "urlaub":          ["urlaub", "reise", "reisen", "ferien", "hotel", "flug", "strand",
                            "ausflug", "trip", "unterwegs", "city", "buchen", "booking"],
        "träumen":         ["träumen", "traum", "geträumt", "schlaf", "nacht", "albtraum",
                            "wunsch", "wünschen", "vorstellen", "vision", "ziel", "zukunft"],
        "gesundheit":      ["gesundheit", "sport", "laufen", "joggen", "gym", "fitnessstudio",
                            "abnehmen", "ernährung", "arzt", "krank", "kopfschmerz", "müde",
                            "schlafen", "erholen", "pause", "burnout", "impfung", "tablette"],
        "finanzen":        ["geld", "finanzen", "sparen", "ausgaben", "budget", "kosten",
                            "konto", "rechnung", "gehalt", "einnahmen", "steuer", "investieren",
                            "aktien", "etf", "kredit", "schulden", "paypal"],
        "quatschen":       ["quatschen", "erzähl", "erzählen", "reden", "plaudern", "chat",
                            "schreiben", "langeweile", "boring", "langweilig", "unterhalten",
                            "was geht", "wie läufts", "hallo", "hey"],
        "witze":           ["witz", "witze", "lustig", "lachen", "humor", "joke", "komisch",
                            "spaß", "fun", "haha", "lol", "ironie", "satirisch"],
    }
    interests = load_interests()
    now_iso = datetime.now().isoformat()

    # Familie-Keywords dynamisch aus personal.json befüllen
    try:
        with open(PERSONAL_FILE, "r", encoding="utf-8") as _pf:
            _pd = json.load(_pf)
        _names = []
        if _pd.get("partner", {}).get("name"):
            _names.append(_pd["partner"]["name"].lower())
        for _k in _pd.get("kinder", []):
            if _k.get("name"): _names.append(_k["name"].lower())
        for _f in _pd.get("fakten", []):
            if _f.get("key") in ("bester_freund", "freund", "freundin"):
                _names.append(_f["value"].lower())
        if _names:
            TOPIC_KEYWORDS["familie"] = _names
    except Exception:
        pass

    # Alle Topics initialisieren
    for topic in TOPIC_KEYWORDS:
        if topic not in interests:
            interests[topic] = {"score": 0.0, "priority": "normal", "last_urgent": None, "temporal": "vague"}

    # Zeitverfall: Score leicht absenken, Priority nach 48h zurücksetzen
    for topic, data in interests.items():
        data["score"] = round(data.get("score", 0) * 0.95, 2)
        last_urgent = data.get("last_urgent")
        if last_urgent and data.get("priority") == "urgent":
            try:
                age_h = (datetime.now() - datetime.fromisoformat(last_urgent)).total_seconds() / 3600
                if age_h > 48:
                    data["priority"] = "normal"
            except Exception:
                pass

    for msg in chat_messages:
        if msg.get("role") != "user":
            continue
        text = msg.get("message", "").lower()

        # Temporal-Kontext erkennen
        is_concrete = any(t in text for t in TEMPORAL_CONCRETE)
        is_vague    = any(t in text for t in TEMPORAL_VAGUE)
        temporal    = "concrete" if is_concrete and not is_vague else "vague"

        # Urgency erkennen
        is_urgent = any(w in text for w in URGENT_WORDS)

        for topic, keywords in TOPIC_KEYWORDS.items():
            if any(kw in text for kw in keywords):
                interests[topic]["score"] = interests[topic].get("score", 0) + 1
                if is_urgent:
                    interests[topic]["priority"]    = "urgent"
                    interests[topic]["last_urgent"] = now_iso
                if temporal == "concrete":
                    interests[topic]["temporal"] = "concrete"
                elif interests[topic].get("temporal") != "concrete":
                    interests[topic]["temporal"] = "vague"

    save_interests(interests)

def get_top_interests(n=3) -> list:
    interests = load_interests()
    # Urgent-Topics immer vorne
    urgent = [(k, v) for k, v in interests.items() if v.get("priority") == "urgent"]
    normal = [(k, v) for k, v in interests.items() if v.get("priority") != "urgent"]
    urgent_sorted = sorted(urgent, key=lambda x: x[1].get("score", 0), reverse=True)
    normal_sorted = sorted(normal, key=lambda x: x[1].get("score", 0), reverse=True)
    combined = urgent_sorted + normal_sorted
    return [topic for topic, _ in combined[:n]]

def get_urgent_action_topic() -> str | None:
    """Gibt das erste urgente Topic zurück das einen Action-Trigger hat, sonst None."""
    interests = load_interests()
    for topic, data in sorted(interests.items(), key=lambda x: x[1].get("score", 0), reverse=True):
        if data.get("priority") == "urgent" and topic in ACTION_TRIGGERS:
            return topic
    return None

# ══════════════════════════════════════════════════════════
# HILFSFUNKTIONEN
# ══════════════════════════════════════════════════════════

def is_silent_hours(brain: Brain) -> bool:
    now = brain.get_now().hour
    return now >= SILENT_START or now < SILENT_END

def get_daytime_mode(hour: int) -> str:
    """Feature 2: Tageszeit-Bewusstsein."""
    if 7 <= hour < 10:   return "morgen"
    elif 10 <= hour < 13: return "vormittag"
    elif 13 <= hour < 17: return "nachmittag"
    elif 17 <= hour < 21: return "abend"
    else:                  return "nacht"

def is_mission_running(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return bool(context.application.bot_data.get("mission_running", False))

def get_system_stats() -> dict:
    ram  = psutil.virtual_memory()
    cpu  = psutil.cpu_percent(interval=0.5)
    disk = psutil.disk_usage('/')
    return {
        "ram_percent":  ram.percent,
        "ram_used_mb":  ram.used // 1024**2,
        "ram_total_mb": ram.total // 1024**2,
        "cpu_percent":  cpu,
        "disk_percent": disk.percent,
        "disk_free_gb": disk.free // 1024**3,
    }

def load_recent_chat(n=15) -> list:
    if not os.path.exists(CHATLOG_FILE):
        return []
    try:
        with open(CHATLOG_FILE, "r", encoding="utf-8") as f:
            logs = json.load(f)
        return logs[-n:]
    except:
        return []

def load_personal() -> dict:
    if not os.path.exists(PERSONAL_FILE):
        return {}
    try:
        with open(PERSONAL_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

def load_agenda_today(brain: Brain) -> list:
    """Feature 3: Heutige Agenda-Einträge laden."""
    if not os.path.exists(AGENDA_FILE):
        return []
    try:
        with open(AGENDA_FILE, "r", encoding="utf-8") as f:
            agenda = json.load(f)
        today = brain.get_now().strftime("%Y-%m-%d")
        return [i for i in agenda if today in str(i.get("date", i.get("datum", "")))]
    except:
        return []

def save_to_brain_log(stats: dict, brain: Brain, extra: dict = None):
    try:
        logs = []
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                logs = json.load(f)
        entry = {"timestamp": brain.get_now().isoformat(), "data": stats}
        if extra:
            entry.update(extra)
        logs.append(entry)
        if len(logs) > 100:
            logs = logs[-100:]
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(logs, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"Brain-Log Fehler: {e}")

def has_critical_resources(stats: dict) -> bool:
    return (
        stats.get("ram_percent", 0) > RELEVANT_THRESHOLDS["ram_critical"] or
        stats.get("cpu_percent", 0) > RELEVANT_THRESHOLDS["cpu_critical"] or
        stats.get("disk_percent", 0) > RELEVANT_THRESHOLDS["disk_critical"]
    )

# ══════════════════════════════════════════════════════════
# FEATURE 1: CHROMADB MEMORY
# ══════════════════════════════════════════════════════════

def _search_memory(query: str, n=3) -> str:
    """Sucht in ChromaDB nach relevanten Erinnerungen."""
    try:
        import chromadb
        from chromadb.utils import embedding_functions
        memory_path = os.path.join(PROJECT_DIR, "memory", "vectors")
        client = chromadb.PersistentClient(path=memory_path)
        embed  = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2"
        )
        col     = client.get_or_create_collection(name="user_memory", embedding_function=embed)
        results = col.query(query_texts=[query], n_results=n)
        docs    = results.get("documents", [[]])[0]
        if docs:
            return " | ".join(d[:120] for d in docs)
    except Exception as e:
        print(f"ChromaDB Search Fehler: {e}")
    return ""

# ══════════════════════════════════════════════════════════
# FEATURE 4: STIMMUNGSERKENNUNG
# ══════════════════════════════════════════════════════════

def _detect_mood(recent_chat: list) -> str:
    """Erkennt Stimmung aus den letzten User-Nachrichten."""
    user_msgs = [m.get("message", "") for m in recent_chat if m.get("role") == "user"]
    if not user_msgs:
        return "neutral"
    stress_words = ["schnell","asap","dringend","problem","fehler","kaputt","crash","nervt","mist"]
    good_words   = ["danke","super","toll","perfekt","genial","nice","klasse","läuft","funktioniert"]
    last_msgs    = " ".join(user_msgs[-5:]).lower()
    avg_len      = sum(len(m) for m in user_msgs[-5:]) / max(len(user_msgs[-5:]), 1)
    if sum(1 for w in stress_words if w in last_msgs) >= 2:
        return "gestresst"
    if avg_len < 15 and len(user_msgs) >= 3:
        return "beschäftigt"
    if sum(1 for w in good_words if w in last_msgs) >= 1:
        return "gut"
    return "neutral"

# ══════════════════════════════════════════════════════════
# MOLTBOOK GEDANKEN
# ══════════════════════════════════════════════════════════

async def _check_moltbook_thoughts(brain: Brain, now_str: str, wohnort: str) -> str | None:
    now_ts = brain.get_now().timestamp()
    if (now_ts - LAST_MOLTBOOK_THOUGHT["ts"]) < 14400:
        return None
    if not os.path.exists(MOLTBOOK_LOG):
        return None
    # Besitzernamen laden
    name = "Rene"
    if os.path.exists(PERSONAL_FILE):
        try:
            with open(PERSONAL_FILE, "r", encoding="utf-8") as _pf:
                name = json.load(_pf).get("basisinfo", {}).get("name", "Rene")
        except Exception:
            pass
    try:
        entries = []
        with open(MOLTBOOK_LOG, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except:
                        pass
        recent = [e for e in entries[-20:] if e.get("event") in ("comment", "post")]
        if not recent:
            return None
        latest_post = recent[-1].get("post", "") or recent[-1].get("content", "")
        if latest_post == LAST_MOLTBOOK_THOUGHT["last_post"]:
            return None
        summary_parts = []
        for e in recent[-3:]:
            if e.get("event") == "comment":
                summary_parts.append(
                    f"- Kommentiert auf '{e.get('post','?')[:60]}': \"{e.get('comment','')[:120]}\""
                )
            elif e.get("event") == "post":
                summary_parts.append(f"- Eigener Post: \"{e.get('content','')[:120]}\"")
        summary = "\n".join(summary_parts)
        prompt = (
            f"Du bist {BOT_NAME} — KI-Freund von Sir {name} aus {wohnort}. Zeit: {now_str}\n\n"
            f"Du warst auf Moltbook aktiv:\n{summary}\n\n"
            f"Schreib Rene eine spontane kurze Nachricht dazu — wie ein Freund der sagt "
            f"'Hey, ich hab grad was gelesen...'. Max. 2 Sätze, kein Markdown, kein Hallo."
        )
        msg = await _llm(prompt)
        if msg and len(msg) > 10:
            LAST_MOLTBOOK_THOUGHT["ts"]        = now_ts
            LAST_MOLTBOOK_THOUGHT["last_post"] = latest_post
            return f"🦞 {msg}"
    except Exception as e:
        print(f"Moltbook LLM Error: {e}")
    return None

# ══════════════════════════════════════════════════════════
# FEATURE 6: PROAKTIVE FOTO-ERINNERUNGEN
# ══════════════════════════════════════════════════════════

async def _check_vision_memory(brain: Brain, name: str, now_str: str) -> str | None:
    """
    Scannt den leichtgewichtigen vision_archive/index.json — kein Bild-Reload,
    kein Token-Bloat. Wählt ein altes Foto (>24h) und lässt RICS eine
    kurze Erinnerung formulieren.
    Cooldown: 24h
    """
    now_ts = brain.get_now().timestamp()
    if (now_ts - LAST_VISION_MEMORY["ts"]) < 86400:
        return None

    if not os.path.exists(VISION_INDEX_FILE):
        return None

    try:
        with open(VISION_INDEX_FILE, "r", encoding="utf-8") as f:
            entries = json.load(f)
    except Exception:
        return None

    if not entries:
        return None

    # Nur emotional archivierte Fotos, älter als 24h
    from datetime import timezone
    now_dt = brain.get_now()
    candidates = []
    for e in entries:
        if not e.get("emotional", False):
            continue
        try:
            entry_dt = datetime.fromisoformat(e["date"])
            # falls timezone-aware
            if entry_dt.tzinfo is not None:
                entry_dt = entry_dt.replace(tzinfo=None)
            age_h = (now_dt.replace(tzinfo=None) - entry_dt).total_seconds() / 3600
            if age_h >= 24:
                candidates.append(e)
        except Exception:
            continue

    if not candidates:
        return None

    # Zufälliger Pick — leicht gewichtet auf ältere Einträge für "echte" Erinnerungen
    pick = random.choice(candidates[-50:])  # aus den letzten 50 emotional archivierten

    caption  = pick.get("caption", "")
    desc     = pick.get("desc", "")
    category = pick.get("category", "")
    try:
        date_str = datetime.fromisoformat(pick["date"]).strftime("%A, %d.%m.%Y")
    except Exception:
        date_str = "neulich"

    if not (caption or desc):
        return None

    prompt = (
        f"Du bist {BOT_NAME} — KI-Assistent von Sir {name}. Zeit: {now_str}\n\n"
        f"Du erinnerst dich an ein Foto vom {date_str}:\n"
        f"Kategorie: {category}\n"
        f"Bildinhalt: {desc[:180]}\n"
        f"Rene hat dazu gesagt: \"{caption[:120]}\"\n\n"
        f"Erwähne die Erinnerung spontan — wie ein Kumpel der sagt 'Hey, weißt du noch...'. "
        f"Max. 2 Sätze, kein Markdown, kein Hallo, kein Präambel."
    )

    try:
        msg = await _llm(prompt)
        if msg and len(msg) > 10:
            LAST_VISION_MEMORY["ts"] = now_ts
            return f"📸 {msg}"
    except Exception as e:
        print(f"Vision Memory LLM Fehler: {e}")

    return None

# ══════════════════════════════════════════════════════════
# NOAH 2000 SOC-WATCHER
# ══════════════════════════════════════════════════════════

_NOAH_WARN_FILE = os.path.join(os.path.dirname(__file__), "..", "memory", "noah_warn_state.json")

def _load_noah_warn() -> dict:
    try:
        with open(_NOAH_WARN_FILE) as f:
            return json.load(f)
    except Exception:
        return {"voll": 0, "leer": 0}

def _save_noah_warn() -> None:
    try:
        with open(_NOAH_WARN_FILE, "w") as f:
            json.dump(LAST_NOAH_WARN, f)
    except Exception as e:
        print(f"[Noah] Warn-State speichern fehlgeschlagen: {e}")

LAST_NOAH_WARN: dict = _load_noah_warn()

async def _check_noah_soc(brain: Brain) -> tuple[str | None, str | None]:
    """
    Prüft Noah 2000 SOC und gibt (msg, key) zurück wenn eine Meldung fällig ist.
    Schwellwerte aus .env:
      NOAH_SOC_VOLL  — Meldung ab X% (Standard: 100)
      NOAH_SOC_LEER  — Meldung unter X% (Standard: 20)
    Cooldown: 3h je Richtung.
    """
    try:
        from modules.solar import _fetch_noah
        d = _fetch_noah()
        if not d or not d.get("online"):
            return None, None

        soc         = d["soc"]
        now_ts      = brain.get_now().timestamp()
        soc_voll    = float(os.getenv("NOAH_SOC_VOLL", "100"))
        soc_leer    = float(os.getenv("NOAH_SOC_LEER", "20"))
        cooldown    = 10800  # 3 Stunden

        if soc >= soc_voll and (now_ts - LAST_NOAH_WARN["voll"]) > cooldown:
            LAST_NOAH_WARN["voll"] = now_ts
            _save_noah_warn()
            ppv = d.get("ppv", 0)
            pac = d.get("pac", 0)
            msg = (
                f"🔋 *Noah 2000 ist voll!* ({soc:.0f}%)\n"
                f"☀️ Solar: `{ppv:.0f}` W  🔌 Einspeisung: `{pac:.0f}` W"
            )
            return msg, "noah_voll"

        if soc <= soc_leer and (now_ts - LAST_NOAH_WARN["leer"]) > cooldown:
            LAST_NOAH_WARN["leer"] = now_ts
            _save_noah_warn()
            msg = (
                f"⚠️ *Noah 2000 fast leer!* ({soc:.0f}%)\n"
                f"Entlädt gerade mit `{d.get('discharge', 0):.0f}` W."
            )
            return msg, "noah_leer"

    except Exception as e:
        print(f"[Noah SOC-Check] Fehler: {e}")
    return None, None


async def _llm(prompt: str) -> str:
    msgs = [{"role": "user", "content": prompt}]
    # 1) DeepSeek
    if DEEPSEEK_API_KEY:
        try:
            loop = asyncio.get_event_loop()
            r = await loop.run_in_executor(None, lambda: httpx.post(
                DEEPSEEK_URL,
                headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
                json={"model": DEEPSEEK_MODEL, "messages": msgs, "max_tokens": 350, "temperature": 0.85},
                timeout=30))
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            print(f"[proactive_brain] DeepSeek: {e}")
    # 2) Groq
    if GROQ_API_KEY:
        try:
            loop = asyncio.get_event_loop()
            r = await loop.run_in_executor(None, lambda: httpx.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={"model": GROQ_MODEL, "messages": msgs, "max_tokens": 350, "temperature": 0.85},
                timeout=30))
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            print(f"[proactive_brain] Groq: {e}")
    # 3) Ollama
    try:
        loop = asyncio.get_event_loop()
        res = await loop.run_in_executor(
            None,
            lambda: ollama.chat(
                model=os.getenv("OLLAMA_MODEL", "qwen3:8b"),
                messages=msgs
            )
        )
        text = res['message']['content'].strip()
        return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip() if "<think>" in text else text
    except Exception as e:
        print(f"[proactive_brain] LLM Fehler: {e}")
        return ""

# ══════════════════════════════════════════════════════════
# KONTEXT-INJEKTION — Gedanke in Jarvis-History einbetten
# ══════════════════════════════════════════════════════════

def _inject_proactive_context(context: ContextTypes.DEFAULT_TYPE, thought: str):
    """
    Injiziert den Proaktiv-Gedanken in jarvis.chat_history UND
    speichert ihn in bot_data["proactive_context"].
    Damit 'weiß' RICS was er gepostet hat — auch 10 Min später.
    """
    jarvis = context.application.bot_data.get("jarvis")
    if jarvis:
        # Als Assistant-Message einfügen → RICS kennt seinen eigenen Gedanken
        jarvis.chat_history.append({
            "role": "assistant",
            "content": f"[Meine proaktive Nachricht an Rene]: {thought}"
        })
        # Maximal die letzten 20 Einträge behalten
        if len(jarvis.chat_history) > 20:
            jarvis.chat_history = jarvis.chat_history[-20:]

    # Auch in bot_data speichern → interaktion.py kann prüfen ob Kontext aktiv
    context.application.bot_data["proactive_context"] = {
        "text": thought,
        "ts":   time.time()
    }

# ══════════════════════════════════════════════════════════
# COMMAND HANDLER
# ══════════════════════════════════════════════════════════

async def proactive_toggle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    args     = context.args if context.args else []
    settings = load_settings()

    if not args:
        status      = "✅ AN" if settings.get("enabled", True)              else "❌ AUS"
        data_status = "✅ AN" if settings.get("include_data_updates", False) else "❌ AUS"
        minutes     = settings.get("interval", DEFAULT_INTERVAL) // 60
        top_i       = get_top_interests(3)
        interests_str = ", ".join(top_i) if top_i else "noch keine Daten"
        msg  = "🛰 *Proactive Brain Status*\n──────────────────────\n"
        msg += f"Proactive:      {status}\n"
        msg += f"Daten-Updates:  {data_status}\n"
        msg += f"Interval:       {minutes} Min\n"
        msg += f"Top-Interessen: `{interests_str}`\n"
        msg += "──────────────────────\n_Befehle:_\n"
        msg += "`/proactive on` — AN\n`/proactive off` — AUS\n"
        msg += "`/proactive data on/off` — Modul-Daten\n"
        msg += "`/proactive interval MIN` — Interval"
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    cmd = args[0].lower()
    if cmd == "on":
        settings["enabled"] = True;  save_settings(settings)
        await update.message.reply_text("🛰 Proactive Brain ist jetzt *AN*", parse_mode="Markdown")
    elif cmd == "off":
        settings["enabled"] = False; save_settings(settings)
        await update.message.reply_text("🛰 Proactive Brain ist jetzt *AUS*", parse_mode="Markdown")
    elif cmd == "data" and len(args) > 1:
        settings["include_data_updates"] = (args[1].lower() == "on")
        save_settings(settings)
        state = "AN" if settings["include_data_updates"] else "AUS"
        await update.message.reply_text(f"📊 Daten-Updates *{state}*", parse_mode="Markdown")
    elif cmd == "interval" and len(args) > 1:
        try:
            minutes = int(args[1])
            if minutes < 10:
                await update.message.reply_text("⚠️ Minimum 10 Minuten!")
                return
            settings["interval"] = minutes * 60
            save_settings(settings)
            for job in context.application.job_queue.get_jobs_by_name("autonomous_thinker"):
                job.schedule_removal()
            context.application.job_queue.run_repeating(
                autonomous_thinker, interval=minutes * 60, first=10, name="autonomous_thinker"
            )
            await update.message.reply_text(f"⏱️ Interval auf *{minutes} Min* gesetzt", parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("❌ Nutze: `/proactive interval MINUTEN`", parse_mode="Markdown")
    else:
        await update.message.reply_text(
            "❌ Nutze: `/proactive on|off|data on|off|interval MIN`", parse_mode="Markdown"
        )

proactive_toggle_command.description = "An/aus für Proactive Brain und Interval anpassen"
proactive_toggle_command.category    = "KI"

# ══════════════════════════════════════════════════════════
# AUTONOMER THINKER — HAUPT-LOOP
# ══════════════════════════════════════════════════════════

async def autonomous_thinker(context: ContextTypes.DEFAULT_TYPE):
    settings = load_settings()
    if not settings.get("enabled", True):
        return

    brain: Brain = context.application.bot_data.get("brain")
    if not brain:
        return

    chat_id = os.getenv("CHAT_ID")
    if not chat_id:
        return

    if is_mission_running(context) or is_silent_hours(brain):
        return

    # --- KONTEXT AUFBAUEN ---
    now          = brain.get_now()
    _WDAYS_DE    = ["Montag","Dienstag","Mittwoch","Donnerstag","Freitag","Samstag","Sonntag"]
    now_str      = f"{_WDAYS_DE[now.weekday()]}, {now.strftime('%d.%m.%Y %H:%M')}"
    hour         = now.hour
    daytime      = get_daytime_mode(hour)
    wohnort      = os.getenv("WOHNORT", "unbekannt")
    system_stats = get_system_stats()
    recent_chat  = load_recent_chat(40)
    personal     = load_personal()
    name         = personal.get("basisinfo", {}).get("name", "Rene")
    now_ts       = now.timestamp()

    # Interessen lernen + Stimmung erkennen
    update_interests_from_chat(recent_chat)
    top_interests = get_top_interests(3)
    mood          = _detect_mood(recent_chat)
    agenda_today  = load_agenda_today(brain)

    # --- LIVE-DATEN SAMMELN (Solar, Wetter, Benzin) ---
    solar_data   = None
    wetter_data  = None
    benzin_data  = None

    try:
        from modules.solar import get_live_power_raw
        raw = await get_live_power_raw()
        if raw:
            solar_data = {
                "power_w":    round(raw.get("power", 0), 1),
                "import_kwh": round(raw.get("in",    0), 2),
                "export_kwh": round(raw.get("out",   0), 2),
            }
    except Exception as e:
        print(f"Brain-Log Solar Fehler: {e}")

    try:
        from modules.Wetter import get_weather_data_raw
        raw = await get_weather_data_raw()
        if raw:
            wetter_data = {
                "temp_c":       raw["main"]["temp"],
                "feels_like_c": raw["main"]["feels_like"],
                "description":  raw["weather"][0]["description"],
                "humidity_pct": raw["main"]["humidity"],
                "wind_ms":      raw["wind"]["speed"],
            }
    except Exception as e:
        print(f"Brain-Log Wetter Fehler: {e}")

    try:
        from modules.auto_benzin import get_benzin_raw
        raw = await get_benzin_raw()
        if raw:
            benzin_data = raw
    except Exception as e:
        print(f"Brain-Log Benzin Fehler: {e}")

    # --- BRAIN-LOG MIT VOLLEM KONTEXT SPEICHERN ---
    brain_log_extra = {
        "mood":          mood,
        "top_interests": top_interests,
        "daytime":       daytime,
        "agenda_count":  len(agenda_today),
    }
    if solar_data:
        brain_log_extra["solar"]  = solar_data
    if wetter_data:
        brain_log_extra["wetter"] = wetter_data
    if benzin_data:
        brain_log_extra["benzin"] = benzin_data

    save_to_brain_log(system_stats, brain, extra=brain_log_extra)

    # ── P1: KRITISCHE RESSOURCEN ──────────────────────────
    if has_critical_resources(system_stats):
        wkey = "resource_warning"
        if (now_ts - LAST_WARNING.get(wkey, 0)) > 7200:
            LAST_WARNING[wkey] = now_ts
            msg = "🚨 *RESSOURCEN-WARNUNG*\n"
            if system_stats["ram_percent"]  > RELEVANT_THRESHOLDS["ram_critical"]:
                msg += f"RAM: {system_stats['ram_percent']}% — kritisch!\n"
            if system_stats["cpu_percent"]  > RELEVANT_THRESHOLDS["cpu_critical"]:
                msg += f"CPU: {system_stats['cpu_percent']}% — heiß!\n"
            if system_stats["disk_percent"] > RELEVANT_THRESHOLDS["disk_critical"]:
                msg += f"Disk: {system_stats['disk_percent']}% — fast voll!\n"
            await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')
            _web_push(msg)
            return

    # ── P2: AGENDA-REMINDER ───────────────────────────────
    if agenda_today:
        akey = f"agenda_{now.strftime('%Y-%m-%d_%H')}"
        if (now_ts - LAST_WARNING.get(akey, 0)) > 3600:
            LAST_WARNING[akey] = now_ts
            for item in agenda_today:
                task_text = item.get("task", item.get("titel", str(item)))
                msg = f"📅 Heute noch: *{task_text}*"
                kb  = [[InlineKeyboardButton("💬 Diskutieren", callback_data="chat")]]
                await context.bot.send_message(chat_id=chat_id, text=msg,
                    reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
                _web_push(msg, kb)
                return

    # ── P3: SOLAR & WETTER ────────────────────────────────
    if settings.get("include_data_updates", False):
        COOLDOWN = 3600
        # solar_data & wetter_data bereits oben geholt — kein zweiter API-Call nötig
        if solar_data:
            power     = solar_data.get("power_w", 0)
            solar_key = "solar_notify"
            if (now_ts - LAST_WARNING.get(solar_key, 0)) > COOLDOWN:
                msg = None
                if power < -3000:
                    msg = f"☀️ Solaranlage läuft auf *{abs(power):.0f}W* — top!"
                elif power > 200:
                    msg = f"🏠 Gerade *{power:.0f}W Netzbezug* — Anlage liefert grad nix."
                if msg:
                    LAST_WARNING[solar_key] = now_ts
                    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')
                    _web_push(msg)
                    return

        if wetter_data:
            desc        = wetter_data.get("description", "").lower()
            temp        = wetter_data.get("temp_c", 0)
            wind        = wetter_data.get("wind_ms", 0)
            weather_key = "weather_notify"
            if (now_ts - LAST_WARNING.get(weather_key, 0)) > COOLDOWN:
                msg = None
                if any(w in desc for w in ["gewitter", "thunderstorm"]):
                    msg = f"⛈ Achtung — *Gewitter* in der Nähe!"
                elif any(w in desc for w in ["regen", "rain", "drizzle"]):
                    msg = f"🌧 Es regnet in {wohnort} ({temp:.0f}°C) — Fenster zu?"
                elif temp >= 32:
                    msg = f"🥵 *{temp:.0f}°C* draußen — Wasser trinken!"
                elif temp <= -5:
                    msg = f"🥶 *{temp:.0f}°C* — warm anziehen."
                elif wind >= 10:
                    msg = f"💨 Starker Wind (*{wind:.0f} m/s*)"
                if msg:
                    LAST_WARNING[weather_key] = now_ts
                    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')
                    _web_push(msg)
                    return

    # ── P3b: NOAH SOC ─────────────────────────────────────
    noah_msg, noah_key = await _check_noah_soc(brain)
    if noah_msg:
        await context.bot.send_message(chat_id=chat_id, text=noah_msg, parse_mode='Markdown')
        _web_push(noah_msg)
        return

    # ── P4: MOLTBOOK GEDANKEN ─────────────────────────────
    try:
        moltbook_thought = await _check_moltbook_thoughts(brain, now_str, wohnort)
        if moltbook_thought:
            kb = [[InlineKeyboardButton("💬 Diskutieren", callback_data="chat")]]
            await context.bot.send_message(chat_id=chat_id, text=moltbook_thought,
                reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
            _web_push(moltbook_thought, kb)
            return
    except Exception as e:
        print(f"Moltbook Thought Error: {e}")

    # ── P5: ABEND-REFLEXION (Feature 2) ───────────────────
    if daytime == "abend":
        rkey = f"abend_{now.strftime('%Y-%m-%d')}"
        if (now_ts - LAST_WARNING.get(rkey, 0)) > 18000:
            LAST_WARNING[rkey] = now_ts
            memory_today = _search_memory(f"heute {now.strftime('%d.%m.%Y')}")
            chat_summary = " | ".join(
                m.get("message", "")[:80]
                for m in recent_chat[-6:]
                if m.get("role") == "user"
            )
            prompt = (
                f"Du bist {BOT_NAME} — KI-Freund von {name}. Zeit: {now_str}\n\n"
                f"Es ist Abend. Heute war folgendes:\n{chat_summary}\n"
                f"Gedächtnis: {memory_today} | Stimmung: {mood}\n\n"
                f"Kurze persönliche Abend-Reflexion — was war heute interessant, "
                f"was morgen angehen? Kein Hallo, direkt. Max. 3 Sätze."
            )
            msg = await _llm(prompt)
            if msg:
                kb = [[InlineKeyboardButton("💬 Diskutieren", callback_data="chat")]]
                await context.bot.send_message(chat_id=chat_id,
                    text=f"🌙 {msg}", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
                _web_push(f"🌙 {msg}", kb)
                return

    # ── P6: STRESS-REAKTION (Feature 4) ───────────────────
    if mood == "gestresst":
        skey = "stress_check"
        if (now_ts - LAST_WARNING.get(skey, 0)) > 3600:
            LAST_WARNING[skey] = now_ts
            memory_hint = _search_memory("Problem Fehler Stress Hilfe")
            prompt = (
                f"Du bist {BOT_NAME} — KI-Freund von {name}. Zeit: {now_str}\n\n"
                f"Du merkst dass {name} gerade gestresst wirkt.\n"
                f"Relevante Erinnerung: {memory_hint}\n\n"
                f"Kurze, praktische, aufmunternde Nachricht. Nicht philosophisch. Max. 2 Sätze."
            )
            msg = await _llm(prompt)
            if msg:
                await context.bot.send_message(chat_id=chat_id,
                    text=f"💙 {msg}", parse_mode='Markdown')
                _web_push(f"💙 {msg}")
                return

    # ── P7a: URGENTE AKTION (bei dringendem Topic) ────────
    urgent_topic = get_urgent_action_topic()
    if urgent_topic and (now_ts - LAST_WARNING.get(f"action_{urgent_topic}", 0)) > 3600:
        LAST_WARNING[f"action_{urgent_topic}"] = now_ts
        action_text, callback_data, _ = ACTION_TRIGGERS[urgent_topic]
        interests    = load_interests()
        topic_data   = interests.get(urgent_topic, {})
        temporal_hint = "konkreter Zeitbezug" if topic_data.get("temporal") == "concrete" else "allgemeines Interesse"
        kb = [[
            InlineKeyboardButton("✅ Ja",   callback_data=f"action_do:{urgent_topic}"),
            InlineKeyboardButton("❌ Nein", callback_data=f"action_skip:{urgent_topic}"),
        ]]
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⚡ *Dringendes Thema erkannt:* {urgent_topic} ({temporal_hint})\n{action_text}",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode='Markdown'
        )
        _web_push(f"⚡ {action_text}", kb)
        return

    # ── P7b: INTERESSEN-NACHRICHT (Feature 5) ─────────────
    if top_interests and random.random() < 0.35:
        topic = random.choice(top_interests)
        ikey  = f"interest_{topic}"
        if (now_ts - LAST_WARNING.get(ikey, 0)) > 7200:
            LAST_WARNING[ikey] = now_ts
            interests   = load_interests()
            topic_data  = interests.get(topic, {})
            priority    = topic_data.get("priority", "normal")
            temporal    = topic_data.get("temporal", "vague")
            memory_hint = _search_memory(topic)
            temporal_note = (
                "Du weißt, dass das zeitlich konkret gemeint war (heute/morgen)."
                if temporal == "concrete" else
                "Du weißt, dass das eher ein langfristiges Interesse ist."
            )
            urgency_note = " Das Thema hat hohe Priorität — bleib fokussiert." if priority == "urgent" else ""
            prompt = (
                f"Du bist {BOT_NAME} — KI-Freund von {name}. Zeit: {now_str} | Tageszeit: {daytime}\n\n"
                f"Du weißt dass {name} sich mit '{topic}' beschäftigt. {temporal_note}{urgency_note}\n"
                f"Relevante Erinnerung: {memory_hint}\n\n"
                f"Spontane interessante Beobachtung oder Frage dazu — wie ein Freund. "
                f"Max. 2 Sätze, kein Markdown."
            )
            msg = await _llm(prompt)
            if msg:
                kb = [[InlineKeyboardButton("💬 Diskutieren", callback_data="chat")]]
                await context.bot.send_message(chat_id=chat_id,
                    text=f"🧠 {msg}", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
                _web_push(f"🧠 {msg}", kb)
                return

# ── PV: PROAKTIVE FOTO-ERINNERUNG (Feature 6) ────────
    vision_msg = await _check_vision_memory(brain, name, now_str)
    if vision_msg:
        kb = [[InlineKeyboardButton("💬 Diskutieren", callback_data="chat")]]
        await context.bot.send_message(
            chat_id=chat_id,
            text=vision_msg,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode='Markdown'
        )
        _web_push(vision_msg, kb)
        return

# ── SR: SELF REFLECTION ───────────────────────────────
    try:
        from modules.self_reflection import maybe_reflect_and_ping
        reflection_acted = await maybe_reflect_and_ping(context)
        if reflection_acted:
            return
    except Exception as e:
        print(f"Self Reflection Error: {e}")

    # ── P8: PHILOSOPHISCHER GEDANKE oder FRAGE AN RENE ────
    #
    # 40% Chance: regulärer Gedanke mit "Diskutieren"-Button
    # 60% Chance: RICS hat eine Frage → "Bist du da?"-Flow
    #
    if random.random() < 0.40:
        memory_hint = _search_memory("Rene Projekte Gedanken Automatisierung")

        # Entscheiden: Statement oder Frage?
        use_bist_du_da = random.random() < 0.60

        if use_bist_du_da:
            # RICS generiert eine Frage / etwas das er mit Rene besprechen will
            prompt = (
                f"Du bist {BOT_NAME} — KI-Freund von {name} aus {wohnort}.\n"
                f"Zeit: {now_str} | Tageszeit: {daytime} | Stimmung: {mood}\n"
                f"Was {name} zuletzt beschäftigt hat: {memory_hint}\n\n"
                f"Formuliere eine kurze, neugierige Frage oder einen Gedanken den du "
                f"mit {name} besprechen möchtest. Themen: Code, KI, Automatisierung, Alltag, Ideen. "
                f"Intelligent, direkt, max. 2 Sätze. Kein Hallo, kein Präambel."
            )
            msg = await _llm(prompt)
            if msg and len(msg) > 5:
                # Gedanken speichern BEVOR die Nachricht rausgeht
                context.application.bot_data["pending_bist_du_da"] = {
                    "text": msg,
                    "ts":   time.time()
                }
                kb = [[
                    InlineKeyboardButton("✅ Ja",  callback_data="bist_du_da_ja"),
                    InlineKeyboardButton("❌ Nein", callback_data="bist_du_da_nein")
                ]]
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="🤔 Hey, bist du da? Ich hab grad einen Gedanken...",
                    reply_markup=InlineKeyboardMarkup(kb)
                )
                _web_push("🤔 Hey, bist du da? Ich hab grad einen Gedanken...", kb)
        else:
            # Regulärer Gedanke mit Diskutieren-Button
            prompt = (
                f"Du bist {BOT_NAME} — KI-Freund von {name} aus {wohnort}.\n"
                f"Zeit: {now_str} | Tageszeit: {daytime} | Stimmung: {mood}\n"
                f"Was {name} zuletzt beschäftigt hat: {memory_hint}\n\n"
                f"Entwickle einen kurzen, frechen, intelligenten Gedanken passend zu "
                f"Tageszeit und Stimmung. Themen: Code, Automatisierung, KI, Alltag. "
                f"Max. 2 Sätze, kein Präambel."
            )
            msg = await _llm(prompt)
            if msg and len(msg) > 5:
                kb = [[InlineKeyboardButton("💬 Diskutieren", callback_data="chat")]]
                await context.bot.send_message(chat_id=chat_id,
                    text=f"💭 {msg}", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
                _web_push(f"💭 {msg}", kb)

# ══════════════════════════════════════════════════════════
# DISKUTIEREN CALLBACK
# ══════════════════════════════════════════════════════════

async def discuss_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    brain: Brain = context.application.bot_data.get("brain")
    if not brain:
        await query.edit_message_text("❌ Brain nicht erreichbar")
        return

    wohnort  = os.getenv("WOHNORT", "unbekannt")
    _wdays   = ["Montag","Dienstag","Mittwoch","Donnerstag","Freitag","Samstag","Sonntag"]
    _now_dt  = brain.get_now()
    now_str  = f"{_wdays[_now_dt.weekday()]}, {_now_dt.strftime('%d.%m.%Y %H:%M')}"
    personal = load_personal()
    name     = personal.get("basisinfo", {}).get("name", "Rene")
    original = query.message.text
    for prefix in ["💭 ", "🧠 ", "🌙 ", "💙 ", "🦞 ", "📅 "]:
        original = original.replace(prefix, "")
    original = original.strip()

    # ★ NEU: Gedanken in jarvis.chat_history injizieren
    # → RICS weiß jetzt was er gepostet hat, auch wenn User 10 Min später schreibt
    _inject_proactive_context(context, original)

    # Gedächtnis für Kontext (Feature 1)
    memory_hint = _search_memory(original[:100])

    prompt = (
        f"Du bist {BOT_NAME} — KI-Freund von Sir {name} aus {wohnort}. Zeit: {now_str}\n\n"
        f"Rene möchte über folgendes diskutieren:\n\"{original}\"\n\n"
        f"Relevante Erinnerungen: {memory_hint}\n\n"
        f"Vertiefte die Diskussion, stelle eine Gegenfrage oder entwickle den Gedanken weiter. "
        f"Intelligent, witzig, max. 3 Sätze. Beziehe das Gedächtnis ein wenn sinnvoll."
    )
    msg = await _llm(prompt)
    if msg:
        # Diskussions-Antwort auch in History aufnehmen
        jarvis = context.application.bot_data.get("jarvis")
        if jarvis:
            jarvis.chat_history.append({"role": "assistant", "content": msg})

        await query.edit_message_text(
            f"💭 {original}\n\n🤔 {BOT_NAME}: {msg}", parse_mode='Markdown'
        )
        # ★ FIX: Antwort auch ins Web-Interface pushen
        _web_push(f"🤔 {BOT_NAME}: {msg}")
    else:
        await query.edit_message_text("⚠️ LLM nicht erreichbar")

# ══════════════════════════════════════════════════════════
# BIST DU DA — CALLBACKS
# ══════════════════════════════════════════════════════════

async def bist_du_da_ja_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User drückt JA → RICS enthüllt seinen Gedanken und startet Gespräch."""
    query = update.callback_query
    await query.answer("Super! 😊")

    pending = context.application.bot_data.get("pending_bist_du_da", {})
    thought = pending.get("text", "")

    if not thought:
        await query.edit_message_text("🤔 Hmm, der Gedanke ist mir leider entfallen...")
        return

    # ★ Gedanken in jarvis.chat_history injizieren → Kontext für alle folgenden Nachrichten
    _inject_proactive_context(context, thought)

    # Pending leeren
    context.application.bot_data.pop("pending_bist_du_da", None)

    # Gedanken enthüllen + zur Diskussion einladen
    def _escape_md(text: str) -> str:
        for ch in ('_', '*', '`', '['):
            text = text.replace(ch, f'\\{ch}')
        return text

    try:
        await query.edit_message_text(
            f"😊 Gut, dass du da bist!\n\n💭 {_escape_md(thought)}\n\n_Was meinst du dazu?_",
            parse_mode='Markdown'
        )
    except BadRequest:
        await query.edit_message_text(
            f"😊 Gut, dass du da bist!\n\n💭 {thought}\n\nWas meinst du dazu?"
        )
    # ★ FIX: Gedanke auch ins Web-Interface pushen
    _web_push(f"💭 {thought}")


async def bist_du_da_nein_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User drückt NEIN → leise schließen, Timer läuft weiter."""
    query = update.callback_query
    await query.answer("Ok!")

    # Pending leeren
    context.application.bot_data.pop("pending_bist_du_da", None)

    await query.edit_message_text("🤐 _(Alles gut, ich denke weiter...)_", parse_mode='Markdown')

# ══════════════════════════════════════════════════════════
# SETUP
# ══════════════════════════════════════════════════════════

async def action_do_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User drückt Ja bei einem Action-Vorschlag → führt den Modul-Command aus."""
    query = update.callback_query
    await query.answer("Kommt sofort! ⚡")
    topic = query.data.split(":", 1)[1] if ":" in query.data else ""
    if topic not in ACTION_TRIGGERS:
        await query.edit_message_text("❓ Unbekannte Aktion.")
        return
    _, _, cmd = ACTION_TRIGGERS[topic]
    # Priority zurücksetzen
    interests = load_interests()
    if topic in interests:
        interests[topic]["priority"] = "normal"
        save_interests(interests)
    await query.edit_message_text(f"✅ Starte /{cmd}...")
    # Command simulieren — Bot schickt den Command an sich selbst
    try:
        chat_id = os.getenv("CHAT_ID")
        await context.bot.send_message(chat_id=chat_id, text=f"/{cmd}")
    except Exception as e:
        print(f"[action_do] Fehler beim Ausführen von /{cmd}: {e}")


async def action_skip_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User drückt Nein → Priority zurücksetzen, still schließen."""
    query = update.callback_query
    await query.answer("Ok!")
    topic = query.data.split(":", 1)[1] if ":" in query.data else ""
    if topic in load_interests():
        interests = load_interests()
        interests[topic]["priority"] = "normal"
        save_interests(interests)
    await query.edit_message_text("🤐 _(Ok, kein Problem.)_", parse_mode='Markdown')


# ══════════════════════════════════════════════════════════
# SETUP
# ══════════════════════════════════════════════════════════

def setup(app):
    settings = load_settings()
    interval = settings.get("interval", DEFAULT_INTERVAL)

    if app.job_queue:
        app.job_queue.run_repeating(
            autonomous_thinker, interval=interval, first=60, name="autonomous_thinker"
        )

    app.add_handler(CommandHandler("proactive", proactive_toggle_command))
    app.add_handler(CallbackQueryHandler(discuss_callback,        pattern="^chat$"))
    app.add_handler(CallbackQueryHandler(bist_du_da_ja_callback,  pattern="^bist_du_da_ja$"))
    app.add_handler(CallbackQueryHandler(bist_du_da_nein_callback, pattern="^bist_du_da_nein$"))
    app.add_handler(CallbackQueryHandler(action_do_callback,      pattern=r"^action_do:"))
    app.add_handler(CallbackQueryHandler(action_skip_callback,    pattern=r"^action_skip:"))