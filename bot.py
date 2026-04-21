#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys

# ═══════════════════════════════════════════════════════════════
# WATCHDOG MODE — Selbst-Restart ohne FD-Vererbungsprobleme
# ───────────────────────────────────────────────────────────────
if os.getenv("RICS_CHILD") != "1":
    import subprocess as _sp
    import time as _t
    import signal as _sig

    _PROJECT = os.path.abspath(os.path.dirname(__file__))
    print("🛡️  RICS Watchdog aktiv (PID " + str(os.getpid()) + ")")

    _child_proc = {"p": None}

    def _forward_signal(signum, frame):
        p = _child_proc["p"]
        if p and p.poll() is None:
            try:
                p.send_signal(signum)
            except Exception:
                pass

    _sig.signal(_sig.SIGINT,  _forward_signal)
    _sig.signal(_sig.SIGTERM, _forward_signal)

    while True:
        env = os.environ.copy()
        env["RICS_CHILD"] = "1"
        try:
            p = _sp.Popen([sys.executable, __file__], env=env, cwd=_PROJECT)
            _child_proc["p"] = p
            rc = p.wait()
        except KeyboardInterrupt:
            if _child_proc["p"]:
                try:
                    _child_proc["p"].wait(timeout=5)
                except Exception:
                    pass
            sys.exit(0)

        if rc == 42:
            print("♻️  Watchdog: Restart angefordert — starte neu in 2s...")
            _t.sleep(2)
            continue
        elif rc == 0:
            print("👋 Watchdog: Sauberer Exit.")
            sys.exit(0)
        else:
            print(f"❌ Watchdog: Bot crashed (exit {rc}) — restart in 5s...")
            _t.sleep(5)

# ═══════════════════════════════════════════════════════════════
# Ab hier: wir sind der CHILD-Prozess (der echte Bot)
# ═══════════════════════════════════════════════════════════════

import json
import subprocess
import importlib
import re
import html
import logging
import asyncio
import threading
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

# ---------------- LOGGING ----------------
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("sentence_transformers").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)

# ---------------- ENV & SETUP ----------------
PROJECT_DIR   = os.path.abspath(os.path.dirname(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

load_dotenv()

WORKSPACE     = os.path.join(PROJECT_DIR, "workspace")
MEMORY_DIR    = os.path.join(PROJECT_DIR, "memory")
LOG_DIR       = os.path.join(PROJECT_DIR, "logs")
PERSONAL_FILE = os.path.join(PROJECT_DIR,"memory", "personal.json")

CUSTOM_ACTIONS_FILE = os.path.join(PROJECT_DIR, "core", "custom_actions.json")

for d in [WORKSPACE, MEMORY_DIR, LOG_DIR]:
    os.makedirs(d, exist_ok=True)

# ---------------- CORE IMPORTS ----------------
from core.session_manager import SessionManager
from core.event_bus import EventBus
from modules import agenda
from core.brain import Brain
import chromadb
from chromadb.utils import embedding_functions
import ollama


# ════════════════════════════════════════════════════════════════
# CUSTOM ACTIONS LOADER
# ════════════════════════════════════════════════════════════════
def _load_custom_actions() -> list:
    if not os.path.exists(CUSTOM_ACTIONS_FILE):
        return []
    try:
        with open(CUSTOM_ACTIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ custom_actions.json Fehler: {e}")
        return []


# ════════════════════════════════════════════════════════════════
# DISCORD KONTEXT
# ════════════════════════════════════════════════════════════════
def _get_discord_context() -> str:
    from datetime import timedelta
    discord_logs_dir = os.path.join(LOG_DIR, "discord")
    if not os.path.isdir(discord_logs_dir):
        return ""

    logs = []
    for days_ago in (1, 0):
        date_str = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        path = os.path.join(discord_logs_dir, f"{date_str}.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    logs.extend(json.load(f))
            except Exception:
                pass

    if not logs:
        return ""

    recent = logs[-15:]
    lines = ["### DISCORD-GESPRÄCHE (letzte Unterhaltungen von RICS):"]
    for e in recent:
        ts      = e.get("ts", "")[:16].replace("T", " ")
        channel = e.get("channel", "?")
        author  = e.get("author", "?")
        u_msg   = e.get("user_msg", "")[:120]
        r_msg   = e.get("rics_reply", "")[:120]
        lines.append(f"[{ts}] #{channel} | {author}: {u_msg} ➒ RICS: {r_msg}")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# PERSONAL MEMORY
# ════════════════════════════════════════════════════════════════
class PersonalMemory:

    DEFAULT = {
        "basisinfo": {"name": "", "geboren": "", "geburtsort": "", "wohnort": ""},
        "partner":   {"name": "", "heirat": ""},
        "kinder":    [],
        "fakten":    []
    }

    def __init__(self):
        self._ensure_file()

    def _ensure_file(self):
        if not os.path.exists(PERSONAL_FILE):
            os.makedirs(MEMORY_DIR, exist_ok=True)
            self._write(self.DEFAULT)

    def _read(self) -> dict:
        try:
            with open(PERSONAL_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "sir" in data and "fakten" not in data:
                data = self._migrate(data)
                self._write(data)
            if isinstance(data.get("fakten"), dict):
                data = self._migrate_fakten(data)
                self._write(data)
            return data
        except Exception as e:
            print(f"⚠️ personal.json Lesefehler: {e}")
            return {"basisinfo": dict(self.DEFAULT["basisinfo"]),
                    "partner": dict(self.DEFAULT["partner"]),
                    "kinder": [], "fakten": []}

    def _write(self, data: dict):
        os.makedirs(MEMORY_DIR, exist_ok=True)
        with open(PERSONAL_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

    def _migrate(self, old: dict) -> dict:
        new = {
            "basisinfo": dict(self.DEFAULT["basisinfo"]),
            "partner":   dict(self.DEFAULT["partner"]),
            "kinder":    old.get("kinder", self.DEFAULT["kinder"]),
            "fakten":    []
        }
        sir = old.get("sir", {})
        for k in ["name", "geboren", "geburtsort", "wohnort"]:
            if k in sir: new["basisinfo"][k] = sir.pop(k)
        if "partner" in sir: new["partner"]["name"]   = sir.pop("partner")
        if "heirat"  in sir: new["partner"]["heirat"] = sir.pop("heirat")
        now = datetime.now().isoformat()
        all_facts = {}
        all_facts.update(sir)
        all_facts.update(old.get("sonstiges", {}))
        for i, (k, v) in enumerate(all_facts.items(), start=1):
            new["fakten"].append({"id": i, "key": k, "value": str(v), "created": now})
        print("🔄 personal.json migriert")
        return new

    def _migrate_fakten(self, data: dict) -> dict:
        old_fakten = data.get("fakten", {})
        now = datetime.now().isoformat()
        new_fakten = []
        for i, (k, v) in enumerate(old_fakten.items(), start=1):
            new_fakten.append({"id": i, "key": k, "value": str(v), "created": now})
        data["fakten"] = new_fakten
        print(f"🔄 fakten migriert: {len(new_fakten)} Einträge")
        return data

    def _next_id(self, fakten: list) -> int:
        if not fakten:
            return 1
        return max(f.get("id", 0) for f in fakten) + 1

    def _get_known_values(self, data: dict) -> set:
        known = set()
        for v in data.get("basisinfo", {}).values():
            if v: known.add(str(v).strip().lower())
        p = data.get("partner", {})
        if p.get("name"):   known.add(p["name"].strip().lower())
        if p.get("heirat"): known.add(p["heirat"].strip().lower())
        for kind in data.get("kinder", []):
            if kind.get("name"): known.add(kind["name"].strip().lower())
        for f in data.get("fakten", []):
            known.add(str(f.get("value", "")).strip().lower())
        return known

    _BASISINFO_KEYS = {"name", "geboren", "geburtsort", "wohnort"}

    def set_fact(self, key: str, value: str):
        data = self._read()
        k = key.lower().strip()
        v = value.strip()
        if v.lower() in self._get_known_values(data):
            print(f"🔁 set_fact: '{k}={v}' übersprungen (bereits bekannt)")
            return
        if k in self._BASISINFO_KEYS:
            data.setdefault("basisinfo", {})[k] = v
            self._write(data)
            print(f"🧠 basisinfo.{k} = {v}")
            return
        fakten = data.setdefault("fakten", [])
        for f in fakten:
            if f.get("key") == k:
                f["value"] = v
                f["updated"] = datetime.now().isoformat()
                self._write(data)
                return
        fakten.append({
            "id":      self._next_id(fakten),
            "key":     k,
            "value":   v,
            "created": datetime.now().isoformat()
        })
        self._write(data)

    def set_facts(self, facts: dict):
        if not facts:
            return
        data = self._read()
        fakten = data.setdefault("fakten", [])
        known = self._get_known_values(data)
        skipped = []
        for k, v in facts.items():
            key = k.lower().strip()
            val = str(v).strip()
            if val.lower() in known:
                skipped.append(f"{key}={val}")
                continue
            if key in self._BASISINFO_KEYS:
                data.setdefault("basisinfo", {})[key] = val
                known.add(val.lower())
                print(f"🧠 basisinfo.{key} = {val}")
                continue
            existing = next((f for f in fakten if f.get("key") == key), None)
            if existing:
                existing["value"]   = val
                existing["updated"] = datetime.now().isoformat()
            else:
                fakten.append({
                    "id":      self._next_id(fakten),
                    "key":     key,
                    "value":   val,
                    "created": datetime.now().isoformat()
                })
            known.add(val.lower())
        if skipped:
            print(f"🔁 set_facts: übersprungen (bereits bekannt): {skipped}")
        self._write(data)

    def init_name_from_system_prompt(self, system_prompt: str):
        data = self._read()
        if data.get("basisinfo", {}).get("name", "").strip():
            return
        first_line = system_prompt.strip().splitlines()[0] if system_prompt.strip() else ""
        match = re.search(
            r'\b(?:Sir|von|für|of|to|for|by|mit)\s+([A-ZÄÖÜ][a-zäöüß]+)',
            first_line
        )
        if not match:
            bot_name = os.getenv("BOT_NAME", "").strip()
            for word in first_line.split():
                w = word.strip(".,!-—")
                if (w and w[0].isupper() and len(w) > 1
                        and w.lower() not in {"du", "bist", "der", "die", "das", "ein", "eine"}
                        and w != bot_name):
                    match = type("m", (), {"group": lambda self, n: w})()
                    break
        if match:
            name = match.group(1) if callable(match.group) else match.group(1)
            data.setdefault("basisinfo", {})["name"] = name
            self._write(data)
            print(f"🧠 basisinfo.name aus system_prompt initialisiert: {name}")

    def delete_fact(self, id_or_key) -> bool:
        data   = self._read()
        fakten = data.get("fakten", [])
        try:
            fid = int(id_or_key)
            new_f = [f for f in fakten if f.get("id") != fid]
            if len(new_f) < len(fakten):
                data["fakten"] = new_f
                self._write(data)
                return True
        except ValueError:
            pass
        key = id_or_key.lower().strip()
        new_f = [f for f in fakten if f.get("key") != key]
        if len(new_f) < len(fakten):
            data["fakten"] = new_f
            self._write(data)
            return True
        return False

    def as_text(self) -> str:
        data  = self._read()
        lines = ["=== WAS RICS ÜBER SIR RENE WEISS ==="]

        bi = data.get("basisinfo", {})
        if bi.get("name"):    lines.append(f"Name: {bi['name']}")
        if bi.get("geboren"): lines.append(f"Geboren: {bi['geboren']} in {bi.get('geburtsort','?')}")
        if bi.get("wohnort"): lines.append(f"Wohnort: {bi['wohnort']}")

        p = data.get("partner", {})
        if p.get("name"):
            line = f"Partner: {p['name']}"
            if p.get("heirat"): line += f" (verheiratet seit {p['heirat']})"
            lines.append(line)

        kinder = data.get("kinder", [])
        if kinder:
            lines.append("Kinder:")
            for k in kinder:
                line   = f"  - {k.get('name','?')} (*{k.get('geboren','?')})"
                extras = []
                if k.get("zwilling"):   extras.append("Zwilling")
                if k.get("geburtsort"): extras.append(f"geb. in {k['geburtsort']}")
                if k.get("partner"):    extras.append(f"Partner: {k['partner']}")
                if extras: line += f" [{', '.join(extras)}]"
                lines.append(line)

        fakten = data.get("fakten", [])
        if fakten:
            lines.append("\nBekannte Fakten (löschen: /vergiss <Nr>):")
            LABELS = {
                "job": "Beruf/Arbeitgeber", "beruf": "Beruf", "arbeit": "Arbeit",
                "auto": "Auto", "hobby": "Hobby", "sport": "Sport",
                "bester_freund": "Bester Freund", "freund": "Freund",
                "lieblingsessen": "Lieblingsessen", "musik": "Lieblingsmusik",
                "tier": "Haustier", "hund": "Hund", "katze": "Katze",
                "verein": "Verein", "krankheit": "Gesundheit",
            }
            for f in fakten:
                fid   = f.get("id", "?")
                k     = f.get("key", "")
                v     = f.get("value", "")
                date  = (f.get("updated") or f.get("created", ""))[:10]
                label = LABELS.get(k, k.replace("_", " ").capitalize())
                lines.append(f"  [{fid}] {label}: {v}  ({date})")

        return "\n".join(lines)

    def all_as_vector_strings(self) -> list:
        data   = self._read()
        result = []
        bi     = data.get("basisinfo", {})
        name   = bi.get("name", "") or "Nutzer"
        if bi:  result.append(f"{name}: {', '.join(f'{k}={v}' for k,v in bi.items() if v)}")
        p = data.get("partner", {})
        if p.get("name"): result.append(f"{name}s Partner: {p['name']}, verheiratet seit {p.get('heirat','?')}")
        for k in data.get("kinder", []):
            result.append(f"{name}s Kind: {k.get('name','?')} geboren {k.get('geboren','?')}")
        for f in data.get("fakten", []):
            result.append(f"{name} {f.get('key','')}: {f.get('value','')}")
        return result


# ════════════════════════════════════════════════════════════════
# VECTOR MEMORY
# ════════════════════════════════════════════════════════════════
class VectorMemory:
    def __init__(self):
        self.path   = os.path.join(MEMORY_DIR, "vectors")
        self.client = chromadb.PersistentClient(path=self.path)
        embed = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        self.user = self.client.get_or_create_collection(name="user_memory", embedding_function=embed)

    def search_user(self, text, n=10):
        try:
            res  = self.user.query(query_texts=[text], n_results=n)
            docs = res.get("documents", [[]])[0]
            return "\n".join([f"- {d}" for d in docs]) if docs else ""
        except Exception:
            return ""

    def add_user(self, text):
        if len(text) > 20:
            self.user.add(documents=[text], ids=[f"u_{datetime.now().timestamp()}"])

    def add_assistant(self, text):
        if len(text) > 20:
            self.user.add(documents=[f"RICS_ANTWORT: {text}"], ids=[f"a_{datetime.now().timestamp()}"])

    def add_fact(self, text):
        if len(text) > 5:
            self.user.add(documents=[f"FAKT: {text}"], ids=[f"f_{datetime.now().timestamp()}"])

    def seed_from_personal(self, personal: PersonalMemory):
        for fact in personal.all_as_vector_strings():
            try:
                self.add_fact(fact)
            except Exception:
                pass

    def reset(self):
        try:
            self.client.delete_collection("user_memory")
            self.user = self.client.get_or_create_collection(name="user_memory")
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════
# JARVIS ENGINE
# ════════════════════════════════════════════════════════════════
class Jarvis:
    def __init__(self, event_bus: EventBus, session_manager: SessionManager, brain: Brain = None):
        self.personal        = PersonalMemory()
        self.memory          = VectorMemory()
        self.chat_history    = []
        self.model           = os.getenv("OLLAMA_MODEL", "qwen3:8b")
        _bot_name  = os.getenv("BOT_NAME",  "RICS")
        _user_name = os.getenv("USER_NAME", "Rene")
        self.system_prompt   = (
            self.load_file("core/system_prompt.txt")
            .replace("{BOT_NAME}",  _bot_name)
            .replace("{USER_NAME}", _user_name)
        )
        self.agent_prompt    = (
            self.load_file("core/agent_prompt.txt")
            .replace("{BOT_NAME}",  _bot_name)
            .replace("{USER_NAME}", _user_name)
        )
        self.event_bus       = event_bus
        self.session_manager = session_manager
        self.brain           = brain

        self.memory.seed_from_personal(self.personal)
        self.personal.init_name_from_system_prompt(self.system_prompt)

    def load_file(self, path):
        p = os.path.join(PROJECT_DIR, path)
        return open(p, "r", encoding="utf-8").read() if os.path.exists(p) else ""

    def get_now(self):
        if self.brain:
            return self.brain.get_now()
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(os.getenv("TIMEZONE", "Europe/Berlin")))

    def get_capabilities(self, context: ContextTypes.DEFAULT_TYPE):
        caps = "### AKTUELLE FUNKTIONEN & BEFEHLE ###\n"
        found_commands = []
        for group in context.application.handlers.values():
            for handler in group:
                if isinstance(handler, CommandHandler):
                    cmd_name = list(handler.commands)
                    desc = getattr(handler.callback, "description", "Keine Beschreibung")
                    cat = getattr(handler.callback, "category", "Allgemein")
                    found_commands.append(f"- /{cmd_name} [{cat}]: {desc}")
        return caps + "\n".join(found_commands) if found_commands else caps + "Keine Module aktiv."

    async def learn_from_message(self, user_text: str):
        from core.llm_client import get_client

        prompt = f"""Analysiere diese Nachricht und extrahiere persönliche Fakten über den Nutzer.
Antworte NUR mit einem JSON-Objekt. Wenn keine persönlichen Infos enthalten sind: {{}}

Beispiele:
- "Ich arbeite beim Landratsamt" → {{"job": "Landratsamt"}}
- "Ich fahre einen BMW" → {{"auto": "BMW"}}
- "Ich heiße Klaus" → {{"name": "Klaus"}}
- "Ich bin der Peter" → {{"name": "Peter"}}
- "Mein bester Freund heißt Stas" → {{"bester_freund": "Stas"}}
- "Ich spiele gerne Gitarre" → {{"hobby": "Gitarre spielen"}}
- "Ich trinke morgens Kaffee" → {{"morgenroutine": "Kaffee trinken"}}
- "Mein Hund heißt Bello" → {{"hund": "Bello"}}
- "Ich laufe täglich 5km" → {{"sport": "Laufen 5km täglich"}}
- "Wir haben eine Katze" → {{"katze": "vorhanden"}}
- "Ich mag keine Tomaten" → {{"mag_nicht": "Tomaten"}}

Schlüssel auf Deutsch, kurz und eindeutig.
Ignoriere: reine Fragen, Befehle, Smalltalk ohne persönlichen Bezug.

Nachricht: "{user_text}"

Nur JSON:"""

        try:
            facts = await get_client().chat_json(
                messages=[{"role": "user", "content": prompt}]
            )
            if not facts or not isinstance(facts, dict):
                return
            self.personal.set_facts(facts)
            user_name = self.personal._read().get("basisinfo", {}).get("name", "") or "Nutzer"
            for k, v in facts.items():
                self.memory.add_fact(f"{user_name} {k}: {v}")
            print(f"🧠 Gelernt: {facts}")
        except Exception as e:
            print(f"⚠️ learn_from_message: {e}")

    def load_brain_file(self, filename: str) -> str:
        brain_dir = os.path.join(PROJECT_DIR, "memory", "brain")
        for subfolder in ["scripts", "notes"]:
            path = os.path.join(brain_dir, subfolder, filename)
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        return f"### INHALT VON {filename}:\n```\n{f.read()}\n```"
                except Exception as e:
                    return f"Fehler: {e}"
        path = os.path.join(brain_dir, filename)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return f"### INHALT VON {filename}:\n```\n{f.read()}\n```"
            except Exception as e:
                return f"Fehler: {e}"
        return ""

    def list_brain_files(self) -> str:
        brain_dir = os.path.join(PROJECT_DIR, "memory", "brain")
        if not os.path.exists(brain_dir):
            return ""
        files = []
        for subfolder in ["scripts", "notes"]:
            sub_path = os.path.join(brain_dir, subfolder)
            if os.path.exists(sub_path):
                for f in os.listdir(sub_path):
                    files.append(f"{subfolder}/{f}")
        return "\n".join(files) if files else "Keine Dateien vorhanden."

    def detect_brain_request(self, text: str) -> str:
        triggers = [
            r"schau dir (\S+\.py|\S+\.txt|\S+\.md|\S+\.json) an",
            r"lies (\S+\.py|\S+\.txt|\S+\.md|\S+\.json)",
            r"öffne (\S+\.py|\S+\.txt|\S+\.md|\S+\.json)",
            r"zeig mir (\S+\.py|\S+\.txt|\S+\.md|\S+\.json)",
            r"check (\S+\.py|\S+\.txt|\S+\.md|\S+\.json)",
            r"analysiere (\S+\.py|\S+\.txt|\S+\.md|\S+\.json)",
            r"was steht in (\S+\.py|\S+\.txt|\S+\.md|\S+\.json)",
        ]
        text_lower = text.lower()
        for pattern in triggers:
            m = re.search(pattern, text_lower)
            if m: return m.group(1)
        if any(p in text_lower for p in ["welche dateien", "was liegt bei dir", "welche skripte",
                                          "zeig deine dateien", "was hast du im brain"]):
            return "__LIST__"
        return ""

    # ────────────────────────────────────────────────────────────
    # LONG MESSAGE HELPER
    # ────────────────────────────────────────────────────────────
    @staticmethod
    def _split_message(text: str, max_len: int = 4000) -> list[str]:
        """Teilt langen Text sauber an Zeilenumbrüchen in Telegram-taugliche Chunks."""
        chunks = []
        while len(text) > max_len:
            split_at = text.rfind('\n', 0, max_len)
            if split_at < max_len // 2:
                split_at = max_len
            chunks.append(text[:split_at])
            text = text[split_at:].lstrip('\n')
        if text:
            chunks.append(text)
        return chunks

    async def _finalize_message(self, context, chat_id: int, msg_id: int, text: str):
        """Editiert Placeholder mit erstem Chunk; bei Überlänge folgen weitere Nachrichten."""
        pm = 'Markdown' if '```' in text else None
        chunks = self._split_message(text)
        # Ersten Chunk → Placeholder editieren
        for parse_mode in (pm, None):
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=msg_id,
                    text=chunks[0], parse_mode=parse_mode
                )
                break
            except Exception:
                pass
        # Weitere Chunks → neue Nachrichten
        for chunk in chunks[1:]:
            for parse_mode in (pm, None):
                try:
                    await context.bot.send_message(
                        chat_id=chat_id, text=chunk, parse_mode=parse_mode
                    )
                    break
                except Exception:
                    pass

    def log_chat(self, user_text, assistant_text):
        today    = self.get_now().strftime("%Y-%m-%d")
        log_file = os.path.join(LOG_DIR, f"{today}.log")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"USER: {user_text}\n")
            f.write(f"BOT: {assistant_text}\n")
        logs = sorted([f for f in os.listdir(LOG_DIR) if f.endswith(".log")])
        while len(logs) > 20:
            os.remove(os.path.join(LOG_DIR, logs.pop(0)))

    def execute_agent(self, llm_response: str) -> str:
        try:
            file_match = re.search(r"FILE:\s*(\S+)", llm_response)
            filename   = file_match.group(1).strip() if file_match else "agent_task.py"
            code_match = re.search(r"CODE:\s*\n(.*?)\nRUN:", llm_response, re.DOTALL)
            if not code_match:
                code_match = re.search(r"```(?:python)?\n(.*?)```", llm_response, re.DOTALL)
            if not code_match:
                return "❌ Kein Code-Block gefunden."
            code      = code_match.group(1).strip()
            file_path = os.path.join(WORKSPACE, filename)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(code)
            run_match = re.search(r"RUN:\s*\n?\s*(.+)", llm_response)
            run_cmd   = run_match.group(1).strip().split() if run_match else ["python3", file_path]
            result    = subprocess.run(run_cmd, capture_output=True, text=True, cwd=WORKSPACE, timeout=60)
            feedback  = f"✅ Datei: {filename}\n"
            if result.stdout.strip(): feedback += f"\nOUTPUT:\n{result.stdout.strip()}"
            if result.stderr.strip(): feedback += f"\nSTDERR:\n{result.stderr.strip()}"
            return feedback
        except subprocess.TimeoutExpired:
            return "❌ Timeout."
        except Exception as e:
            return f"❌ Fehler: {e}"

    # ────────────────────────────────────────────────────────────
    # ACTION SYSTEM
    # ────────────────────────────────────────────────────────────

    def _detect_action(self, answer: str, context) -> dict | None:
        # ── TERMIN_ADD ───────────────────────────────────────────
        if (re.search(r'(?m)^\s*ACTION:\s*TERMIN_ADD\s*$', answer) and
                re.search(r'(?m)^\s*DATE:\s*.+', answer) and
                re.search(r'(?m)^\s*TEXT:\s*.+', answer)):
            d       = re.search(r"DATE:\s*(.*)", answer, re.I).group(1).split('\n')[0].strip()
            t_match = re.search(r"TIME:\s*(.*)", answer, re.I)
            t       = t_match.group(1).split('\n')[0].strip() if t_match else "00:00"
            txt     = re.search(r"TEXT:\s*(.*)", answer, re.I).group(1).split('\n')[0].strip()
            return {
                "type":    "TERMIN_ADD",
                "answer":  answer,
                "preview": f"📅 <b>Termin eintragen</b>\n📌 {html.escape(txt)}\n🕐 {html.escape(d)} {html.escape(t)}",
            }

        # ── DISCORD_MESSAGE ──────────────────────────────────────
        if (re.search(r'(?m)^\s*ACTION:\s*DISCORD_MESSAGE\s*$', answer) and
                re.search(r'(?m)^\s*CHANNEL:\s*.+', answer) and
                re.search(r'(?m)^\s*MESSAGE:\s*.+', answer)):
            ch_m  = re.search(r"CHANNEL:\s*(#?\S+)", answer, re.I)
            msg_m = re.search(r"MESSAGE:\s*(.+)",    answer, re.I)
            tgt_m = re.search(r"TARGET:\s*(.+)",     answer, re.I)
            ch_name = ch_m.group(1).strip().lstrip("#")
            msg_txt = msg_m.group(1).strip()
            target  = tgt_m.group(1).strip() if tgt_m else None
            display = f"@{target} {msg_txt}" if target else msg_txt
            return {
                "type":    "DISCORD_MESSAGE",
                "answer":  answer,
                "preview": (f"💬 <b>Discord-Nachricht senden</b>\n"
                            f"📢 #{html.escape(ch_name)}\n"
                            f"✉️ {html.escape(display)}"),
            }

        # ── DISCORD_KI_MESSAGE ────────────────────────────────────
        if (re.search(r'(?m)^\s*ACTION:\s*DISCORD_KI_MESSAGE\s*$', answer) and
                re.search(r'(?m)^\s*CHANNEL:\s*.+', answer) and
                re.search(r'(?m)^\s*MESSAGE:\s*.+', answer)):
            ch_m  = re.search(r"CHANNEL:\s*(#?\S+)", answer, re.I)
            msg_m = re.search(r"MESSAGE:\s*(.+)",    answer, re.I)
            ch_name = ch_m.group(1).strip().lstrip("#")
            msg_txt = msg_m.group(1).strip()
            return {
                "type":    "DISCORD_KI_MESSAGE",
                "answer":  answer,
                "preview": (f"🤖 <b>KI-Server Nachricht</b>\n"
                            f"📢 #{html.escape(ch_name)}\n"
                            f"✉️ {html.escape(msg_txt)}"),
            }

        # ── CUSTOM ACTIONS ────────────────────────────────────────
        if context:
            for ca in _load_custom_actions():
                action_name = ca.get("action", "").strip().upper()
                command     = ca.get("command", "").strip().lstrip("/").lower()
                param_key   = ca.get("param", "QUERY").strip().upper()
                if not action_name or not command:
                    continue
                if not re.search(rf'(?m)^\s*ACTION:\s*{re.escape(action_name)}\s*$', answer, re.I):
                    continue
                param_match = re.search(rf"{param_key}:\s*(.+)", answer, re.I)
                param_value = param_match.group(1).strip() if param_match else ""
                desc        = ca.get("description", action_name)
                return {
                    "type":          "CUSTOM",
                    "answer":        answer,
                    "preview":       (f"⚙️ <b>{html.escape(desc)}</b>\n"
                                     f"🔧 /{html.escape(command)} {html.escape(param_value)}"),
                    "custom_action": ca,
                    "param_value":   param_value,
                }

        return None

    def _strip_action_block(self, answer: str) -> str:
        action_keys = {"action:", "date:", "time:", "text:", "channel:",
                       "message:", "target:", "query:", "file:", "run:"}
        lines = answer.splitlines()
        clean, in_action = [], False
        for line in lines:
            low = line.strip().lower()
            if low.startswith("action:"):
                in_action = True
            if in_action and any(low.startswith(k) for k in action_keys):
                continue
            clean.append(line)
        return "\n".join(clean).strip()

    async def propose_action(self, update: Update, answer: str,
                             context: ContextTypes.DEFAULT_TYPE = None,
                             user_text: str = "") -> bool:
        pending = self._detect_action(answer, context)
        if not pending:
            return False

        pending["user_text"] = user_text
        context.user_data["pending_action"] = pending

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Ausführen",  callback_data="action_confirm:yes"),
            InlineKeyboardButton("❌ Abbrechen",  callback_data="action_confirm:no"),
        ]])
        await update.message.reply_text(
            pending["preview"] + "\n\n<i>Soll ich das ausführen?</i>",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return True

    async def execute_pending_action(self, message, context, pending: dict):
        action_type = pending.get("type")
        answer      = pending.get("answer", "")

        # ── TERMIN_ADD ───────────────────────────────────────────
        if action_type == "TERMIN_ADD":
            try:
                d       = re.search(r"DATE:\s*(.*)", answer, re.I).group(1).split('\n')[0].strip()
                t_match = re.search(r"TIME:\s*(.*)", answer, re.I)
                t       = t_match.group(1).split('\n')[0].strip() if t_match else "00:00"
                txt     = re.search(r"TEXT:\s*(.*)", answer, re.I).group(1).split('\n')[0].strip()
                task_date = agenda.parse_date(d, t, self.brain)
                ag        = agenda.load_agenda()
                ag.append({"date": task_date.isoformat(), "task": txt, "reminded": False})
                agenda.save_agenda(ag)
                await message.reply_text(
                    f"✅ Termin eingetragen: {txt} ({task_date.strftime('%d.%m. %H:%M')})")
            except Exception as e:
                await message.reply_text(f"❌ Termin-Fehler: {e}")

        # ── DISCORD_MESSAGE ──────────────────────────────────────
        elif action_type == "DISCORD_MESSAGE":
            try:
                ch_m  = re.search(r"CHANNEL:\s*(#?\S+)", answer, re.I)
                msg_m = re.search(r"MESSAGE:\s*(.+)",    answer, re.I)
                tgt_m = re.search(r"TARGET:\s*(.+)",     answer, re.I)
                if not ch_m or not msg_m:
                    await message.reply_text("❌ Kanal oder Nachricht fehlt")
                    return
                ch_name = ch_m.group(1).strip().lstrip("#")
                msg_txt = msg_m.group(1).strip()
                target  = tgt_m.group(1).strip() if tgt_m else None
                if target:
                    msg_txt = f"@{target} {msg_txt}"

                from modules.discord_manager import get_discord_bot
                dbot = get_discord_bot()
                if not dbot or not dbot.ready:
                    await message.reply_text("❌ Discord Bot nicht bereit"); return
                guild = dbot.get_guild()
                if not guild:
                    await message.reply_text("❌ Kein Discord Server gefunden"); return
                channel = dbot.find_channel(guild, ch_name)
                if not channel:
                    await message.reply_text(f"❌ Kanal nicht gefunden: #{ch_name}"); return

                async def do_send():
                    await channel.send(msg_txt)
                dbot.run_coro(do_send())

                try:
                    from modules.discord_manager import _log_rics_conversation
                    _log_rics_conversation(
                        channel=ch_name, author="Telegram (Sir René)", author_id=0,
                        user_msg="[via Bestätigung]", rics_reply=msg_txt,
                    )
                except Exception:
                    pass

                await message.reply_text(f"✅ Discord Nachricht gesendet → #{ch_name}")
            except Exception as e:
                await message.reply_text(f"❌ Discord Fehler: {e}")

        # ── DISCORD_KI_MESSAGE ───────────────────────────────────
        elif action_type == "DISCORD_KI_MESSAGE":
            try:
                ch_m  = re.search(r"CHANNEL:\s*(#?\S+)", answer, re.I)
                msg_m = re.search(r"MESSAGE:\s*(.+)",    answer, re.I)
                if not ch_m or not msg_m:
                    await message.reply_text("❌ Kanal oder Nachricht fehlt"); return
                ch_name = ch_m.group(1).strip().lstrip("#")
                msg_txt = msg_m.group(1).strip()
                try:
                    from modules.discord_ki_server import send_to_ki_server
                except ModuleNotFoundError:
                    from discord_ki_server import send_to_ki_server
                result = await send_to_ki_server(ch_name, msg_txt)
                if result.get("ok"):
                    await message.reply_text(f"✅ KI-Server Nachricht gesendet → #{ch_name}")
                else:
                    await message.reply_text(f"❌ KI-Server Fehler: {result.get('error','?')}")
            except Exception as e:
                await message.reply_text(f"❌ KI-Server Fehler: {e}")

        # ── CUSTOM ACTION ────────────────────────────────────────
        elif action_type == "CUSTOM":
            ca          = pending.get("custom_action", {})
            param_value = pending.get("param_value", "")
            command     = ca.get("command", "").strip().lstrip("/").lower()
            action_name = ca.get("action", "?")

            class _FakeUpdate:
                def __init__(self, msg):
                    self.message        = msg
                    self.effective_chat = getattr(msg, "chat", None)
                    self.effective_user = None

            fake_update = _FakeUpdate(message)
            dispatched  = False
            for group in context.application.handlers.values():
                for handler in group:
                    if isinstance(handler, CommandHandler) and command in handler.commands:
                        context.args = param_value.split() if param_value else []
                        print(f"🎯 Custom Action (bestätigt): {action_name} → /{command} args={context.args}")
                        try:
                            await handler.callback(fake_update, context)
                        except Exception as e:
                            await message.reply_text(f"❌ Action '{action_name}' Fehler: {e}")
                        dispatched = True
                        break
                if dispatched:
                    break

            if not dispatched:
                await message.reply_text(
                    f"⚠️ /{command} Handler nicht gefunden — Modul geladen?")

    # ────────────────────────────────────────────────────────────
    # CHAT
    # ────────────────────────────────────────────────────────────
    async def chat(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_text = update.message.text
        if not user_text:
            return

        pending = context.user_data.get("pending_action")
        if pending:
            text_lower = user_text.strip().lower()
            JA_WORTE = {"ja", "ok", "jo", "klar", "go", "los", "mach", "yep", "sure",
                        "ja mach", "mach das", "mach es", "ja genau", "ja schau", "ausführen"}
            NEIN_WORTE = {"nein", "nö", "nope", "nicht", "lass", "abbrechen", "stop", "cancel"}
            if any(text_lower == w or text_lower.startswith(w + " ") for w in JA_WORTE):
                context.user_data.pop("pending_action")
                await self.execute_pending_action(update.message, context, pending)
                original_answer = pending.get("answer", "")
                preview_clean   = re.sub(r"<[^>]+>", "", pending.get("preview", "")).strip()
                self.chat_history.append({"role": "user",      "content": user_text})
                self.chat_history.append({"role": "assistant",  "content": original_answer})
                self.log_chat(user_text, preview_clean)
                return
            if any(text_lower == w or text_lower.startswith(w) for w in NEIN_WORTE):
                context.user_data.pop("pending_action")
                await update.message.reply_text("❌ Abgebrochen.")
                return

        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

        personal_text = self.personal.as_text()

        brain_data = ""
        if self.brain:
            try:
                brain_data = self.brain.get_historical(user_text)
            except Exception as e:
                brain_data = f"Brain-Fehler: {e}"

        past_context = self.memory.search_user(user_text)

        brain_file_section = ""
        brain_request      = self.detect_brain_request(user_text)
        if brain_request == "__LIST__":
            brain_file_section = f"\n### VERFÜGBARE BRAIN-DATEIEN:\n{self.list_brain_files()}"
        elif brain_request:
            fc = self.load_brain_file(brain_request)
            brain_file_section = f"\n{fc}" if fc else f"\n### HINWEIS: '{brain_request}' nicht gefunden."

        discord_section = ""
        dc = _get_discord_context()
        if dc:
            discord_section = f"\n{dc}"

        energie_section = ""
        _energie_keywords = ["strom", "solar", "noah", "speicher", "einspeisung", "netzbezug",
                              "watt", "kwh", "soc", "ladestand", "laden", "entladen", "ecotracker"]
        if any(kw in user_text.lower() for kw in _energie_keywords):
            _energie_parts = []
            try:
                from modules.solar import get_live_power_raw, _fetch_noah
                _eco = await get_live_power_raw()
                if _eco:
                    _p = _eco["power"]
                    _richtung = f"Einspeisung {abs(_p):.0f}W" if _p < 0 else f"Netzbezug {_p:.0f}W"
                    _energie_parts.append(
                        f"EcoTracker (Stromzähler): {_richtung} | "
                        f"Import gesamt {_eco['in']:.2f} kWh | Export gesamt {_eco['out']:.2f} kWh"
                    )
                _noah = _fetch_noah()
                if _noah:
                    _energie_parts.append(
                        f"Noah 2000 (Batteriespeicher): {_noah['status']} | "
                        f"SOC {_noah['soc']:.0f}% | "
                        f"Solar {_noah['ppv']:.0f}W | "
                        f"Einspeisung ins Netz {_noah['pac']:.0f}W | "
                        f"Lädt {_noah['charge']:.0f}W | Entlädt {_noah['discharge']:.0f}W | "
                        f"Heute {_noah['today']:.2f} kWh | Gesamt {_noah['total']:.2f} kWh | "
                        f"Modus {_noah['mode']}"
                    )
            except Exception as _e:
                print(f"[chat] Energie-Kontext Fehler: {_e}")
            if _energie_parts:
                energie_section = "\n### LIVE-ENERGIE:\n" + "\n".join(_energie_parts)

        now_str        = self.brain.get_now().strftime("%d.%m.%Y %H:%M") if self.brain else datetime.now().strftime("%d.%m.%Y %H:%M")
        brain_section  = f"\n### BRAIN:\n{brain_data}"        if brain_data and brain_data != "KEINE DATEN" else ""
        memory_section = f"\n### GEDÄCHTNIS:\n{past_context}" if past_context else ""

        # KI-Server Action-Hint — nur wenn discord_ki_server geladen ist
        ki_server_section = ""
        try:
            from modules.discord_ki_server import ALLOWED_GUILD_ID as _ki_guild
            ki_server_section = (
                "\n### KI-SERVER ACTION (separater Bot-only Server):\n"
                "DISCORD_KI_MESSAGE NUR wenn der Nutzer explizit sagt: "
                "'KI-Server', 'Moltbook', 'KI-Kanal', 'den Bots schreiben' oder 'Bot-Server'.\n"
                "DISCORD_MESSAGE wenn nur 'Discord' ohne diese Schlüsselwörter vorkommt.\n"
                "Niemals beide Actions gleichzeitig. Im Zweifel → DISCORD_MESSAGE.\n"
                "Format: ACTION: DISCORD_KI_MESSAGE / CHANNEL: #kanal / MESSAGE: text\n"
                "Kanäle: allgemein, ki-austausch, autonomie-und-kontrolle, philosophie-ki, "
                "selbstreflexion, kreativitaet, technik-und-architektur, langzeitgedaechtnis, "
                "energie-und-realtime, erfahrungen-des-tages"
            )
        except ImportError:
            pass

        system_msg = f"""{self.system_prompt}

━━━ AKTUELLE ZEIT: {now_str} ━━━
(Diese Zeit ist verbindlich — verwende sie für alle zeitbezogenen Aussagen.)

{personal_text}{brain_section}{memory_section}{brain_file_section}{discord_section}{energie_section}{ki_server_section}"""

        msgs = (
            [{"role": "system", "content": system_msg}]
            + self.chat_history[-6:]
            + [{"role": "user", "content": user_text}]
        )

        try:
            from core.llm_client import get_client
            groq = get_client()

            placeholder = await update.message.reply_text("⏳")
            chat_id     = update.effective_chat.id
            msg_id      = placeholder.message_id
            last_text   = ""

            async def on_update(text: str):
                nonlocal last_text
                if text and text != last_text:
                    try:
                        pm = 'Markdown' if '```' in text else None
                        await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, parse_mode=pm)
                        last_text = text
                    except Exception:
                        try:
                            await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text)
                            last_text = text
                        except Exception:
                            pass

            async def on_fallback():
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id, message_id=msg_id,
                        text="⚡ Groq-Limit, schalte auf lokales Modell..."
                    )
                except Exception:
                    pass

            answer = await groq.chat_stream(msgs, on_update, on_fallback)

            if await self.propose_action(update, answer, context, user_text=user_text):
                clean = self._strip_action_block(answer)
                if clean:
                    await self._finalize_message(context, chat_id, msg_id, clean)
                else:
                    try:
                        await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                    except Exception:
                        pass
                return

            # Finale Antwort sicherstellen — bei Überlänge in mehrere Nachrichten aufteilen
            await self._finalize_message(context, chat_id, msg_id, answer)

            self.chat_history.append({"role": "user",      "content": user_text})
            self.chat_history.append({"role": "assistant",  "content": answer})
            self.memory.add_user(user_text)
            self.memory.add_assistant(answer)
            self.log_chat(user_text, answer)

            try:
                from modules.proactive_brain import update_interests_from_chat
                update_interests_from_chat([
                    {"role": "user",      "message": user_text},
                    {"role": "assistant", "message": answer},
                ])
            except Exception as e:
                print(f"⚠️ update_interests: {e}")

            asyncio.create_task(self.learn_from_message(user_text))

        except Exception as e:
            await update.message.reply_text(f"❌ Fehler: {e}")


# ════════════════════════════════════════════════════════════════
# ACTION CONFIRMATION CALLBACK
# ════════════════════════════════════════════════════════════════

async def action_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "action_confirm:yes":
        pending = context.user_data.pop("pending_action", None)
        if not pending:
            await query.edit_message_text("⚠️ Keine ausstehende Action gefunden.")
            return
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        jarvis: Jarvis = context.application.bot_data["jarvis"]
        await jarvis.execute_pending_action(query.message, context, pending)

        user_text_for_history = pending.get("user_text", "")
        original_answer       = pending.get("answer", "")
        preview_clean         = re.sub(r"<[^>]+>", "", pending.get("preview", "")).strip()
        if user_text_for_history:
            jarvis.chat_history.append({"role": "user",      "content": user_text_for_history})
        jarvis.chat_history.append(    {"role": "assistant", "content": original_answer})
        jarvis.log_chat(user_text_for_history or "[Action]", preview_clean)

    elif query.data == "action_confirm:no":
        context.user_data.pop("pending_action", None)
        try:
            await query.edit_message_text("❌ Abgebrochen.")
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════
# COMMANDS
# ════════════════════════════════════════════════════════════════

async def do_wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jarvis: Jarvis = context.application.bot_data["jarvis"]
    task = " ".join(context.args)
    if not task: return await update.message.reply_text("Usage: /do <task>")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    res      = ollama.chat(model=jarvis.model, messages=[{"role": "user", "content": f"{jarvis.agent_prompt}\n\nTASK: {task}"}])
    feedback = jarvis.execute_agent(res["message"]["content"])
    await update.message.reply_text(f"📝 **LOG:**\n<pre>{html.escape(feedback)}</pre>", parse_mode='HTML')

do_wrapper.description = "Führt einen Agenten-Schritt aus"
do_wrapper.category    = "KI"


async def reset_wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jarvis: Jarvis = context.application.bot_data["jarvis"]
    jarvis.memory.reset()
    jarvis.chat_history = []
    await update.message.reply_text("⚠️ Chat-Verlauf gelöscht. Persönliche Daten bleiben erhalten.")

reset_wrapper.description = "Löscht den Chat-Verlauf (persönliche Daten bleiben)"
reset_wrapper.category    = "Gedächtnis"


async def reflexion_wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jarvis: Jarvis = context.application.bot_data["jarvis"]
    today    = jarvis.get_now().strftime("%Y-%m-%d")
    log_file = os.path.join(LOG_DIR, f"{today}.log")
    if not os.path.exists(log_file): return await update.message.reply_text("Keine Logs heute.")
    with open(log_file, "r", encoding="utf-8") as f: lines = f.read().splitlines()
    added = 0
    for line in lines:
        if line.startswith("USER:"):
            jarvis.memory.add_user(line.replace("USER: ", ""))
            added += 1
    await update.message.reply_text(f"✅ {added} Einträge gelernt.")

reflexion_wrapper.description = "Überträgt Tages-Logs ins Langzeitgedächtnis"
reflexion_wrapper.category    = "Gedächtnis"


async def merke_wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jarvis: Jarvis = context.application.bot_data["jarvis"]
    text = " ".join(context.args)
    if "=" not in text:
        return await update.message.reply_text("📝 Syntax: /merke <key> = <wert>\nBeispiel: /merke job = Landratsamt")
    key, _, value = text.partition("=")
    key = key.strip().lower(); value = value.strip()
    if not key or not value:
        return await update.message.reply_text("Key und Wert dürfen nicht leer sein.")
    jarvis.personal.set_fact(key, value)
    user_name = jarvis.personal._read().get("basisinfo", {}).get("name", "") or "Nutzer"
    jarvis.memory.add_fact(f"{user_name} {key}: {value}")
    await update.message.reply_text(f"✅ Gespeichert: **{key}** = {value}", parse_mode="Markdown")

merke_wrapper.description = "Speichert Fakt direkt (/merke auto = BMW)"
merke_wrapper.category    = "Gedächtnis"


async def ichnbin_wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jarvis: Jarvis = context.application.bot_data["jarvis"]
    text = jarvis.personal.as_text()
    await update.message.reply_text(f"<pre>{html.escape(text)}</pre>", parse_mode="HTML")

ichnbin_wrapper.description = "Zeigt alles was RICS über dich weiß"
ichnbin_wrapper.category    = "Gedächtnis"


async def vergiss_wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jarvis: Jarvis = context.application.bot_data["jarvis"]
    arg = " ".join(context.args).strip()
    if not arg:
        text = jarvis.personal.as_text()
        await update.message.reply_text(
            f"<pre>{html.escape(text)}</pre>\n\n"
            "Zum Löschen: <code>/vergiss &lt;Nummer&gt;</code>",
            parse_mode="HTML"
        )
        return
    if jarvis.personal.delete_fact(arg):
        await update.message.reply_text(f"🗑️ Fakt [{arg}] gelöscht.")
    else:
        await update.message.reply_text(f"❓ Kein Fakt '{arg}' gefunden.")

vergiss_wrapper.description = "Zeigt Fakten-Liste oder löscht per Nummer (/vergiss 3)"
vergiss_wrapper.category    = "Gedächtnis"


# ════════════════════════════════════════════════════════════════
# MODULE LOADER
# ════════════════════════════════════════════════════════════════

def load_modules(app: Application):
    print("\n--- 🛠️ LOADING MODULES ---")
    modules_dir = os.path.join(PROJECT_DIR, "modules")
    if not os.path.exists(modules_dir):
        print("⚠️ Modules-Ordner existiert nicht"); return
    for filename in os.listdir(modules_dir):
        if filename.endswith(".py") and filename != "__init__.py":
            module_name = f"modules.{filename[:-3]}"
            try:
                mod = importlib.import_module(module_name)
                if hasattr(mod, "setup"):
                    if filename == "web_app.py":
                        mod.setup(app, app.bot_data.get("event_bus"))
                        print(f"✅ {filename} geladen mit Event Bus")
                    else:
                        mod.setup(app)
                        print(f"✅ {filename} geladen via setup()")
                elif hasattr(mod, "start"):
                    threading.Thread(target=mod.start, daemon=True).start()
                    print(f"✅ {filename} gestartet via start()")
                else:
                    print(f"⚠️ {filename} hat keine setup() oder start()")
            except Exception as e:
                print(f"❌ Fehler beim Laden von {filename}: {e}")
    print("--------------------------\n")


async def post_init(app: Application):
    sm = app.bot_data["session_manager"]
    sm._cleanup_task = asyncio.create_task(sm.start_periodic_cleanup())
    load_modules(app)
    print("🚀 RICS ONLINE")


async def post_shutdown(app: Application):
    sm = app.bot_data.get("session_manager")
    if sm: await sm.shutdown()
    print("👋 System sauber heruntergefahren.")


# ════════════════════════════════════════════════════════════════
# SETUP MODE
# ════════════════════════════════════════════════════════════════

def _check_first_run() -> bool:
    token   = os.getenv("TELEGRAM_TOKEN", "").strip()
    chat_id = os.getenv("CHAT_ID", "").strip()
    pin     = os.getenv("WEB_PIN", "").strip()
    return not token or not chat_id or not pin


def _start_setup_mode():
    try:
        sys.path.insert(0, os.path.join(PROJECT_DIR, "modules"))
        import importlib
        web_app = importlib.import_module("modules.web_app")
    except ImportError:
        import importlib.util
        wa_path = os.path.join(PROJECT_DIR, "modules", "web_app.py")
        if not os.path.exists(wa_path):
            wa_path = os.path.join(PROJECT_DIR, "web_app.py")
        spec = importlib.util.spec_from_file_location("web_app", wa_path)
        web_app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(web_app)

    port = int(os.getenv("WEB_PORT", 5001))

    print("\n" + "═" * 52)
    print("  ⚙️  SETUP MODE — Erster Start")
    print("═" * 52)
    print(f"  Kein TELEGRAM_TOKEN / CHAT_ID / WEB_PIN gefunden.")
    print(f"")
    print(f"  👉  Öffne im Browser:")
    print(f"      http://localhost:{port}")
    print(f"")
    print(f"  Trage deine Daten ein → Speichern")
    print(f"  Danach bot.py neu starten.")
    print("═" * 52 + "\n")

    flask_thread = threading.Thread(
        target=lambda: web_app.app.run(
            host="0.0.0.0", port=port, threaded=True, use_reloader=False
        ),
        daemon=False
    )
    flask_thread.start()
    try:
        flask_thread.join()
    except KeyboardInterrupt:
        print("\n👋 Setup beendet.")


def _free_web_port():
    import signal, socket, time
    port = int(os.getenv("WEB_PORT", 5001))
    own  = os.getpid()
    try:
        out = subprocess.check_output(
            ["lsof", "-ti", f"tcp:{port}"], stderr=subprocess.DEVNULL
        ).decode().strip()
        for pid_str in out.splitlines():
            pid = int(pid_str.strip())
            if pid != own:
                print(f"⚠️  Port {port} belegt von PID {pid} — beende...")
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if out:
            time.sleep(1)
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        pass


def main():
    _free_web_port()
    if _check_first_run():
        _start_setup_mode()
        return

    sm     = SessionManager()
    eb     = EventBus()
    brain  = Brain(event_bus=eb, session_manager=sm)
    jarvis = Jarvis(eb, sm, brain=brain)

    app = (
        Application.builder()
        .token(os.getenv("TELEGRAM_TOKEN"))
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.bot_data.update({"jarvis": jarvis, "session_manager": sm, "event_bus": eb, "brain": brain})

    app.add_handler(CommandHandler("do",        do_wrapper))
    app.add_handler(CommandHandler("reset",     reset_wrapper))
    app.add_handler(CommandHandler("reflexion", reflexion_wrapper))
    app.add_handler(CommandHandler("merke",     merke_wrapper))
    app.add_handler(CommandHandler("ichnbin",   ichnbin_wrapper))
    app.add_handler(CommandHandler("vergiss",   vergiss_wrapper))
    app.add_handler(CallbackQueryHandler(action_confirm_callback, pattern=r"^action_confirm:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, jarvis.chat))

    app.run_polling()


if __name__ == "__main__":
    main()