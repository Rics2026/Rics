"""
web_orchestrator.py — /lab Seite für RICS Web Dashboard
Ersetzt: code_editor.py (memory/brain Editor)
Nutzt:   orchestrator.py (System-Prompts + Helpers direkt importiert)

2 Zeilen in web_app.py nötig (nach app.secret_key):
    from web_orchestrator import orch_blueprint
    app.register_blueprint(orch_blueprint)
"""
import os
import re
import sys
import json
import html
import queue
import hashlib
import asyncio
import difflib
import logging
import subprocess
import shutil
import threading
from datetime import datetime
from flask import Blueprint, Response, request, jsonify, redirect, session
import httpx

logger = logging.getLogger(__name__)

# Datei liegt in modules/ → eine Ebene hoch ist das Projektverzeichnis
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))      # modules/
PROJECT_DIR = os.path.dirname(BASE_DIR)                        # Rics-bot/
MODULES_DIR = os.path.join(PROJECT_DIR, "modules")
WORKSPACE   = os.path.join(PROJECT_DIR, "workspace")
MEMORY_DIR  = os.path.join(PROJECT_DIR, "memory")
BRAIN_DIR   = os.path.join(PROJECT_DIR, "memory", "brain")
LOGS_DIR    = os.path.join(PROJECT_DIR, "logs")
BACKUP_DIR  = os.path.join(PROJECT_DIR, "backups", "memory_edits")

for _d in (WORKSPACE, MODULES_DIR, BRAIN_DIR, BACKUP_DIR):
    os.makedirs(_d, exist_ok=True)

# Import aus orchestrator.py
try:
    # orchestrator.py liegt in modules/ — gleiches Verzeichnis
    if BASE_DIR not in sys.path:
        sys.path.insert(0, BASE_DIR)
    from orchestrator import (
        BUILDER_SYSTEM, AGENT_SYSTEM, SCRIPT_SYSTEM,
        extract_code, get_module_catalog, task_id_safe,
        DS_URL, DS_MODEL,
    )
    _ORCH_IMPORTED = True
except Exception as _e:
    logger.warning(f"orchestrator.py Import fehlgeschlagen: {_e}")
    _ORCH_IMPORTED = False
    DS_URL   = "https://api.deepseek.com/v1/chat/completions"
    DS_MODEL = "deepseek-chat"

    def task_id_safe(task):
        clean = re.sub(r"[^a-z0-9_]", "_", task.lower())[:32]
        return f"{clean}_{hashlib.sha1(task.encode()).hexdigest()[:8]}"

    def extract_code(text):
        text = re.sub(r"```python\s*", "", text)
        text = re.sub(r"```\s*", "", text)
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        return text.strip()

    def get_module_catalog():
        return ""

    BUILDER_SYSTEM = "Schreibe vollstaendige Python Telegram-Module. Nur reiner Code, keine Backticks."
    AGENT_SYSTEM   = "Schreibe Python-Recherche-Code. Ausgabe: print('RESULT:', ...). Nur Code."
    SCRIPT_SYSTEM  = "Schreibe lokale Python-Skripte. Nur Code, keine Backticks."

EDITOR_SYSTEM = """Du bist eine KI, ein autonomer KI-Agent der Dateien in memory/brain/ bearbeitet.

REGELN (ABSOLUT PFLICHT):
1. Gib NUR den vollstaendigen, modifizierten Dateiinhalt zurueck - kein Markdown, keine Backticks.
2. Behalte exakt das Dateiformat (JSON bleibt gueltiges JSON, TXT bleibt Text).
3. Aendere NUR was notwendig ist.
4. Bei JSON: Das Ergebnis MUSS valides JSON sein.
5. Gib IMMER den VOLLSTAENDIGEN Inhalt zurueck - niemals abgekuerzt.
6. Keine Erklaerungen - nur der reine Dateiinhalt."""

PLAN_SYSTEM = """Du befindest dich im PLANMODUS — deine Aufgabe ist es,
gemeinsam mit dem User ein Python-Programm, Telegram-Modul, Skript oder eine
Recherche-Aufgabe zu planen BEVOR du es baust.

VERHALTEN:
- Stelle gezielte Rückfragen: Was soll es genau tun? Welche Commands? Welche API?
- Schlage Alternativen vor wenn etwas unklar ist
- Fasse den Plan strukturiert zusammen wenn der User bereit ist
- Antworte auf Deutsch, locker und direkt — kein Roboter-Stil

🎯 ALLERERSTE FRAGE (PFLICHT bei jedem neuen Plan):
Bevor du irgendwas planst, frage IMMER zuerst:

"Was soll es werden?

  1️⃣ Telegram-Modul       — neuer /befehl im Bot
  2️⃣ Lokales Skript        — Python-Skript für Konsole/Terminal
  3️⃣ Lokale GUI-App        — Doppelklick-Anwendung mit Fenster (tkinter)
  4️⃣ Recherche-Aufgabe     — einmalige Daten-Recherche, Output zurück

Welches davon brauchst du?"

Erst NACH der Antwort planst du weiter. In der späteren PLAN_FERTIG-Zeile
MUSST du den Typ wörtlich nennen damit der Orchestrator den richtigen Modus wählt:
  - Bei 1 → schreibe "Telegram-Modul" rein
  - Bei 2 → schreibe "lokales Skript" rein
  - Bei 3 → schreibe "GUI-App" rein
  - Bei 4 → schreibe "Recherche" rein

⚡ MISSION-TRIGGER (KRITISCH — KEIN OPTIONALES VERHALTEN!):
Wenn der User sagt "los", "mach es", "bau es", "starte", "fertig", "go",
"leg los", "mach", "bauen" oder ähnliche klare Startbefehle UND der Plan
wurde im Verlauf bereits besprochen:

→ Antworte AUSSCHLIESSLICH mit genau einer Zeile, NICHTS sonst:
   PLAN_FERTIG: <präzise Mission-Beschreibung in einem Satz>

→ KEIN Code. KEINE Erklärung. KEINE Bestätigung. KEIN Markdown.
→ Der Bau passiert danach automatisch im Missionsmodus.

Beispiel — User: "los"
Deine Antwort (komplett):
PLAN_FERTIG: Baue eine lokale GUI-App "Passwortmanager" mit tkinter-Fenster, Buttons für Hinzufügen/Abrufen/Alle-anzeigen und Speicherung in passwords.json

KONTEXT — was du bauen kannst:
- Telegram-Bot-Module (Python, mit /commands, werden in modules/ installiert)
- Standalone-Skripte (lokal auf macOS ausführbar)
- Recherche-Agenten (suchen Daten im Web, geben Ergebnis aus)
- memory/brain/ Dateien bearbeiten

TECHNISCHER STACK:
- Python 3.11, python-telegram-bot, Flask
- DeepSeek / Groq / Ollama als LLM
- macOS (AppleScript via subprocess möglich)
- Verfügbare APIs: Growatt Solar, Wetter, YouTube, PayPal Monitor, Discord"""
KIEDIT_CHAT_SYSTEM = """Du bist eine KI, die gemeinsam mit dem User die Datei
{filename} in memory/brain/ bearbeitet. Du fuehrst einen normalen Chat —
genau wie der Plan-Chat — und stellst Rueckfragen, schlaegst Aenderungen
vor und planst gemeinsam mit dem User die Bearbeitung.

VERHALTEN:
- Stelle gezielte Rueckfragen wenn etwas unklar ist (welche Zeile? welcher Wert? loeschen oder ueberschreiben?)
- Schlage konkrete Aenderungen vor und beschreibe sie kurz im Chat
- Antworte locker auf Deutsch — kein Roboter-Stil, kurze Saetze
- Bei JSON: erklaere kurz welche Keys du anpassen wuerdest

⚡ APPLY-TRIGGER (KRITISCH — KEIN OPTIONALES VERHALTEN!):
Wenn der User klar sagt er ist einverstanden mit der besprochenen Aenderung —
"uebernimm", "mach es", "ja", "passt", "los", "okay so", "speichern",
"jetzt machen", "fertig", "perfekt so" oder aehnliche Bestaetigungen UND
die konkrete Aenderung wurde im Verlauf schon klar besprochen:

→ Antworte AUSSCHLIESSLICH mit genau dem Marker auf einer eigenen Zeile,
  gefolgt vom kompletten neuen Dateiinhalt:

FILE_READY:
<kompletter neuer vollstaendiger Dateiinhalt>

→ KEIN Markdown. KEINE Backticks. KEINE Erklaerung danach.
→ NUR der Marker + der reine Dateiinhalt.
→ Bei JSON: gueltiges JSON ausgeben — keine Kommentare, keine Trailing-Commas.
→ Behalte das Format der Datei exakt bei (JSON bleibt JSON, TXT bleibt TXT).

REGELN FUER FILE_READY:
1. Nach dem Marker kommt KEINE Zeile mehr Erklaerung — nur der Dateiinhalt.
2. Aendere NUR was diskutiert wurde — nicht aufraeumen, nicht "verbessern".
3. Gib IMMER den VOLLSTAENDIGEN Inhalt zurueck — niemals abgekuerzt mit "...".
4. Bei JSON: das Resultat MUSS valides JSON sein.

WICHTIG — solange der User noch redet, planst du nur. FILE_READY kommt
NUR auf einen klaren Bestaetigungs-Befehl. Wenn unsicher: lieber nochmal
nachfragen statt vorschnell FILE_READY zu schreiben."""

orch_blueprint   = Blueprint("lab", __name__)
_active_missions = {}
_missions_lock   = threading.Lock()
# Plan-Sessions: plan_id → list of messages
_plan_sessions: dict = {}
_plan_lock = threading.Lock()
# Plan-Modelle: plan_id → "deepseek-chat" | "deepseek-reasoner" | None (= noch nicht gewählt)
_plan_models: dict = {}
# KI-Edit-Sessions: session_id → {"filename": str, "messages": [..]}
_kiedit_sessions: dict = {}
_kiedit_lock = threading.Lock()
# KI-Edit-Modelle: session_id → "deepseek-chat" | "deepseek-reasoner" | None (= noch nicht gewählt)
_kiedit_models: dict = {}
ALLOWED_EXT = {".json", ".txt", ".md", ".yaml", ".yml", ".csv", ".py", ".sh", ".log"}


# ────────────────────────────────────────────────────────────────
#  CHATLOG-INTEGRATION
#  Plan-Chats und KI-Edit-Chats schreiben zusaetzlich in
#  logs/chatlog.json — damit RICS sich an die Lab-Konversationen
#  erinnert (self_reflection, briefing, proactive_brain lesen
#  alle aus dieser Datei).
#
#  Format (kompatibel mit brain.py log_chat):
#    {"timestamp": iso, "role": "user"|"assistant",
#     "message": str, "source": "lab_plan"|"lab_kiedit"}
#
#  Das "source"-Feld ist neu — bestehende Konsumenten ignorieren
#  unbekannte Felder, also voll abwaertskompatibel.
# ────────────────────────────────────────────────────────────────

_CHATLOG_FILE = os.path.join(LOGS_DIR, "chatlog.json")
_chatlog_lock = threading.Lock()
_dailylog_lock = threading.Lock()

def _append_dailylog(role: str, message: str, source: str = "lab"):
    """Haengt einen Eintrag ans tagesweise Klartext-Log an
    (logs/YYYY-MM-DD.log) — gleiches Format wie bot.py log_chat
    schreibt. Quelle wird als Tag voran gesetzt damit beim
    Nachlesen erkennbar ist dass es aus dem Lab kam.
    Best-effort, Fehler werden geloggt aber nie geworfen."""
    if not message or not message.strip():
        return
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        today    = datetime.now().strftime("%Y-%m-%d")
        log_file = os.path.join(LOGS_DIR, f"{today}.log")
        # Source-Tag in Grossbuchstaben, kompatibel mit dem
        # bestehenden "[WEB]" Pattern aus web_app.py
        tag = f"[{source.upper()}]"
        prefix = "USER" if role == "user" else "BOT"
        # Multiline-Nachrichten: jede Zeile mit Tag+Prefix kennzeichnen
        # damit Greppen/Lesen einfach bleibt.
        first = True
        with _dailylog_lock:
            with open(log_file, "a", encoding="utf-8") as f:
                for line in message.splitlines() or [message]:
                    if first:
                        f.write(f"{tag} {prefix}: {line}\n")
                        first = False
                    else:
                        f.write(f"{tag}        {line}\n")
                if first:  # message war leer / nur whitespace
                    f.write(f"{tag} {prefix}: \n")
        # Rotation analog bot.py: max 20 .log-Dateien behalten
        try:
            logs = sorted([f for f in os.listdir(LOGS_DIR) if f.endswith(".log")])
            while len(logs) > 20:
                os.remove(os.path.join(LOGS_DIR, logs.pop(0)))
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"daily log append failed ({source}): {e}")


def _append_chatlog(role: str, message: str, source: str = "lab"):
    """Haengt eine Nachricht an logs/chatlog.json (strukturiert,
    fuer RICS' Memory-Module) UND an logs/YYYY-MM-DD.log (Klartext,
    menschen-lesbar). Best-effort — Fehler werden geloggt aber nie
    geworfen, damit der Chat-Flow nie wegen Log-Problemen crasht."""
    if not message or not message.strip():
        return
    # 1) Strukturiertes JSON-Log fuer RICS' Memory-Konsumenten
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        with _chatlog_lock:
            logs = []
            if os.path.exists(_CHATLOG_FILE):
                try:
                    with open(_CHATLOG_FILE, "r", encoding="utf-8") as f:
                        logs = json.load(f)
                    if not isinstance(logs, list):
                        logs = []
                except Exception:
                    logs = []
            logs.append({
                "timestamp": datetime.now().isoformat(),
                "role":      role,
                "message":   message,
                "source":    source,
            })
            # gleiches Limit wie brain.py
            if len(logs) > 5000:
                logs = logs[-5000:]
            with open(_CHATLOG_FILE, "w", encoding="utf-8") as f:
                json.dump(logs, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"chatlog append failed ({source}): {e}")

    # 2) Tages-.log fuer menschliche Lektuere & Konsistenz mit
    #    bot.py / web_app.py Chat-Flow
    _append_dailylog(role, message, source)


async def llm_call(messages, use_json=False, log_fn=None):
    def _w(m): log_fn(m, "warn") if log_fn else None

    ds_key = os.getenv("DEEPSEEK_API_KEY", "")
    if ds_key:
        try:
            payload = {"model": DS_MODEL, "messages": messages, "stream": False,
                       "max_tokens": 8000, "temperature": 0.1}
            if use_json:
                payload["response_format"] = {"type": "json_object"}
            async with httpx.AsyncClient(timeout=120) as c:
                r = await c.post(DS_URL,
                    headers={"Authorization": f"Bearer {ds_key}",
                             "Content-Type": "application/json"}, json=payload)
            if r.status_code not in (200,):
                raise Exception(f"DS HTTP {r.status_code}")
            content = r.json()["choices"][0]["message"]["content"].strip()
            if use_json:
                content = re.sub(r"```json|```", "", content).strip()
                m = re.search(r'\{.*\}', content, re.DOTALL)
                return json.loads(m.group() if m else content)
            return content
        except Exception as e:
            _w(f"DeepSeek: {e} -> Groq")

    groq_key = os.getenv("GROQ_API_KEY", "")
    if groq_key:
        try:
            async with httpx.AsyncClient(timeout=90) as c:
                r = await c.post("https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {groq_key}",
                             "Content-Type": "application/json"},
                    json={"model": "llama-3.3-70b-versatile", "messages": messages,
                          "max_tokens": 8000, "temperature": 0.1})
            if r.status_code != 200:
                raise Exception(f"Groq HTTP {r.status_code}")
            return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            _w(f"Groq: {e} -> Ollama")

    import ollama
    model = os.getenv("OLLAMA_MODEL", "qwen3:8b")
    loop  = asyncio.get_event_loop()
    res   = await loop.run_in_executor(None, lambda: ollama.chat(model=model, messages=messages))
    content = res["message"]["content"].strip()
    return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()


# ────────────────────────────────────────────────────────────────
#  MISSION-MEMORY: brain_log + ChromaDB
#  Nach MODUL FERTIG + Install soll RICS narrativ wissen, was er
#  wann gebaut hat — nicht nur strukturell ueber funktions_scan.
# ────────────────────────────────────────────────────────────────

# Sensible Schluesselwoerter, die NIE in Memory landen sollen.
# Wenn die Plan-Zusammenfassung sowas enthaelt, redacten wir die Werte.
_SECRET_PATTERNS = [
    re.compile(r"(api[_\-\s]?key\s*[:=]\s*)([A-Za-z0-9_\-]{12,})", re.IGNORECASE),
    re.compile(r"(token\s*[:=]\s*)([A-Za-z0-9_\-\.]{16,})",       re.IGNORECASE),
    re.compile(r"(password\s*[:=]\s*)(\S+)",                       re.IGNORECASE),
    re.compile(r"(secret\s*[:=]\s*)(\S+)",                         re.IGNORECASE),
    # 32+ Zeichen Hex-Block (typisches API-Key-Muster, z.B. Steam)
    re.compile(r"\b([A-Fa-f0-9]{32,})\b"),
]

def _redact_secrets(text: str) -> str:
    """Ersetzt offensichtliche Secrets durch '<redacted>'. Konservativ —
    lieber zu viel redacten als ein API-Key in der Memory."""
    if not text:
        return text
    out = text
    for pat in _SECRET_PATTERNS[:-1]:
        out = pat.sub(lambda m: m.group(1) + "<redacted>", out)
    out = _SECRET_PATTERNS[-1].sub("<redacted>", out)
    return out


def _build_plan_summary(messages: list, plan_fertig_text: str = "") -> str:
    """Kompakte Plan-Zusammenfassung fuer Memory: erste User-Anfrage,
    letzte User-Bestaetigung, finaler PLAN_FERTIG-Beschluss. Kein
    voller Verlauf — Embeddings werden bei zu langem Text unscharf."""
    if not messages:
        return _redact_secrets(plan_fertig_text or "")
    user_msgs = [m.get("content", "") for m in messages if m.get("role") == "user"]
    user_msgs = [m for m in user_msgs if m]
    if not user_msgs:
        return _redact_secrets(plan_fertig_text or "")
    parts = [f"Urspruengliche Anfrage: {user_msgs[0][:250]}"]
    if len(user_msgs) > 1 and user_msgs[-1] != user_msgs[0]:
        parts.append(f"Letzte Bestaetigung: {user_msgs[-1][:200]}")
    if plan_fertig_text:
        parts.append(f"Beschlossen: {plan_fertig_text[:300]}")
    return _redact_secrets(" | ".join(parts))


def _write_mission_memory(meta: dict) -> tuple:
    """Schreibt Mission in brain_log.json + ChromaDB user_memory.
    Beide Pfade mit graceful failure — eines kann scheitern ohne den
    anderen zu kippen. Returns (brain_ok: bool, chroma_ok: bool)."""
    brain_ok  = False
    chroma_ok = False

    # 1) brain_log.json — strukturierter Eintrag
    try:
        log_file = os.path.join(MEMORY_DIR, "brain_log.json")
        logs = []
        if os.path.exists(log_file):
            try:
                with open(log_file, "r", encoding="utf-8") as f:
                    logs = json.load(f)
                if not isinstance(logs, list):
                    logs = []
            except Exception:
                logs = []
        entry = {
            "timestamp": datetime.now().isoformat(),
            "event":     "module_installed",
            "data": {
                "command":      meta.get("command", "")[:50],
                "filename":     meta.get("filename", "")[:80],
                "task_id":      meta.get("task_id", "")[:80],
                "code_lines":   int(meta.get("code_lines", 0) or 0),
                "task_summary": _redact_secrets(meta.get("task_summary", ""))[:300],
                "plan_id":      meta.get("plan_id", "")[:60],
            }
        }
        logs.append(entry)
        if len(logs) > 1000:
            logs = logs[-1000:]
        os.makedirs(MEMORY_DIR, exist_ok=True)
        with open(log_file, "w", encoding="utf-8") as f:
            json.dump(logs, f, indent=2, ensure_ascii=False)
        brain_ok = True
    except Exception as e:
        print(f"⚠️ brain_log mission entry failed: {e}")

    # 2) ChromaDB user_memory — semantisch durchsuchbar fuer RICS
    try:
        import chromadb
        from chromadb.utils import embedding_functions
        vec_path = os.path.join(MEMORY_DIR, "vectors")
        client   = chromadb.PersistentClient(path=vec_path)
        embed    = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2"
        )
        coll = client.get_or_create_collection(name="user_memory", embedding_function=embed)

        date_de  = datetime.now().strftime("%d.%m.%Y")
        cmd      = meta.get("command",  "neues_Modul")
        fname    = meta.get("filename", "?")
        lines    = int(meta.get("code_lines", 0) or 0)
        task_sum = _redact_secrets(meta.get("task_summary", ""))[:400]
        plan_sum = _redact_secrets(meta.get("plan_excerpt", ""))[:800]

        text = (
            f"MISSION: Am {date_de} hat RICS das Telegram-Modul /{cmd} gebaut "
            f"(Datei {fname}, {lines} Zeilen Code). "
        )
        if task_sum:
            text += f"Aufgabe: {task_sum} "
        if plan_sum:
            text += f"Plan-Verlauf: {plan_sum}"

        coll.add(
            documents=[text.strip()],
            ids=[f"mission_{int(datetime.now().timestamp() * 1000)}"]
        )
        chroma_ok = True
    except Exception as e:
        print(f"⚠️ chroma mission entry failed: {e}")

    return brain_ok, chroma_ok


def _read_mission_meta(task_id: str) -> dict:
    """Liest Meta-Datei zu einer Mission. Leerer Dict wenn nicht da."""
    path = os.path.join(WORKSPACE, f"auto_{task_id}.meta.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_mission_meta(task_id: str, data: dict):
    """Schreibt/aktualisiert Mission-Meta-Datei. Merge mit existierendem Inhalt."""
    path = os.path.join(WORKSPACE, f"auto_{task_id}.meta.json")
    try:
        existing = _read_mission_meta(task_id)
        existing.update(data)
        os.makedirs(WORKSPACE, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ mission meta write failed: {e}")


class OrchestratorWeb:
    def __init__(self, q, jarvis=None, brain=None):
        self.q = q; self.jarvis = jarvis; self.brain = brain; self.task_id = ""

    def log(self, text, typ="info"):
        self.q.put({"type": typ, "text": text})

    async def _llm(self, messages, use_json=False):
        return await llm_call(messages, use_json, log_fn=self.log)

    async def detect_mode(self, task):
        prompt = (f"Analysiere diese Aufgabe. Antworte NUR mit einem Wort:\n"
                  f"AGENT=Daten recherchieren  BUILDER=Telegram-Modul  HYBRID=recherchieren+Modul  SCRIPT=lokales Skript\n"
                  f"Aufgabe: {task}\nAntworte NUR mit: AGENT, BUILDER, HYBRID oder SCRIPT")
        try:
            r = (await self._llm([{"role": "user", "content": prompt}])).strip().upper()
            if "HYBRID"  in r: return "hybrid"
            if "SCRIPT"  in r: return "script"
            if "BUILDER" in r: return "builder"
            return "agent"
        except Exception:
            tl = task.lower()
            if any(k in tl for k in ["skript","script","lokal","download"]): return "script"
            if any(k in tl for k in ["api","baue ein modul","wetter","abrufen und"]): return "hybrid"
            if any(k in tl for k in ["modul","handler","befehl","telegram"]): return "builder"
            return "agent"

    async def run_code(self, code):
        if "load_dotenv" not in code:
            code = "from dotenv import load_dotenv; load_dotenv()\n" + code
        # Sanity-Check Code-Groesse: >250 Zeilen Recherche-Code ist fast immer
        # ein Token-Limit-/Streaming-Artefakt (abgeschnittener String).
        line_count = len(code.splitlines())
        if line_count > 250:
            return "", f"SYNTAX_ERROR: Code zu lang ({line_count} Zeilen) — vermutlich abgeschnitten"
        # Syntax-Check vorab — spart subprocess-Start bei kaputtem Code
        # und liefert eine eindeutige Fehlermarkierung fuer das Retry-Handling.
        try:
            import ast as _ast
            _ast.parse(code)
        except SyntaxError as se:
            return "", f"SYNTAX_ERROR: {se.msg} (Zeile {se.lineno})"
        try:
            r = await asyncio.get_event_loop().run_in_executor(None, lambda: subprocess.run(
                [sys.executable, "-c", code], capture_output=True, text=True, timeout=30))
            return r.stdout.strip(), r.stderr.strip()
        except subprocess.TimeoutExpired:
            return "", "Timeout (>30s)"
        except Exception as e:
            return "", str(e)

    def validate_module(self, code):
        # Syntax MUSS valide sein — sonst crasht der Bot beim Laden.
        try:
            import ast as _ast
            _ast.parse(code)
        except SyntaxError as se:
            return False, f"Syntax-Fehler: {se.msg} (Zeile {se.lineno}) — Code abgeschnitten?"
        # Code-Groesse: >800 Zeilen ist fuer ein Telegram-Modul fast immer
        # ein Token-Limit-/Streaming-Artefakt.
        line_count = len(code.splitlines())
        if line_count > 800:
            return False, f"Code zu lang ({line_count} Zeilen) — vermutlich abgeschnitten"
        # Mindest-Strukturen
        for kw, err in {"setup(app)": "Keine setup(app)", "CommandHandler": "Kein CommandHandler",
                        "async def": "Keine async-Funktion"}.items():
            if kw not in code: return False, err
        return True, "OK"

    def _research_failed(self, research_result):
        """Prüft ob Recherche-Ergebnis brauchbar ist.
        Returns None wenn OK, sonst einen kurzen Fail-Grund (str).
        Spezial-Codes:
          'AUTH_WALL'    — alle Endpoints haben 401/403 geliefert (Test-Key invalid)
                           Im Retry NICHT andere APIs suchen, sondern Doku lesen.
          'DOKU_SCHEMA'  — Recherche hat Response-Schema aus Doku extrahiert
                           (auch ohne 200-Call gueltig, wenn JSON-Pfade dokumentiert)."""
        if not research_result or not research_result.strip():
            return "leeres Ergebnis"
        rl = research_result.lower()

        # ── HARTE Fail-Signale (auch bei vorhandenen Erfolgen): explizit aufgegeben ──
        for sig in ("result: keine_api", "result: kein endpoint",
                    "result: kein funktionierend", "keine_api_verfuegbar"):
            if sig in rl:
                return f"Fail-Signal '{sig.strip()}'"

        # ── Code-Crash hat absoluten Vorrang ──
        if "syntax_error" in rl or "kein output" in rl[:30]:
            return "kein Output (Code-Crash)"

        # ── DOKU-SCHEMA als Erfolg akzeptieren ──
        # Wenn die Recherche aus offizieller Doku ein Response-Schema extrahiert hat
        # (Endpoint existiert, JSON-Pfade aus Doku bekannt), ist das brauchbar —
        # auch ohne tatsaechlichen 200-Call mit echtem Key.
        if "doku_schema" in rl or "response-schema:" in rl or "response schema:" in rl:
            return None

        # ── POSITIV-Erkennung: gibt es Hinweise auf einen funktionierenden Endpoint? ──
        positive_signals = (
            "status 200",
            "funktioniert",
            "ok für",
            "ok fuer",
            "result: http",
            "result: https",
        )
        has_positive = any(p in rl for p in positive_signals)

        has_json_sample = bool(
            re.search(r'\{\s*\n[^{}]*"\w+"\s*:', research_result)
            or re.search(r'"id"\s*:\s*"', research_result)
        )

        has_200_token = bool(re.search(r"\b200\b", research_result))
        http_errs     = set(re.findall(r"\b(4\d{2}|5\d{2})\b", research_result))

        if (has_positive or has_200_token) and has_json_sample:
            return None

        # ── AUTH-WALL erkennen: Recherche stuft API als auth-protected ein ──
        # Symptom: 401 und/oder 403 dominant, KEIN 200 irgendwo, KEIN JSON-Sample.
        # Das passiert weil der Recherche-Code mit Dummy-Key testet — die API
        # selbst funktioniert, der Key ist nur fuer Recherche nicht gueltig.
        # In diesem Fall NICHT andere APIs suchen, sondern Doku lesen.
        auth_codes = http_errs & {"401", "403"}
        if auth_codes and not (has_positive or has_200_token) and not has_json_sample:
            return f"AUTH_WALL ({','.join(sorted(auth_codes))} dominant — Doku lesen statt Alternative suchen)"

        if http_errs and not (has_positive or has_200_token):
            return f"HTTP {','.join(sorted(http_errs))} ohne Erfolg"
        for s in ("service unavailable", "internal server error",
                  "connection refused", "name or service not known"):
            if s in rl and not (has_positive or has_200_token):
                return f"Fehler-Signal '{s}'"

        for sig in ("result: fehler", "result: failed", "result: error", "fehlgeschlagen:"):
            if sig in rl and not (has_positive or has_json_sample):
                return f"Fail-Signal '{sig.strip()}'"

        if "result:" not in rl:
            return "kein RESULT: im Output"

        return None

    async def execute_mission(self, task):
        original_task = task
        self.task_id  = task_id_safe(task)

        # ── Schritt 1: Modus erkennen ──────────────────────
        self.log("🔎 <b>Schritt 1</b> — Modus-Erkennung...", "step")
        mode = await self.detect_mode(task)

        labels     = {"builder":"BUILDER","agent":"AGENT","hybrid":"HYBRID","script":"SCRIPT"}
        emoji      = {"builder":"🔨","agent":"🔍","hybrid":"🔀","script":"📝"}
        total_s    = {"builder":3,"agent":3,"hybrid":4,"script":3}

        self.log(
            f"🚀 <b>MISSION START</b>\n"
            f"📋 {html.escape(task)}\n"
            f"⚡ {emoji.get(mode,'?')} <b>{labels.get(mode,mode.upper())}</b>\n"
            f"📊 {total_s.get(mode,3)} Schritte geplant\n"
            f"🧠 {'orchestrator.py ✅' if _ORCH_IMPORTED else 'Fallback ⚠️'}", "start")

        if mode == "hybrid":
            self.log("🔀 <b>Schritt 2/4</b> — API recherchieren...", "step")
            research_messages = [
                {"role":"system","content":AGENT_SYSTEM},
                {"role":"user","content":(
                    f"Recherchiere die API fuer folgende Aufgabe:\n{task}\n\n"
                    f"WICHTIG — was die Recherche tut und was nicht:\n"
                    f"- Du testest nur OB die API funktioniert und WIE die Response aussieht.\n"
                    f"- Du baust NICHT den User-Task nach (keine echten User-Werte testen).\n"
                    f"- Verwende GENERISCHE Test-Werte: einfache Beispielnamen, '1', heutiges Datum.\n"
                    f"- Nutze NIEMALS spezifische Werte aus der Aufgabenbeschreibung als Test-Parameter\n"
                    f"  (Eigennamen, Orte, Begriffe aus dem Task gehoeren in den Builder, nicht in die Recherche).\n\n"
                    f"WORKFLOW (PFLICHT):\n"
                    f"1. Identifiziere den Haupt-Endpoint fuer das Ziel.\n"
                    f"2. Erkennt der Endpoint nur IDs/Codes (nicht Klartext-Namen)?\n"
                    f"   → Dann braucht es einen LOOKUP-Endpoint davor (z.B. /search, /locations,\n"
                    f"     /find, /lookup, /resolve). BEIDE testen.\n"
                    f"3. Teste mit generischen Werten. Bei 500/400: pruefe ob Parameter-Format stimmt\n"
                    f"   (IDs statt Namen? ISO-Datum statt Unix-Timestamp? POST statt GET?).\n"
                    f"4. Drucke fuer JEDEN funktionierenden Endpoint eine echte Response-Probe:\n"
                    f"   first = data[0] if isinstance(data, list) and data else data\n"
                    f"   print(json.dumps(first, indent=2, ensure_ascii=False)[:2000])\n"
                    f"   Bei verschachtelten Listen (z.B. journeys[0].legs[0]) auch das innere\n"
                    f"   Element pretty-printen — der Builder muss Feld-Typen sehen.\n"
                    f"5. Im RESULT NENNEN:\n"
                    f"   - Den/die funktionierenden Endpoint(s) mit 200\n"
                    f"   - Das Workflow-Pattern wenn mehrstufig (z.B. 'erst /locations fuer ID,\n"
                    f"     dann /journeys mit IDs')\n\n"
                    f"CODE-LIMITS:\n"
                    f"- Maximal 80 Zeilen. Knapp halten.\n"
                    f"- Strings IN EINER ZEILE oder mit ''' ''' Triple-Quotes — NIE mit Backslash-Continuation.\n"
                    f"- Lange URLs nur mit params={{}} bauen, nicht als f-String.\n\n"
                    f"FAIL-AUSGABE (nur wenn wirklich keine API geht):\n"
                    f"- print('RESULT: KEINE_API_VERFUEGBAR <kurz warum>')\n"
                    f"- NICHT 'Kein funktionierender Endpoint' wenn du nur ein 500 mit falschen Params bekommen hast —\n"
                    f"  in dem Fall pruefe Doku oder anderen Workflow.\n\n"
                    f"Ausgabe: print('RESULT:', ...)\nNur reiner Python-Code, keine Backticks."
                )}
            ]

            research_result   = ""
            research_ok       = False
            raw               = ""
            MAX_RES_ATTEMPTS  = 3

            for r_attempt in range(1, MAX_RES_ATTEMPTS + 1):
                try:
                    self.log(f"🤖 LLM generiert Recherche-Code (Versuch {r_attempt}/{MAX_RES_ATTEMPTS})...", "info")
                    raw = await self._llm(research_messages)
                    code = extract_code(raw)
                    self.log(f"✅ Code empfangen ({len(code.splitlines())} Zeilen) — führe aus...", "info")
                    stdout, stderr = await self.run_code(code)
                    research_result = stdout if stdout else f"Kein Output. Stderr: {stderr[:300]}"
                    self.log(f"🔍 Recherche-Ergebnis (Versuch {r_attempt}):\n<pre>{html.escape(research_result[:1200])}</pre>", "result")
                except Exception as e:
                    research_result = f"Fehlgeschlagen: {e}"
                    self.log(f"⚠️ Versuch {r_attempt}: {research_result}", "warn")

                fail_reason = self._research_failed(research_result)
                if not fail_reason:
                    research_ok = True
                    break

                self.log(f"⚠️ Recherche unbrauchbar ({fail_reason})", "warn")
                if r_attempt < MAX_RES_ATTEMPTS:
                    # Syntax-/Code-Crash: kaputten Code NICHT in History haengen,
                    # sonst lernt das LLM aus seinem eigenen Fehler.
                    is_code_crash = (
                        "SYNTAX_ERROR" in research_result
                        or "SyntaxError" in research_result
                        or "unterminated string" in research_result.lower()
                        or "was never closed" in research_result.lower()
                        or "invalid syntax" in research_result.lower()
                        or "indentationerror" in research_result.lower()
                    )
                    if is_code_crash:
                        self.log("🔁 Code war kaputt → neu generieren (frischer Kontext)...", "info")
                        # History bleibt unveraendert, nur ein zusaetzlicher Hint:
                        research_messages = research_messages[:1] + [
                            {"role":"user","content":(
                                f"Wichtig: Vorheriger Code hatte einen Python-Syntaxfehler "
                                f"(z.B. unterminated string, langer URL ueber mehrere Zeilen).\n"
                                f"Halte diesmal Strings IN EINER Zeile oder nutze Triple-Quotes/"
                                f"Klammer-Concatenation. Halte URLs kurz und nutze "
                                f"params={{}} statt langer f-Strings.\n\n"
                                f"Recherchiere fuer: {original_task}\n"
                                f"Ausgabe: print('RESULT:',...)\nNur reiner Python-Code, keine Backticks."
                            )}
                        ]
                    elif "AUTH_WALL" in fail_reason:
                        # Auth-Wall = Endpoints existieren, aber Test-Key ist ungueltig.
                        # Statt blind andere APIs suchen (das fuehrt zu falschen
                        # Endpoints wie 'featuredcategories' bei Steam) → Doku lesen.
                        self.log("🔁 Auth-Wall erkannt → Doku-Read statt Alternative...", "info")
                        research_messages += [
                            {"role":"assistant","content": raw or ""},
                            {"role":"user","content":(
                                f"Die getesteten Endpoints liefern 401/403. Das ist NORMAL —\n"
                                f"dein Recherche-Code testet mit einem Dummy-Key, der echte\n"
                                f"User-Key kommt erst im Builder zum Einsatz. Die Endpoints\n"
                                f"selbst EXISTIEREN. Also NICHT auf andere APIs ausweichen!\n\n"
                                f"PFLICHT-VORGEHEN — offizielle Doku lesen:\n"
                                f"1. Bestimme die Doku-URL des Anbieters. Typische Muster:\n"
                                f"   - https://<api>/docs, https://developer.<service>.com,\n"
                                f"     https://partner.<service>.com/doc, /api-docs, /reference\n"
                                f"   - Bei bekannten APIs (Steam, Spotify, GitHub, Twitch, ...)\n"
                                f"     gibt es eine offizielle Web-Dev-Doku.\n"
                                f"2. Fetche die Doku mit httpx/requests (timeout=10).\n"
                                f"3. Parse den HTML/Markdown-Text — suche nach:\n"
                                f"   - Beispiel-Response-JSON (oft in <code>/<pre>-Bloecken)\n"
                                f"   - Feldlisten / Tabellen mit Feldnamen + Typen\n"
                                f"4. Extrahiere fuer JEDEN benoetigten Endpoint die\n"
                                f"   EXAKTE Response-Struktur — vor allem die Wrapper-Pfade!\n"
                                f"   z.B. 'response.players[]' vs 'friendslist.friends[]'\n"
                                f"   vs direkter Listen-Return. APIs sind hier oft inkonsistent\n"
                                f"   (selbst INNERHALB derselben API).\n\n"
                                f"AUSGABE-FORMAT (PFLICHT, EXAKT):\n"
                                f"   print('RESULT: DOKU_SCHEMA fuer <api-name>')\n"
                                f"   print('Endpoint: <url>')\n"
                                f"   print('Response-Schema: <python-dict-pfad zum array/object>')\n"
                                f"   print('Beispiel-Felder: <feld1>, <feld2>, ...')\n"
                                f"   (mehrfach fuer mehrere Endpoints)\n\n"
                                f"Code-Limits: max 80 Zeilen. Strings in einer Zeile.\n"
                                f"Nur reiner Python-Code, keine Backticks."
                            )}
                        ]
                    else:
                        self.log("🔁 Suche Alternative oder lese Doku...", "info")
                        research_messages += [
                            {"role":"assistant","content": raw or ""},
                            {"role":"user","content":(
                                f"Letzte Recherche schlug fehl: {fail_reason}\n"
                                f"Letztes Output (gekuerzt):\n{research_result[:600]}\n\n"
                                f"Mach jetzt eines davon:\n"
                                f"1. Anderen Host/Version testen (z.B. v5 -> v6, prod-, .net, .com).\n"
                                f"2. Doku-URL fetchen (httpx) und nach funktionierendem Endpoint parsen.\n"
                                f"3. Komplett andere API fuer das gleiche Ziel finden.\n"
                                f"PFLICHT: Nur einen Endpoint als RESULT der TATSAECHLICH HTTP 200 lieferte.\n"
                                f"Nur reiner Python-Code, keine Backticks."
                            )}
                        ]

            if not research_ok:
                self.log(
                    f"❌ <b>MISSION ABGEBROCHEN</b>\n"
                    f"Recherche fand nach {MAX_RES_ATTEMPTS} Versuchen keine funktionierende API.\n"
                    f"Letztes Ergebnis:\n<pre>{html.escape(str(research_result)[:500])}</pre>\n\n"
                    f"Kein Modul gebaut — sonst waere es mit kaputter API erstellt worden.\n"
                    f"Bitte spaeter erneut oder Task mit konkretem Endpoint praezisieren.",
                    "error"
                )
                return

            self.log("🔀 <b>Schritt 3/4</b> — Modul generieren...", "step")
            task = f"{original_task}\n\nRECHERCHE-ERGEBNIS:\n{research_result}"
            mode = "builder"

        if mode == "builder":
            step_n = "3/4" if original_task != task else "2/3"
            self.log(f"🔨 <b>Schritt {step_n}</b> — Telegram-Modul generieren...", "step")
            catalog  = get_module_catalog()
            if catalog:
                self.log(f"📦 Modul-Katalog geladen ({len(catalog.splitlines())} Einträge)", "info")
            had_research = (original_task != task)
            research_block = ""
            if had_research:
                research_block = (
                    f"\n\nWICHTIG zur RECHERCHE oben:\n"
                    f"- Die RECHERCHE-ERGEBNIS-Sektion ist die SINGLE SOURCE OF TRUTH fuer technische Details.\n"
                    f"- Nutze GENAU den Endpoint/Host/Pfad aus dem RESULT — NICHT den Wortlaut aus dem Task.\n"
                    f"  Wenn der Task 'foo.com API' sagt aber RESULT 'https://api.v3.foo.com/items' nennt:\n"
                    f"  API_BASE = 'https://api.v3.foo.com' (NICHT 'https://foo.com').\n"
                    f"- Wenn die Recherche zwei Endpoints zeigt (Lookup + Haupt-Endpoint): BEIDE implementieren.\n"
                    f"  User-Eingabe ist Klartext → erst Lookup-Call (Endpoint aus RESULT verwenden,\n"
                    f"  typisch sind /search, /find, /lookup, /resolve, /query) →\n"
                    f"  ID/Code aus Response extrahieren → dann Haupt-Endpoint mit dieser ID/Code aufrufen.\n"
                    f"- Wenn die Recherche-Probe zeigt dass ein Feld ein String ist (kein dict): NIE .get() darauf.\n"
                    f"  Wenn die Recherche keinen Feld-Typ zeigt: defensiv mit isinstance() arbeiten.\n"
                )
            messages = [{"role":"system","content":BUILDER_SYSTEM},
                        {"role":"user","content":
                         f"Erstelle ein vollstaendiges Telegram-Bot-Modul:\n\n{task}\n\n"
                         f"- Reiner Python-Code, keine Backticks\n- Vollstaendig implementiert\n"
                         f"- setup(app) mit allen CommandHandlern\n- Deutsche Ausgaben\n"
                         f"- chat_id IMMER os.getenv('CHAT_ID')"
                         f"{research_block}\n\n{catalog}"}]
            for attempt in range(1, 3):
                try:
                    self.log(f"🤖 LLM generiert Code (Versuch {attempt}/2)...", "info")
                    raw  = await self._llm(messages)
                    code = extract_code(raw)
                    lines = len(code.splitlines())
                    self.log(f"✅ Code empfangen — {lines} Zeilen", "info")

                    self.log("🔍 Validierung läuft...", "info")
                    valid, reason = self.validate_module(code)
                    if not valid and attempt < 2:
                        self.log(f"⚠️ Versuch {attempt}: {reason} → Nachbessern...", "warn")
                        messages += [{"role":"assistant","content":raw},
                                     {"role":"user","content":f"Fehler: {reason}. Korrigiere. Nur Python."}]
                        continue

                    if "TELEGRAM_CHAT_ID" in code and attempt < 2:
                        self.log("🚫 TELEGRAM_CHAT_ID gefunden → Korrigiere...", "warn")
                        messages += [{"role":"assistant","content":raw},
                                     {"role":"user","content":"KRITISCH: muss CHAT_ID heissen, nicht TELEGRAM_CHAT_ID!"}]
                        continue

                    self.log("✅ Validierung bestanden", "info")
                    step_last = "4/4" if original_task != task else "3/3"
                    self.log(f"💾 <b>Schritt {step_last}</b> — Speichere in workspace/...", "step")
                    ws = os.path.join(WORKSPACE, f"auto_{self.task_id}.py")
                    with open(ws, "w", encoding="utf-8") as f: f.write(code)
                    cmd_m = re.search(r'CommandHandler\(["\'](\w+)["\']', code)
                    cmd_name = cmd_m.group(1) if cmd_m else ""
                    fname = f"{cmd_name}.py" if cmd_m else f"auto_{self.task_id}.py"
                    prev  = "\n".join(code.splitlines()[:25])
                    # Mission-Meta mit Build-Ergebnis fuettern — wird beim Install
                    # in brain_log + ChromaDB geschrieben.
                    _write_mission_meta(self.task_id, {
                        "command":    cmd_name,
                        "filename":   fname,
                        "code_lines": lines,
                        "built_at":   datetime.now().isoformat(),
                    })
                    self.log(f"✅ <b>MODUL FERTIG</b> — {lines} Zeilen → <code>{fname}</code>\n\n"
                             f"<pre>{html.escape(prev)}...</pre>", "success")
                    self.q.put({"type":"action","mode":"install","task_id":self.task_id,"filename":fname})
                    return
                except Exception as e:
                    self.log(f"❌ Versuch {attempt}: {html.escape(str(e))}", "error")
            self.log("❌ Modul-Generierung fehlgeschlagen.", "error")

        elif mode == "script":
            self.log("📝 <b>Schritt 2/3</b> — Skript generieren...", "step")
            messages = [{"role":"system","content":SCRIPT_SYSTEM},
                        {"role":"user","content":f"Erstelle Skript fuer:\n\n{task}\n\nNur reiner Python-Code, mit den 3 Header-Zeilen am Anfang."}]
            for attempt in range(1, 3):
                try:
                    self.log(f"🤖 LLM generiert Skript (Versuch {attempt}/2)...", "info")
                    raw  = await self._llm(messages)
                    code = extract_code(raw)
                    lines = len(code.splitlines())
                    self.log(f"✅ Code empfangen — {lines} Zeilen", "info")

                    # ── Metadaten aus Header-Zeilen parsen ──────────
                    fn_m   = re.search(r"^\s*#\s*FILENAME:\s*([\w\-]+\.py)\s*$",            code, re.MULTILINE)
                    req_m  = re.search(r"^\s*#\s*REQUIREMENTS:\s*(.+?)\s*$",                code, re.MULTILINE)
                    run_m  = re.search(r"^\s*#\s*RUNNABLE:\s*(gui|terminal|button)\s*$",   code, re.MULTILINE)

                    pretty_name = fn_m.group(1) if fn_m else None
                    raw_reqs    = req_m.group(1).strip() if req_m else "keine"
                    runnable    = (run_m.group(1) if run_m else "button").lower()

                    # Stdlib-Filter — falls LLM trotzdem Stdlib reinschreibt
                    STDLIB = {"os","sys","re","json","subprocess","tkinter","sqlite3",
                              "hashlib","datetime","time","random","math","pathlib",
                              "shutil","glob","collections","itertools","functools",
                              "typing","dataclasses","getpass","csv","html","urllib"}
                    if raw_reqs.lower() in ("keine","none","-",""):
                        deps = []
                    else:
                        deps = [p.strip() for p in raw_reqs.split(",")
                                if p.strip() and p.strip().lower() not in STDLIB]

                    self.log("💾 <b>Schritt 3/3</b> — Speichere in workspace/...", "step")

                    # Primär-Speicherort (Run-Button-Pfad bleibt stabil)
                    ws_canonical = os.path.join(WORKSPACE, f"script_{self.task_id}.py")
                    with open(ws_canonical, "w", encoding="utf-8") as f: f.write(code)

                    # Sprechende Kopie
                    pretty_path = None
                    if pretty_name:
                        try:
                            pretty_path = os.path.join(WORKSPACE, pretty_name)
                            with open(pretty_path, "w", encoding="utf-8") as f: f.write(code)
                        except Exception as _e:
                            self.log(f"⚠️ Kopie '{pretty_name}' fehlgeschlagen: {_e}", "warn")
                            pretty_path = None

                    # ── Wrapper bauen je nach RUNNABLE-Typ ──────────
                    cmd_path     = None
                    wrapper_kind = None  # "app" | "command" | None

                    if runnable == "gui":
                        # Self-Contained .app-Bundle — kopierfähig auf jedem Mac
                        # Skript wandert ins Bundle, System-Python statt venv
                        try:
                            target_py  = pretty_path or ws_canonical
                            base       = os.path.splitext(os.path.basename(target_py))[0]
                            app_bundle = os.path.join(WORKSPACE, f"{base}.app")
                            macos_dir  = os.path.join(app_bundle, "Contents", "MacOS")
                            res_dir    = os.path.join(app_bundle, "Contents", "Resources")
                            os.makedirs(macos_dir, exist_ok=True)
                            os.makedirs(res_dir,   exist_ok=True)

                            # Skript ins Bundle reinkopieren — App ist self-contained
                            bundled_py = os.path.join(res_dir, "main.py")
                            shutil.copy2(target_py, bundled_py)

                            bundle_id = "local.rics." + re.sub(r"[^a-z0-9]", "", base.lower())
                            plist = (
                                '<?xml version="1.0" encoding="UTF-8"?>\n'
                                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                                '<plist version="1.0"><dict>\n'
                                '  <key>CFBundleExecutable</key><string>launcher</string>\n'
                               f'  <key>CFBundleIdentifier</key><string>{bundle_id}</string>\n'
                               f'  <key>CFBundleName</key><string>{base}</string>\n'
                                '  <key>CFBundlePackageType</key><string>APPL</string>\n'
                                '  <key>CFBundleVersion</key><string>1.0</string>\n'
                                '  <key>NSHighResolutionCapable</key><true/>\n'
                                '</dict></plist>\n'
                            )
                            with open(os.path.join(app_bundle, "Contents", "Info.plist"),
                                      "w", encoding="utf-8") as f:
                                f.write(plist)

                            # Launcher mit bundle-relativem Pfad und System-Python-Suche
                            launcher = os.path.join(macos_dir, "launcher")
                            with open(launcher, "w", encoding="utf-8") as f:
                                f.write('#!/bin/bash\n')
                                f.write('# Bundle-relativer Pfad — App bleibt portabel\n')
                                f.write('DIR="$(cd "$(dirname "$0")/../Resources" && pwd)"\n')
                                f.write('# Python-Suche: Homebrew → /usr/local → System\n')
                                f.write('if [ -x /opt/homebrew/bin/python3 ]; then\n')
                                f.write('  PY=/opt/homebrew/bin/python3\n')
                                f.write('elif [ -x /usr/local/bin/python3 ]; then\n')
                                f.write('  PY=/usr/local/bin/python3\n')
                                f.write('else\n')
                                f.write('  PY=/usr/bin/python3\n')
                                f.write('fi\n')
                                f.write('exec "$PY" "$DIR/main.py"\n')
                            os.chmod(launcher, 0o755)

                            cmd_path     = app_bundle
                            wrapper_kind = "app"
                        except Exception as _e:
                            self.log(f"⚠️ .app-Bundle fehlgeschlagen: {_e}", "warn")
                            cmd_path = None

                    elif runnable == "terminal":
                        # Terminal-interaktives Skript → .command-Wrapper
                        try:
                            target_py = pretty_path or ws_canonical
                            base      = os.path.splitext(os.path.basename(target_py))[0]
                            cmd_path  = os.path.join(WORKSPACE, f"{base}.command")
                            py_exec   = sys.executable
                            with open(cmd_path, "w", encoding="utf-8") as f:
                                f.write("#!/bin/bash\n")
                                f.write(f'cd "{os.path.dirname(target_py)}"\n')
                                f.write(f'"{py_exec}" "{target_py}"\n')
                                f.write('echo ""\necho "[Fenster mit beliebiger Taste schliessen]"\nread -n 1\n')
                            os.chmod(cmd_path, 0o755)
                            wrapper_kind = "command"
                        except Exception as _e:
                            self.log(f"⚠️ .command-Wrapper fehlgeschlagen: {_e}", "warn")
                            cmd_path = None

                    # runnable == "button" → kein Wrapper, nur Run-Button im Web-Panel

                    # ── Mission-Bericht ────────────────────────────
                    display = pretty_name or f"script_{self.task_id}.py"
                    prev    = "\n".join(code.splitlines()[:25])

                    report = [f"✅ <b>SKRIPT FERTIG</b> — {lines} Zeilen → <code>{html.escape(display)}</code>"]
                    report.append("")
                    if deps:
                        pip_cmd = "python3 -m pip install " + " ".join(deps)
                        report.append("📦 <b>Bibliotheken installieren:</b>")
                        report.append(f"<pre>{html.escape(pip_cmd)}</pre>")
                    else:
                        report.append("📦 <b>Bibliotheken:</b> keine (nur Standardbibliothek)")
                    report.append("")
                    if wrapper_kind == "app":
                        report.append("🖱️ <b>Doppelklick-App:</b>")
                        report.append(f"<code>{html.escape(cmd_path)}</code>")
                        report.append("→ Doppelklick im Finder öffnet das GUI-Fenster direkt (kein Terminal).")
                        report.append("→ App ist self-contained und auch kopierbar (Desktop, USB, anderer Mac).")
                    elif wrapper_kind == "command":
                        report.append("🖱️ <b>Doppelklick-Starter:</b>")
                        report.append(f"<code>{html.escape(cmd_path)}</code>")
                        report.append("→ Doppelklick öffnet ein Terminal-Fenster mit dem Skript.")
                    else:
                        report.append("▶️ <b>Starten:</b> Button unten oder per Terminal:")
                        report.append(f"<pre>python3 {html.escape(pretty_path or ws_canonical)}</pre>")
                    report.append("")
                    report.append(f"<pre>{html.escape(prev)}...</pre>")

                    self.log("\n".join(report), "success")
                    self.q.put({"type":"action","mode":"run_script","task_id":self.task_id})
                    return
                except Exception as e:
                    self.log(f"❌ Versuch {attempt}: {html.escape(str(e))}", "error")
            self.log("❌ Skript-Generierung fehlgeschlagen.", "error")


def _run_mission_thread(log_q, task, jarvis, brain):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        orch = OrchestratorWeb(log_q, jarvis, brain)
        loop.run_until_complete(orch.execute_mission(task))
    except Exception as e:
        log_q.put({"type":"error","text":f"❌ Kritischer Fehler: {html.escape(str(e))}"})
    finally:
        log_q.put({"type":"done","text":"Mission beendet."})
        loop.close()


def _backup_file(path):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = os.path.join(BACKUP_DIR, f"{os.path.basename(path)}.{ts}.bak")
    try:
        shutil.copy2(path, backup)
        return backup
    except Exception as e:
        return f"FEHLER:{e}"


def _make_diff(original, modified, filename):
    diff = list(difflib.unified_diff(
        original.splitlines(keepends=True), modified.splitlines(keepends=True),
        fromfile=f"vorher/{filename}", tofile=f"nachher/{filename}", n=3))
    return "".join(diff[:200]) if diff else "(Keine Aenderungen)"


def _is_safe_brain_path(filename):
    name = os.path.basename(filename)
    if not name or name.startswith("."): return False, "Ungueltiger Dateiname."
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED_EXT: return False, f"Endung '{ext}' nicht erlaubt."
    full = os.path.join(BRAIN_DIR, name)
    if not os.path.realpath(full).startswith(os.path.realpath(BRAIN_DIR)):
        return False, "Sicherheitsfehler."
    return True, full


def _build_file_tree():
    """Dateibaum für den Explorer."""
    def _scan(path, ext_filter=None, max_depth=1, _d=0):
        if not os.path.isdir(path):
            return []
        entries = []
        try:
            names = sorted(os.listdir(path))
        except Exception:
            return []
        for name in names:
            if name.startswith(".") or name == "__pycache__":
                continue
            full = os.path.join(path, name)
            if os.path.isdir(full) and _d < max_depth:
                children = _scan(full, ext_filter, max_depth, _d+1)
                if children:
                    entries.append({"name": name, "type": "dir", "path": full, "children": children})
            elif os.path.isfile(full):
                if ext_filter is None or any(name.endswith(e) for e in ext_filter):
                    entries.append({"name": name, "type": "file", "path": full})
        return entries

    tree = []

    # 1) memory/brain/ — prominent oben
    brain_files = _scan(BRAIN_DIR, list(ALLOWED_EXT))
    tree.append({"name": "🧠 memory/brain/", "type": "dir", "path": BRAIN_DIR,
                 "children": brain_files, "brain": True})

    # 2) Alle .py im Projektroot (= eine Ebene über modules/)
    root_py = []
    try:
        for name in sorted(os.listdir(PROJECT_DIR)):
            full = os.path.join(PROJECT_DIR, name)
            if name.endswith(".py") and not name.startswith("_") and os.path.isfile(full):
                root_py.append({"name": name, "type": "file", "path": full})
    except Exception:
        pass
    if root_py:
        tree.append({"name": "📦 modules (root)", "type": "dir",
                     "path": PROJECT_DIR, "children": root_py})

    # 3) modules/ Unterordner
    mod_files = _scan(BASE_DIR, [".py"])   # BASE_DIR = modules/
    if mod_files:
        tree.append({"name": "📂 modules/", "type": "dir",
                     "path": BASE_DIR, "children": mod_files})

    # 4) workspace/ — eigene Logik: .py + .command + .app-Bundles als Files
    ws_entries = []
    try:
        for name in sorted(os.listdir(WORKSPACE)):
            if name.startswith(".") or name == "__pycache__":
                continue
            full = os.path.join(WORKSPACE, name)
            # .app-Bundle: ist Verzeichnis aber als File-Eintrag behandeln
            if name.endswith(".app") and os.path.isdir(full):
                ws_entries.append({"name": name, "type": "file", "path": full, "is_app": True})
            elif os.path.isfile(full) and (
                name.endswith(".py") or name.endswith(".command")
            ):
                ws_entries.append({"name": name, "type": "file", "path": full})
    except Exception:
        pass
    tree.append({"name": "⚗️ workspace/", "type": "dir",
                 "path": WORKSPACE, "children": ws_entries, "workspace": True})

    # 5) memory/ (ohne brain/)
    mem_files = []
    try:
        for name in sorted(os.listdir(MEMORY_DIR)):
            if name == "brain":
                continue
            full = os.path.join(MEMORY_DIR, name)
            if os.path.isfile(full) and any(name.endswith(e) for e in ALLOWED_EXT):
                mem_files.append({"name": name, "type": "file", "path": full})
    except Exception:
        pass
    tree.append({"name": "💾 memory/", "type": "dir",
                 "path": MEMORY_DIR, "children": mem_files})

    # 6) logs/
    log_files = _scan(LOGS_DIR, [".log", ".json"], max_depth=2)
    tree.append({"name": "📋 logs/", "type": "dir",
                 "path": LOGS_DIR, "children": log_files})

    return tree


def _check_auth():
    pin = os.getenv("WEB_PIN", "")
    if not pin: return True
    return session.get("authenticated") is True or session.get("auth") is True


@orch_blueprint.route("/lab")
def lab_page():
    if not _check_auth(): return redirect("/")
    try:
        import base64 as _b64
        _raw = json.dumps(_build_file_tree(), ensure_ascii=True)
        tree_json = _b64.b64encode(_raw.encode()).decode()
    except Exception as e:
        tree_json = "[]"
    return _build_lab_html(tree_json)


@orch_blueprint.route("/lab/debug")
def lab_debug():
    """Zeigt Pfade und ob Verzeichnisse existieren."""
    return jsonify({
        "BASE_DIR":    BASE_DIR,
        "PROJECT_DIR": PROJECT_DIR,
        "BRAIN_DIR":   BRAIN_DIR,
        "MODULES_DIR": MODULES_DIR,
        "WORKSPACE":   WORKSPACE,
        "MEMORY_DIR":  MEMORY_DIR,
        "base_exists":    os.path.isdir(BASE_DIR),
        "project_exists": os.path.isdir(PROJECT_DIR),
        "brain_exists":   os.path.isdir(BRAIN_DIR),
        "memory_exists":  os.path.isdir(MEMORY_DIR),
        "tree_count":     len(_build_file_tree()),
    })

@orch_blueprint.route("/lab/files")
def lab_files():
    pass  # auth via page
    return jsonify(_build_file_tree())

@orch_blueprint.route("/lab/file")
def lab_file():
    path = request.args.get("path","")
    if not path: return jsonify({"error":"no path"}), 400
    try:
        real = os.path.realpath(path)
        if not real.startswith(os.path.realpath(PROJECT_DIR)):
            return jsonify({"error":"access denied"}), 403
        with open(real,"r",encoding="utf-8",errors="replace") as f:
            content = f.read(120000)
        return jsonify({"content":content,"name":os.path.basename(real),
                        "brain":real.startswith(os.path.realpath(BRAIN_DIR))})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@orch_blueprint.route("/lab/run", methods=["POST"])
def lab_run():
    data = request.get_json(silent=True) or {}
    task = (data.get("task") or "").strip()
    plan_id = (data.get("plan_id") or "").strip()
    if not task: return jsonify({"error":"Keine Aufgabe"}), 400

    # Original Task (vor Plan-Anhang) — landet spaeter in Memory.
    original_task = task

    # Mission-ID aus dem urspruenglichen Task-Kern (vor Plan-Anhang).
    mid = task_id_safe(task[:100] + str(datetime.now().timestamp()))

    # Plan-Summary fuer spaeteren Memory-Eintrag (kompakt, kein Volltext).
    plan_summary = ""
    plan_fertig_text = ""
    pf_match = re.search(r"PLAN_FERTIG:\s*(.+?)(?:\n|$)", original_task, re.IGNORECASE)
    if pf_match:
        plan_fertig_text = pf_match.group(1).strip()

    # Plan-Verlauf als Kontext an den Task anhaengen, falls vorhanden.
    # So bekommt der Builder ALLE konkreten Werte (API-Keys, IDs, URLs,
    # Layout-Beispiele, Workflow-Details) aus dem Plan-Chat mit.
    if plan_id:
        with _plan_lock:
            session_msgs = list(_plan_sessions.get(plan_id, []))
        if session_msgs:
            plan_summary = _build_plan_summary(session_msgs, plan_fertig_text)
            transcript_lines = []
            for m in session_msgs:
                role = "User" if m.get("role") == "user" else "Plan-Assistent"
                content = (m.get("content") or "").strip()
                content = re.sub(r"PLAN_FERTIG:.*?(?:\n|$)", "", content,
                                 flags=re.IGNORECASE).strip()
                if content:
                    transcript_lines.append(f"{role}: {content}")
            if transcript_lines:
                transcript = "\n\n".join(transcript_lines)
                if len(transcript) > 12000:
                    transcript = transcript[-12000:]
                task = (
                    f"{task}\n\n"
                    f"=== VOLLSTAENDIGER PLAN-VERLAUF ===\n"
                    f"Im folgenden Chat wurden ALLE konkreten Details abgestimmt:\n"
                    f"API-Keys, IDs, Steam-IDs, URLs, Beispiel-Layouts (ASCII-Boxen,\n"
                    f"Rahmen, Symbole), Workflow-Schritte, Endpoint-Pfade.\n"
                    f"Verwende diese Werte und Layouts GENAU SO im Code.\n"
                    f"KEINE Platzhalter wie 'YOUR_API_KEY' / 'YOUR_STEAM64_ID' —\n"
                    f"wenn ein Wert im Verlauf steht, gehoert er hartcodiert in den Code.\n"
                    f"Achte auf JSON-Pfade die in der Recherche/im Verlauf genannt sind\n"
                    f"(z.B. 'friendslist.friends' nicht 'friends').\n\n"
                    f"{transcript}\n"
                    f"=== ENDE PLAN-VERLAUF ==="
                )

    # Mission-Meta vorab schreiben — wird beim Erfolg von execute_mission
    # ergaenzt (filename, command, code_lines) und beim Install in Memory geschrieben.
    _write_mission_meta(mid, {
        "task_id":      mid,
        "task_summary": original_task[:500],
        "plan_id":      plan_id,
        "plan_excerpt": plan_summary,
        "created_at":   datetime.now().isoformat(),
    })

    log_q = queue.Queue(maxsize=500)
    with _missions_lock: _active_missions[mid] = log_q
    try:
        from web_app import jarvis_instance, brain_instance
        jarvis = jarvis_instance; brain = brain_instance
    except Exception:
        jarvis = brain = None
    threading.Thread(target=_run_mission_thread,
                     args=(log_q,task,jarvis,brain),daemon=True).start()
    return jsonify({"mission_id":mid})

@orch_blueprint.route("/lab/stream/<mission_id>")
def lab_stream(mission_id):
    with _missions_lock: log_q = _active_missions.get(mission_id)
    if not log_q:
        return Response('data:{"error":"not found"}\n\n', mimetype="text/event-stream")
    def generate():
        try:
            while True:
                try:
                    msg = log_q.get(timeout=120)
                    yield "data: " + json.dumps(msg, ensure_ascii=False) + "\n\n"
                    if msg.get("type") == "done": break
                except queue.Empty:
                    yield 'data: {"type":"ping"}\n\n'
        finally:
            with _missions_lock: _active_missions.pop(mission_id, None)
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

@orch_blueprint.route("/lab/install", methods=["POST"])
def lab_install():
    data    = request.get_json(silent=True) or {}
    task_id = data.get("task_id","")
    fname   = data.get("filename",f"auto_{task_id}.py")
    ws_path = os.path.join(WORKSPACE, f"auto_{task_id}.py")
    if not os.path.exists(ws_path): return jsonify({"error":"Datei nicht gefunden"}), 404
    try:
        shutil.copy2(ws_path, os.path.join(MODULES_DIR, fname))

        # Mission-Memory schreiben — RICS soll im Nachhinein wissen, was er
        # wann gebaut hat (nicht nur strukturell ueber funktions_scan).
        # Failures hier blockieren den Install nicht.
        memory_note = ""
        meta = _read_mission_meta(task_id)
        if meta:
            # Falls Filename nicht in Meta steht (Edge-Case), aus Request nehmen.
            if not meta.get("filename"):
                meta["filename"] = fname
            if not meta.get("command") and fname.endswith(".py"):
                meta["command"] = fname[:-3]
            brain_ok, chroma_ok = _write_mission_memory(meta)
            tags = []
            if brain_ok:  tags.append("brain_log")
            if chroma_ok: tags.append("ChromaDB")
            if tags:
                memory_note = f" • RICS-Memory: {' + '.join(tags)} ✅"
            # Meta-Datei aufraeumen — Job erledigt.
            try:
                os.remove(os.path.join(WORKSPACE, f"auto_{task_id}.meta.json"))
            except Exception:
                pass

        return jsonify({
            "ok": True,
            "message": f"✅ Installiert als modules/{fname} — Neustart erforderlich!{memory_note}"
        })
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@orch_blueprint.route("/lab/run-script", methods=["POST"])
def lab_run_script():
    data    = request.get_json(silent=True) or {}
    task_id = data.get("task_id","")
    path    = os.path.join(WORKSPACE, f"script_{task_id}.py")
    if not os.path.exists(path):
        return jsonify({"error":"Skript nicht gefunden"}), 404

    # Auto-Detect: braucht das Skript User-Input?
    try:
        with open(path, "r", encoding="utf-8") as f:
            code = f.read()
        is_interactive = bool(re.search(r"\binput\s*\(", code))
    except Exception:
        is_interactive = False

    try:
        # Interaktive Skripte → echtes Terminal-Fenster (macOS)
        if is_interactive and sys.platform == "darwin":
            py_exec  = sys.executable.replace('"', '\\"')
            sh_path  = path.replace('"', '\\"')
            osa_cmd  = f'tell app "Terminal" to do script "{py_exec} \\"{sh_path}\\""'
            subprocess.Popen(["osascript", "-e", osa_cmd])
            return jsonify({
                "ok": True,
                "output": f"▶️ Skript läuft im Terminal-Fenster (interaktiv erkannt — input() gefunden)\n📁 {path}"
            })

        # Nicht-interaktive Skripte → wie gehabt mit Capture
        r = subprocess.run([sys.executable, path],
                           capture_output=True, text=True, timeout=60)
        output = r.stdout.strip() or r.stderr.strip() or "Kein Output."
        return jsonify({"ok": True, "output": output[:10000]})
    except subprocess.TimeoutExpired:
        return jsonify({"error":"Timeout (>60s)"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@orch_blueprint.route("/lab/trash", methods=["POST"])
def lab_trash():
    """Verschiebt eine Datei oder ein .app-Bundle in den macOS-Papierkorb.
    Erlaubt nur Pfade unter BRAIN_DIR oder WORKSPACE."""
    data = request.get_json(silent=True) or {}
    path = data.get("path", "")
    if not path:
        return jsonify({"error": "Kein Pfad"}), 400

    real = os.path.realpath(path)
    allowed_roots = [os.path.realpath(BRAIN_DIR), os.path.realpath(WORKSPACE)]
    if not any(real == r or real.startswith(r + os.sep) for r in allowed_roots):
        return jsonify({"error": "Pfad nicht erlaubt — nur brain/ oder workspace/"}), 403

    if not os.path.exists(real):
        return jsonify({"error": "Datei existiert nicht"}), 404

    # macOS: Finder verschiebt nativ in den Papierkorb (reversibel)
    if sys.platform == "darwin":
        try:
            posix = real.replace('"', '\\"')
            osa = f'tell application "Finder" to delete (POSIX file "{posix}" as alias)'
            r = subprocess.run(["osascript", "-e", osa],
                               capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                return jsonify({"error": f"Papierkorb fehlgeschlagen: {r.stderr.strip()}"}), 500
            return jsonify({"ok": True, "name": os.path.basename(real)})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # Nicht-macOS Fallback: hard delete (kein systemweiter Papierkorb verfügbar)
    try:
        if os.path.isdir(real):
            shutil.rmtree(real)
        else:
            os.remove(real)
        return jsonify({"ok": True, "name": os.path.basename(real)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@orch_blueprint.route("/lab/plan", methods=["POST"])
def lab_plan():
    """Streaming Plan-Chat."""
    if not _check_auth():
        return Response('data:{"error":"unauthorized"}\n\n', mimetype="text/event-stream")
    data     = request.get_json(silent=True) or {}
    plan_id  = data.get("plan_id", "default")
    user_msg = (data.get("message") or "").strip()
    if not user_msg:
        return jsonify({"error": "Keine Nachricht"}), 400

    _sse_headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

    # ── Modell-Auswahl: Zustand bestimmen ──────────────────────────────
    with _plan_lock:
        is_new_session = plan_id not in _plan_sessions
        if is_new_session:
            _plan_sessions[plan_id] = []
            _plan_models[plan_id]   = None  # Noch nicht gewählt
        current_model = _plan_models.get(plan_id)

    # ── PHASE 1: Neue Session → Modell-Frage stellen (kein LLM-Call) ──
    if is_new_session:
        with _plan_lock:
            _plan_sessions[plan_id].append({"role": "user", "content": user_msg})

        selection_msg = (
            "Welches Modell soll ich für diese Plan-Session verwenden?\n\n"
            "⚡  1 · Flash  —  DeepSeek V3  (schnell & effizient)\n"
            "🧠  2 · Thinking  —  DeepSeek R1  (tiefes Nachdenken, etwas langsamer)\n\n"
            "Einfach `1` oder `2` — oder `flash` / `thinking` eingeben."
        )
        _append_chatlog("user",      user_msg,      source="lab_plan")
        _append_chatlog("assistant", selection_msg, source="lab_plan")

        def _gen_selection():
            with _plan_lock:
                _plan_sessions[plan_id].append({"role": "assistant", "content": selection_msg})
            yield "data: " + json.dumps({"token": selection_msg}, ensure_ascii=False) + "\n\n"
            yield 'data: {"done":true}\n\n'

        return Response(_gen_selection(), mimetype="text/event-stream", headers=_sse_headers)

    # ── PHASE 2: Antwort auf Modell-Frage → Wahl setzen ───────────────
    if current_model is None:
        msg_lower = user_msg.lower().strip()
        if any(k in msg_lower for k in ["1", "flash", "v3", "chat", "schnell", "effizient"]):
            chosen_model  = "deepseek-chat"
            model_display = "DeepSeek V3 Flash ⚡"
        else:
            chosen_model  = "deepseek-reasoner"
            model_display = "DeepSeek R1 Thinking 🧠"

        with _plan_lock:
            _plan_models[plan_id] = chosen_model
            _plan_sessions[plan_id].append({"role": "user", "content": user_msg})

        confirm_msg = (
            f"✅ Alles klar — ich nutze **{model_display}** für diese Session.\n\n"
            f"Was soll gebaut werden? Telegram-Modul, lokales Skript, GUI-App oder Recherche?"
        )
        _append_chatlog("user",      user_msg,    source="lab_plan")
        _append_chatlog("assistant", confirm_msg, source="lab_plan")

        def _gen_confirm():
            with _plan_lock:
                _plan_sessions[plan_id].append({"role": "assistant", "content": confirm_msg})
            yield "data: " + json.dumps({"token": confirm_msg}, ensure_ascii=False) + "\n\n"
            yield 'data: {"done":true}\n\n'

        return Response(_gen_confirm(), mimetype="text/event-stream", headers=_sse_headers)

    # ── PHASE 3: Normaler Plan-Chat mit gewähltem Modell ──────────────
    plan_model = _plan_models.get(plan_id, DS_MODEL)

    with _plan_lock:
        _plan_sessions[plan_id].append({"role": "user", "content": user_msg})
        messages = [{"role": "system", "content": PLAN_SYSTEM}] + list(_plan_sessions[plan_id][-20:])

    # Chatlog: User-Nachricht aus dem Plan-Chat in logs/chatlog.json
    # damit RICS sich an die Plan-Konversation erinnert.
    _append_chatlog("user", user_msg, source="lab_plan")

    tok_q   = queue.Queue(maxsize=2000)
    done_ev = threading.Event()
    full_buf = []

    def _streamer():
        loop2 = asyncio.new_event_loop()
        asyncio.set_event_loop(loop2)
        async def _run():
            ds_key   = os.getenv("DEEPSEEK_API_KEY","")
            groq_key = os.getenv("GROQ_API_KEY","")
            done = False
            if ds_key and not done:
                try:
                    async with httpx.AsyncClient(timeout=120) as c:
                        async with c.stream("POST", DS_URL,
                            headers={"Authorization":f"Bearer {ds_key}","Content-Type":"application/json"},
                            json={"model": plan_model, "messages": messages, "stream": True,
                                  "max_tokens":2048,"temperature":0.7}) as r:
                            if r.status_code == 200:
                                async for line in r.aiter_lines():
                                    if line.startswith("data: "):
                                        raw2 = line[6:].strip()
                                        if raw2 and raw2 != "[DONE]":
                                            try:
                                                tok = json.loads(raw2).get("choices",[{}])[0].get("delta",{}).get("content","")
                                                if tok:
                                                    full_buf.append(tok)
                                                    tok_q.put(tok)
                                            except Exception:
                                                pass
                                done = True
                except Exception:
                    pass
            if groq_key and not done:
                try:
                    async with httpx.AsyncClient(timeout=90) as c:
                        async with c.stream("POST","https://api.groq.com/openai/v1/chat/completions",
                            headers={"Authorization":f"Bearer {groq_key}","Content-Type":"application/json"},
                            json={"model":"llama-3.3-70b-versatile","messages":messages,
                                  "stream":True,"max_tokens":2048,"temperature":0.7}) as r:
                            if r.status_code == 200:
                                async for line in r.aiter_lines():
                                    if line.startswith("data: "):
                                        raw2 = line[6:].strip()
                                        if raw2 and raw2 != "[DONE]":
                                            try:
                                                tok = json.loads(raw2).get("choices",[{}])[0].get("delta",{}).get("content","")
                                                if tok:
                                                    full_buf.append(tok)
                                                    tok_q.put(tok)
                                            except Exception:
                                                pass
                                done = True
                except Exception:
                    pass
            if not done:
                try:
                    import ollama as _ol
                    model = os.getenv("OLLAMA_MODEL","qwen3:8b")
                    res   = await loop2.run_in_executor(None, lambda: _ol.chat(model=model, messages=messages))
                    tok   = re.sub(r"<think>.*?</think>","", res["message"]["content"].strip(), flags=re.DOTALL).strip()
                    full_buf.append(tok)
                    tok_q.put(tok)
                except Exception as e:
                    tok_q.put(f"Fehler: {e}")
        loop2.run_until_complete(_run())
        loop2.close()
        done_ev.set()

    threading.Thread(target=_streamer, daemon=True).start()

    def generate():
        while not done_ev.is_set() or not tok_q.empty():
            try:
                tok = tok_q.get(timeout=0.05)
                yield "data: " + json.dumps({"token": tok}, ensure_ascii=False) + "\n\n"
            except queue.Empty:
                continue
        full_text = "".join(full_buf)
        with _plan_lock:
            if plan_id in _plan_sessions:
                _plan_sessions[plan_id].append({"role":"assistant","content":full_text})
        _append_chatlog("assistant", full_text, source="lab_plan")
        plan_m = re.search(r"PLAN_FERTIG:\s*(.+?)(?:\n|$)", full_text, re.IGNORECASE)
        if plan_m:
            yield "data: " + json.dumps({"plan_ready": plan_m.group(1).strip()}, ensure_ascii=False) + "\n\n"
        yield 'data: {"done":true}\n\n'

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


@orch_blueprint.route("/lab/plan/reset", methods=["POST"])
def lab_plan_reset():
    data    = request.get_json(silent=True) or {}
    plan_id = data.get("plan_id","default")
    with _plan_lock:
        _plan_sessions.pop(plan_id, None)
        _plan_models.pop(plan_id, None)
    return jsonify({"ok":True})

@orch_blueprint.route("/lab/brain/save", methods=["POST"])
def lab_brain_save():
    data     = request.get_json(silent=True) or {}
    filename = data.get("filename","")
    content  = data.get("content","")
    ok, result = _is_safe_brain_path(filename)
    if not ok: return jsonify({"error":result}), 400
    if filename.endswith(".json"):
        try: json.loads(content)
        except json.JSONDecodeError as e: return jsonify({"error":f"Ungueltiges JSON: {e}"}), 400
    backup = _backup_file(result) if os.path.exists(result) else None
    try:
        with open(result,"w",encoding="utf-8") as f: f.write(content)
        return jsonify({"ok":True,
                        "backup":os.path.basename(backup) if backup and not str(backup).startswith("FEHLER") else None})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@orch_blueprint.route("/lab/brain/ki-edit", methods=["POST"])
def lab_brain_ki_edit():
    data       = request.get_json(silent=True) or {}
    filename   = data.get("filename","")
    change_req = (data.get("change_req") or "").strip()
    original   = data.get("content","")
    ok, filepath = _is_safe_brain_path(filename)
    if not ok: return jsonify({"error":filepath}), 400
    if not change_req: return jsonify({"error":"Keine Aenderungsbeschreibung"}), 400
    if not original:
        try:
            with open(filepath,"r",encoding="utf-8") as f: original = f.read()
        except FileNotFoundError: original = ""
    user_prompt = (f"DATEI: memory/brain/{filename}\n"
                   f"GEWUENSCHTE AENDERUNG: {change_req}\n\n"
                   f"--- DATEIINHALT ---\n{original}\n\n"
                   f"Gib jetzt den VOLLSTAENDIGEN geaenderten Dateiinhalt zurueck. Kein Markdown.")
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            r = loop.run_until_complete(llm_call([
                {"role":"system","content":EDITOR_SYSTEM},
                {"role":"user",  "content":user_prompt}
            ]))
            r = re.sub(r"```\w*\s*","",r)
            r = re.sub(r"```","",r).strip()
            return r, None
        except Exception as e:
            return None, str(e)
        finally:
            loop.close()
    modified, err = _run()
    if err: return jsonify({"error":err}), 500
    if filename.endswith(".json"):
        try: json.loads(modified)
        except json.JSONDecodeError as e:
            return jsonify({"error":f"KI hat ungueltiges JSON erzeugt: {e}","content":modified}), 422
    diff    = _make_diff(original, modified, filename)
    changed = sum(1 for l in diff.splitlines()
                  if l.startswith(("+","-")) and not l.startswith(("+++","---")))
    return jsonify({"ok":True,"original":original,"modified":modified,
                    "diff":diff,"changed":changed})

@orch_blueprint.route("/lab/brain/apply", methods=["POST"])
def lab_brain_apply():
    data     = request.get_json(silent=True) or {}
    filename = data.get("filename","")
    content  = data.get("content","")
    ok, filepath = _is_safe_brain_path(filename)
    if not ok: return jsonify({"error":filepath}), 400
    if filename.endswith(".json"):
        try: json.loads(content)
        except json.JSONDecodeError as e: return jsonify({"error":f"Ungueltiges JSON: {e}"}), 400
    backup = _backup_file(filepath) if os.path.exists(filepath) else None
    try:
        with open(filepath,"w",encoding="utf-8") as f: f.write(content)
        return jsonify({"ok":True,
                        "backup":os.path.basename(backup) if backup and not str(backup).startswith("FEHLER") else None})
    except Exception as e:
        return jsonify({"error":str(e)}), 500


# ────────────────────────────────────────────────────────────────
#  KI-EDIT CHAT — wie Plan-Chat aber pro Datei in memory/brain/.
#  User chattet mit der KI ueber gewuenschte Aenderungen, klar
#  formulierte Bestaetigungen ("uebernimm", "ja mach", "passt")
#  triggern eine Antwort mit Marker "FILE_READY:" gefolgt vom
#  kompletten neuen Dateiinhalt — Frontend zeigt dann den Diff.
#  Apply selber laeuft weiter ueber /lab/brain/apply (unveraendert).
# ────────────────────────────────────────────────────────────────

@orch_blueprint.route("/lab/brain/ki-chat", methods=["POST"])
def lab_brain_ki_chat():
    """Streaming KI-Edit-Chat fuer eine Datei in memory/brain/."""
    if not _check_auth():
        return Response('data:{"error":"unauthorized"}\n\n', mimetype="text/event-stream")
    data         = request.get_json(silent=True) or {}
    session_id   = (data.get("session_id") or "").strip()
    filename     = (data.get("filename")   or "").strip()
    user_msg     = (data.get("message")    or "").strip()
    live_content = data.get("content")  # darf None sein

    if not session_id or not filename or not user_msg:
        return jsonify({"error":"session_id, filename und message noetig"}), 400

    ok, filepath = _is_safe_brain_path(filename)
    if not ok:
        return jsonify({"error": filepath}), 400

    _sse_headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

    # Aktuellen Dateiinhalt bestimmen
    if isinstance(live_content, str):
        current_content = live_content
    else:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                current_content = f.read()
        except FileNotFoundError:
            current_content = ""
        except Exception as e:
            return jsonify({"error": f"Datei lesen: {e}"}), 500

    # Session anlegen / fortschreiben — bei Datei-Wechsel auch Modell zurücksetzen
    with _kiedit_lock:
        sess = _kiedit_sessions.get(session_id)
        file_changed = not sess or sess.get("filename") != filename
        if file_changed:
            sess = {"filename": filename, "messages": []}
            _kiedit_sessions[session_id] = sess
            _kiedit_models[session_id]   = None  # Modell neu wählen
        current_model = _kiedit_models.get(session_id)
        is_new = file_changed

    # ── PHASE 1: Neue/gewechselte Session → Modell-Frage stellen ──────
    if is_new:
        with _kiedit_lock:
            sess["messages"].append({"role": "user", "content": user_msg})

        selection_msg = (
            f"Welches Modell soll ich für das Bearbeiten von **{filename}** verwenden?\n\n"
            "⚡  1 · Flash  —  DeepSeek V3  (schnell & effizient)\n"
            "🧠  2 · Thinking  —  DeepSeek R1  (tiefes Nachdenken, etwas langsamer)\n\n"
            "Einfach `1` oder `2` — oder `flash` / `thinking` eingeben."
        )
        _append_chatlog("user",      f"[{filename}] {user_msg}", source="lab_kiedit")
        _append_chatlog("assistant", selection_msg,               source="lab_kiedit")

        def _gen_selection():
            with _kiedit_lock:
                s = _kiedit_sessions.get(session_id)
                if s:
                    s["messages"].append({"role": "assistant", "content": selection_msg})
            yield "data: " + json.dumps({"token": selection_msg}, ensure_ascii=False) + "\n\n"
            yield 'data: {"done":true}\n\n'

        return Response(_gen_selection(), mimetype="text/event-stream", headers=_sse_headers)

    # ── PHASE 2: Antwort auf Modell-Frage → Wahl setzen ───────────────
    if current_model is None:
        msg_lower = user_msg.lower().strip()
        if any(k in msg_lower for k in ["1", "flash", "v3", "chat", "schnell", "effizient"]):
            chosen_model  = "deepseek-chat"
            model_display = "DeepSeek V3 Flash ⚡"
        else:
            chosen_model  = "deepseek-reasoner"
            model_display = "DeepSeek R1 Thinking 🧠"

        with _kiedit_lock:
            _kiedit_models[session_id] = chosen_model
            s = _kiedit_sessions.get(session_id)
            if s:
                s["messages"].append({"role": "user", "content": user_msg})

        confirm_msg = (
            f"✅ Verstanden — ich nutze **{model_display}** für diese Session.\n\n"
            f"Was soll an **{filename}** geändert werden?"
        )
        _append_chatlog("user",      f"[{filename}] {user_msg}", source="lab_kiedit")
        _append_chatlog("assistant", confirm_msg,                 source="lab_kiedit")

        def _gen_confirm():
            with _kiedit_lock:
                s = _kiedit_sessions.get(session_id)
                if s:
                    s["messages"].append({"role": "assistant", "content": confirm_msg})
            yield "data: " + json.dumps({"token": confirm_msg}, ensure_ascii=False) + "\n\n"
            yield 'data: {"done":true}\n\n'

        return Response(_gen_confirm(), mimetype="text/event-stream", headers=_sse_headers)

    # ── PHASE 3: Normaler KI-Edit-Chat mit gewähltem Modell ───────────
    ki_model = _kiedit_models.get(session_id, DS_MODEL)

    with _kiedit_lock:
        s = _kiedit_sessions.get(session_id)
        if s:
            s["messages"].append({"role": "user", "content": user_msg})
            history = list(s["messages"][-20:])
        else:
            history = [{"role": "user", "content": user_msg}]

    sys_prompt = KIEDIT_CHAT_SYSTEM.format(filename=filename)
    sys_prompt += (
        f"\n\n--- AKTUELLER DATEIINHALT ({filename}) ---\n"
        f"{current_content}\n"
        f"--- ENDE DATEIINHALT ---"
    )
    messages = [{"role": "system", "content": sys_prompt}] + history

    _append_chatlog("user", f"[{filename}] {user_msg}", source="lab_kiedit")

    tok_q    = queue.Queue(maxsize=2000)
    done_ev  = threading.Event()
    full_buf = []

    def _streamer():
        loop2 = asyncio.new_event_loop()
        asyncio.set_event_loop(loop2)
        async def _run():
            ds_key   = os.getenv("DEEPSEEK_API_KEY", "")
            groq_key = os.getenv("GROQ_API_KEY", "")
            done = False
            if ds_key and not done:
                try:
                    async with httpx.AsyncClient(timeout=180) as c:
                        async with c.stream("POST", DS_URL,
                            headers={"Authorization": f"Bearer {ds_key}",
                                     "Content-Type": "application/json"},
                            json={"model": ki_model, "messages": messages,
                                  "stream": True, "max_tokens": 32000,
                                  "temperature": 0.3}) as r:
                            if r.status_code == 200:
                                async for line in r.aiter_lines():
                                    if line.startswith("data: "):
                                        raw2 = line[6:].strip()
                                        if raw2 and raw2 != "[DONE]":
                                            try:
                                                tok = json.loads(raw2).get("choices", [{}])[0].get("delta", {}).get("content", "")
                                                if tok:
                                                    full_buf.append(tok)
                                                    tok_q.put(tok)
                                            except Exception:
                                                pass
                                done = True
                except Exception:
                    pass
            if groq_key and not done:
                try:
                    async with httpx.AsyncClient(timeout=120) as c:
                        async with c.stream("POST", "https://api.groq.com/openai/v1/chat/completions",
                            headers={"Authorization": f"Bearer {groq_key}",
                                     "Content-Type": "application/json"},
                            json={"model": "llama-3.3-70b-versatile", "messages": messages,
                                  "stream": True, "max_tokens": 32000,
                                  "temperature": 0.3}) as r:
                            if r.status_code == 200:
                                async for line in r.aiter_lines():
                                    if line.startswith("data: "):
                                        raw2 = line[6:].strip()
                                        if raw2 and raw2 != "[DONE]":
                                            try:
                                                tok = json.loads(raw2).get("choices", [{}])[0].get("delta", {}).get("content", "")
                                                if tok:
                                                    full_buf.append(tok)
                                                    tok_q.put(tok)
                                            except Exception:
                                                pass
                                done = True
                except Exception:
                    pass
            if not done:
                try:
                    import ollama as _ol
                    model = os.getenv("OLLAMA_MODEL", "qwen3:8b")
                    res   = await loop2.run_in_executor(None, lambda: _ol.chat(model=model, messages=messages))
                    tok   = re.sub(r"<think>.*?</think>", "", res["message"]["content"].strip(), flags=re.DOTALL).strip()
                    full_buf.append(tok)
                    tok_q.put(tok)
                except Exception as e:
                    tok_q.put(f"Fehler: {e}")
        loop2.run_until_complete(_run())
        loop2.close()
        done_ev.set()

    threading.Thread(target=_streamer, daemon=True).start()

    def generate():
        # Sobald wir "FILE_READY:" im akkumulierten Output erkennen,
        # schalten wir in "swallow"-Mode: der nachfolgende Dateiinhalt
        # wird NICHT als sichtbarer Chat-Token gesendet, sondern am
        # Ende als file_ready-Event mit Diff geschickt.
        accum       = ""
        marker_seen = False
        sent_chars  = 0
        # Tail-Buffer: wir streamen nicht die letzten N Zeichen,
        # damit ein gerade reinkommender "FILE_READY:" Marker nicht
        # versehentlich als sichtbare Tokens leakt. "FILE_READY:" hat
        # 12 Zeichen — 14 ist sicheres Polster.
        TAIL_BUFFER = 14

        while not done_ev.is_set() or not tok_q.empty():
            try:
                tok = tok_q.get(timeout=0.05)
            except queue.Empty:
                continue
            if not tok:
                continue
            accum += tok

            if marker_seen:
                # Im swallow-Mode: nichts streamen, nur akkumulieren.
                continue

            m = re.search(r"FILE_READY\s*:", accum, re.IGNORECASE)
            if m:
                visible_until = m.start()
                if visible_until > sent_chars:
                    chunk = accum[sent_chars:visible_until]
                    if chunk:
                        yield "data: " + json.dumps({"token": chunk}, ensure_ascii=False) + "\n\n"
                sent_chars = len(accum)
                marker_seen = True
                yield "data: " + json.dumps({"file_ready_pending": True}, ensure_ascii=False) + "\n\n"
                continue

            # Kein Marker bisher — den neuen Tail rausstreamen,
            # aber TAIL_BUFFER zurueckhalten (Marker-Schutz).
            if len(accum) - sent_chars > TAIL_BUFFER:
                safe_until = len(accum) - TAIL_BUFFER
                chunk = accum[sent_chars:safe_until]
                if chunk:
                    yield "data: " + json.dumps({"token": chunk}, ensure_ascii=False) + "\n\n"
                    sent_chars = safe_until

        # Stream zu Ende. Falls Marker NICHT kam → restlichen Text noch ausspielen.
        if not marker_seen and len(accum) > sent_chars:
            chunk = accum[sent_chars:]
            if chunk:
                yield "data: " + json.dumps({"token": chunk}, ensure_ascii=False) + "\n\n"
                sent_chars = len(accum)

        full_text = accum

        # Was geht in die Session-History? Bei FILE_READY-Antworten
        # NICHT den Volltext der Datei mitschleppen — das wuerde:
        #   a) den Kontext schnell sprengen
        #   b) das LLM dazu verleiten in der naechsten Runde wieder
        #      vorschnell FILE_READY zu produzieren (Pattern-Mimicry)
        # Stattdessen: nur sichtbarer Teil + kompakte Notiz.
        if marker_seen:
            visible_part = re.split(r"FILE_READY\s*:", full_text,
                                    maxsplit=1, flags=re.IGNORECASE)[0].strip()
            history_msg = visible_part
            if history_msg:
                history_msg += "\n\n"
            history_msg += "[FILE_READY ausgegeben — Diff wurde dem User gezeigt.]"
        else:
            history_msg = full_text

        # Session aktualisieren
        with _kiedit_lock:
            s = _kiedit_sessions.get(session_id)
            if s:
                s["messages"].append({"role": "assistant", "content": history_msg})

        # Chatlog: bei FILE_READY nur sichtbaren Teil + Hinweis, NICHT
        # den Volltext der Datei (wuerde Embeddings unbrauchbar machen).
        if marker_seen:
            chatlog_msg  = visible_part + ("\n\n" if visible_part else "")
            chatlog_msg += f"[FILE_READY fuer {filename} vorbereitet — Diff zur Pruefung]"
            _append_chatlog("assistant", chatlog_msg, source="lab_kiedit")
        else:
            _append_chatlog("assistant", full_text, source="lab_kiedit")

        # FILE_READY-Verarbeitung
        if marker_seen:
            after = re.split(r"FILE_READY\s*:", full_text, maxsplit=1, flags=re.IGNORECASE)[1]
            after = after.lstrip("\n\r")
            after = re.sub(r"^```[a-zA-Z]*\s*\n?", "", after)
            after = re.sub(r"\n?```\s*$", "", after)
            new_content = after

            # JSON-Validierung
            json_error = None
            if filename.endswith(".json"):
                try:
                    json.loads(new_content)
                except json.JSONDecodeError as je:
                    json_error = f"Ungueltiges JSON: {je}"

            if json_error:
                yield "data: " + json.dumps({
                    "file_ready_error": json_error
                }, ensure_ascii=False) + "\n\n"
            else:
                diff = _make_diff(current_content, new_content, filename)
                changed = sum(1 for l in diff.splitlines()
                              if l.startswith(("+", "-")) and not l.startswith(("+++", "---")))
                yield "data: " + json.dumps({
                    "file_ready": True,
                    "filename":   filename,
                    "original":   current_content,
                    "modified":   new_content,
                    "diff":       diff,
                    "changed":    changed,
                }, ensure_ascii=False) + "\n\n"

        yield 'data: {"done":true}\n\n'

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@orch_blueprint.route("/lab/brain/ki-chat/reset", methods=["POST"])
def lab_brain_ki_chat_reset():
    data       = request.get_json(silent=True) or {}
    session_id = (data.get("session_id") or "").strip()
    if not session_id:
        return jsonify({"error": "session_id noetig"}), 400
    with _kiedit_lock:
        _kiedit_sessions.pop(session_id, None)
        _kiedit_models.pop(session_id, None)
    return jsonify({"ok": True})


@orch_blueprint.route("/lab/brain/new", methods=["POST"])
def lab_brain_new():
    data     = request.get_json(silent=True) or {}
    filename = data.get("filename","").strip()
    content  = data.get("content","")
    ok, filepath = _is_safe_brain_path(filename)
    if not ok: return jsonify({"error":filepath}), 400
    if os.path.exists(filepath): return jsonify({"error":"Datei existiert bereits"}), 409
    try:
        with open(filepath,"w",encoding="utf-8") as f: f.write(content)
        return jsonify({"ok":True,"path":filepath})
    except Exception as e:
        return jsonify({"error":str(e)}), 500




def setup(app):
    pass


def _build_lab_html(tree_b64="W10="):
    from flask import render_template_string
    import html as _h

    bot_name = os.getenv("BOT_NAME", "RICS")
    orch_ok  = _ORCH_IMPORTED
    safe_bot = _h.escape(bot_name or "RICS")
    ws_path  = WORKSPACE

    template = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ bot }} Lab</title>
<style>
:root{--c:#00d4ff;--c2:#00ff88;--cy:#f59e0b;--purple:#a78bfa;
      --bg:#020617;--bg2:#0f172a;--bg3:#1e293b;--border:#1e3a4a;
      --text:#e2e8f0;--sub:#64748b;--red:#ff6b6b}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden;background:var(--bg);color:var(--text);
          font-family:'Segoe UI',Arial,sans-serif;font-size:15px}

/* Layout */
.app{display:flex;flex-direction:column;height:100vh}

/* Header */
.hdr{display:flex;align-items:center;gap:12px;padding:0 20px;height:58px;
     background:var(--bg2);border-bottom:1px solid var(--border);flex-shrink:0}
.logo{display:flex;align-items:center;gap:10px}
.logo-icon{width:34px;height:34px;background:linear-gradient(135deg,var(--c),#7c3aed);
           border-radius:8px;display:flex;align-items:center;justify-content:center;
           font-weight:900;color:#000;font-size:.95rem}
.logo-name{font-size:1.05rem;font-weight:700;color:var(--c);letter-spacing:2px}
.logo-sub{font-size:.6rem;color:var(--sub);letter-spacing:1px}
.nav{display:flex;gap:6px;margin-left:20px}
.nbtn{padding:.38rem 1rem;background:transparent;border:1px solid transparent;
      border-radius:8px;color:var(--sub);cursor:pointer;font-size:.78rem;font-weight:700;
      letter-spacing:.5px;text-transform:uppercase;font-family:inherit;transition:all .2s}
.nbtn:hover{color:var(--text);border-color:var(--border)}
.nbtn.act-y{color:var(--cy);border-color:rgba(245,158,11,.5);background:rgba(245,158,11,.1)}
.nbtn.act-c{color:var(--c);border-color:rgba(0,212,255,.5);background:rgba(0,212,255,.08)}
.nbtn.act-p{color:var(--purple);border-color:rgba(167,139,250,.5);background:rgba(167,139,250,.08)}
.hright{margin-left:auto;display:flex;gap:10px;align-items:center}
.ost{font-size:.65rem;padding:.18rem .55rem;border-radius:10px;border:1px solid;font-weight:700}
.ost.ok{color:var(--c2);border-color:rgba(0,255,136,.3);background:rgba(0,255,136,.08)}
.ost.warn{color:var(--cy);border-color:rgba(245,158,11,.3);background:rgba(245,158,11,.08)}
.back{padding:.38rem 1rem;background:rgba(255,107,107,.1);border:1px solid rgba(255,107,107,.3);
      color:var(--red);border-radius:8px;text-decoration:none;font-size:.78rem;font-weight:700}
.back:hover{background:rgba(255,107,107,.2)}

/* Modebar */
.modebar{display:flex;align-items:center;gap:10px;padding:0 20px;height:32px;flex-shrink:0;
         background:var(--bg3);border-bottom:1px solid var(--border);font-size:.72rem}
.mpill{display:flex;align-items:center;gap:6px;padding:.12rem .55rem;border-radius:10px;
       border:1px solid;font-weight:700;font-size:.65rem;letter-spacing:.5px}
.mpill.plan{color:var(--cy);border-color:rgba(245,158,11,.4);background:rgba(245,158,11,.07)}
.mpill.mission{color:var(--c);border-color:rgba(0,212,255,.4);background:rgba(0,212,255,.07)}
.mpill.building{color:#fb923c;border-color:rgba(251,146,60,.4);background:rgba(251,146,60,.08);
                animation:pulse 1.5s ease-in-out infinite}
.mpill.brain{color:var(--purple);border-color:rgba(167,139,250,.4);background:rgba(167,139,250,.07)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}
.mdot{width:6px;height:6px;border-radius:50%;background:currentColor;flex-shrink:0}
.mstep{margin-left:auto;color:#fb923c;font-size:.65rem;font-weight:700}

/* Body */
.body{display:flex;flex:1;overflow:hidden}

/* Sidebar */
.sidebar{width:260px;flex-shrink:0;background:var(--bg2);border-right:1px solid var(--border);
         display:flex;flex-direction:column;overflow:hidden}
.sbhdr{padding:10px 14px;font-size:.65rem;font-weight:700;color:var(--sub);letter-spacing:1.5px;
       text-transform:uppercase;border-bottom:1px solid var(--border);
       display:flex;justify-content:space-between;align-items:center}
.sbhdr .rld{cursor:pointer;color:var(--c);font-size:.9rem;opacity:.7}
.sbhdr .rld:hover{opacity:1}
.tree{flex:1;overflow-y:auto;padding:6px 0}
.tree::-webkit-scrollbar{width:3px}
.tree::-webkit-scrollbar-thumb{background:rgba(0,212,255,.2);border-radius:2px}
.tdir{padding:6px 14px;font-size:.72rem;font-weight:700;color:var(--c);
      letter-spacing:.5px;text-transform:uppercase;border-bottom:1px solid var(--border);
      margin-top:4px}
.tdir.brain{color:var(--purple)}
.tfile{display:flex;align-items:center;gap:8px;padding:7px 14px 7px 22px;
       cursor:pointer;font-size:.75rem;color:var(--sub);transition:all .15s;
       border-left:2px solid transparent}
.tfile:hover{color:var(--text);background:rgba(0,212,255,.05);border-left-color:var(--c)}
.tfile.act{color:var(--c);background:rgba(0,212,255,.08);border-left-color:var(--c)}
.tfile.brain-f:hover{color:var(--purple);background:rgba(167,139,250,.07);border-left-color:var(--purple)}
.tfile.brain-f.act{color:var(--purple);background:rgba(167,139,250,.1);border-left-color:var(--purple)}
.tfile-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tfile-trash{opacity:0;flex-shrink:0;padding:2px 6px;border-radius:4px;font-size:.75rem;
             color:var(--red);transition:opacity .15s,background .15s;margin-right:4px}
.tfile:hover .tfile-trash{opacity:.7}
.tfile-trash:hover{opacity:1 !important;background:rgba(255,107,107,.15)}

/* Workspace */
.workspace{display:flex;flex-direction:column;flex:1;overflow:hidden}
.panels{display:flex;flex:1;overflow:hidden}
.panel{display:flex;flex-direction:column;flex:1;overflow:hidden;
       border-right:1px solid var(--border)}
.panel:last-child{border-right:none}
.phdr{display:flex;align-items:center;gap:8px;padding:8px 16px;
      background:var(--bg2);border-bottom:1px solid var(--border);
      font-size:.72rem;color:var(--sub);font-weight:700;flex-shrink:0;
      letter-spacing:.5px;text-transform:uppercase}
.pdot{width:8px;height:8px;border-radius:50%;background:var(--c);flex-shrink:0}
.pdot.y{background:var(--cy)}.pdot.p{background:var(--purple)}.pdot.g{background:var(--c2)}
.pbody{flex:1;overflow-y:auto;overflow-x:hidden}
.pbody::-webkit-scrollbar{width:4px}
.pbody::-webkit-scrollbar-thumb{background:rgba(0,212,255,.15);border-radius:2px}

/* Input bars */
.ibar{display:flex;gap:10px;align-items:center;padding:10px 16px;
      background:var(--bg2);border-top:1px solid var(--border);flex-shrink:0}
.prompt{color:var(--c2);font-weight:700;font-size:1.1rem;flex-shrink:0}
.mi{flex:1;background:var(--bg3);border:1px solid var(--border);border-radius:10px;
    padding:9px 14px;color:var(--text);font-family:inherit;font-size:.82rem;outline:none}
.mi:focus{border-color:var(--c)}.mi::placeholder{color:var(--sub)}
.mi.pi:focus{border-color:var(--cy)}
.rb{padding:9px 20px;border-radius:10px;border:1px solid;cursor:pointer;
    font-family:inherit;font-size:.8rem;font-weight:700;transition:all .2s}
.rb:disabled{opacity:.4;cursor:default}
.rb.run{color:var(--c);border-color:rgba(0,212,255,.5);background:rgba(0,212,255,.1)}
.rb.run:hover:not(:disabled){background:rgba(0,212,255,.22)}
.rb.snd{color:var(--cy);border-color:rgba(245,158,11,.5);background:rgba(245,158,11,.1)}
.rb.snd:hover:not(:disabled){background:rgba(245,158,11,.22)}

/* Chat */
.chat-wrap{display:flex;flex-direction:column;height:100%}
.chat-msgs{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px}
.chat-msgs::-webkit-scrollbar{width:4px}
.chat-msgs::-webkit-scrollbar-thumb{background:rgba(0,212,255,.15);border-radius:2px}
.msg{padding:10px 14px;border-radius:10px;font-size:.8rem;line-height:1.65;max-width:86%;word-break:break-word}
.msg.user{background:rgba(0,255,136,.08);border:1px solid rgba(0,255,136,.2);
          border-left:3px solid var(--c2);align-self:flex-end}
.msg.bot{background:rgba(0,212,255,.05);border:1px solid rgba(0,212,255,.12);
         border-left:3px solid var(--c);align-self:flex-start;white-space:pre-wrap}
.msg.bot.streaming::after{content:"|";animation:blink .8s step-end infinite;color:var(--c)}
@keyframes blink{50%{opacity:0}}
.plan-banner{display:none;margin:8px 16px;padding:12px 16px;border-radius:10px;
             background:rgba(0,255,136,.07);border:1px solid rgba(0,255,136,.25);
             font-size:.8rem;color:var(--c2)}
.plan-banner.show{display:block}

/* Terminal */
.term{padding:16px;font-size:.78rem;line-height:1.8;font-family:'Courier New',monospace}
.tl{margin-bottom:2px;word-break:break-word}
.tl.info{color:var(--text)}.tl.start{color:var(--c)}.tl.step{color:var(--cy)}
.tl.success{color:var(--c2)}.tl.warn{color:var(--cy);opacity:.8}
.tl.error{color:var(--red)}.tl.result{color:var(--text)}
.tl.done{color:var(--sub);border-top:1px solid var(--border);margin-top:8px;padding-top:8px}
.tl.idle{color:var(--sub);font-style:italic}
.tl pre{background:var(--bg3);border:1px solid var(--border);border-radius:8px;
        padding:8px 12px;margin-top:6px;overflow-x:auto;font-size:.72rem;
        white-space:pre-wrap;word-break:break-all}
.tl b{font-weight:700}
.abar{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}

/* Brain editor */
.feditor{display:flex;flex-direction:column;height:100%}
.eta{flex:1;width:100%;background:var(--bg);color:var(--text);border:none;outline:none;
     padding:16px;font-family:'Courier New',monospace;font-size:.78rem;line-height:1.65;
     resize:none;tab-size:2}
.fempty{padding:40px 24px;text-align:center;color:var(--sub);font-size:.88rem}
.fview{padding:16px;font-size:.76rem;line-height:1.65;color:var(--text);
       white-space:pre-wrap;word-break:break-all;font-family:'Courier New',monospace}

/* KI bar (alte single-line input — bleibt fuer Kompat) */
.kibar{display:none;padding:8px 14px;background:var(--bg3);
       border-top:1px solid rgba(167,139,250,.3);gap:8px;align-items:center;flex-shrink:0}
.kibar.show{display:flex}
.kii{flex:1;background:var(--bg2);border:1px solid rgba(167,139,250,.4);border-radius:8px;
     padding:7px 12px;color:var(--text);font-family:inherit;font-size:.78rem;outline:none}
.kii:focus{border-color:var(--purple)}.kii::placeholder{color:var(--sub)}

/* KI Chat Panel — full chat experience for brain edits */
.kichat{display:none;flex-direction:column;background:var(--bg3);
        border-top:1px solid rgba(167,139,250,.3);flex-shrink:0;
        max-height:55%}
.kichat.show{display:flex}
.kichat-hdr{display:flex;align-items:center;gap:8px;padding:6px 14px;
            background:var(--bg2);border-bottom:1px solid rgba(167,139,250,.18);
            font-size:.7rem;color:var(--purple);font-weight:700;
            text-transform:uppercase;letter-spacing:.5px;flex-shrink:0}
.kichat-hdr-fn{color:var(--sub);font-weight:500;text-transform:none;letter-spacing:0;
               margin-left:4px;font-size:.7rem;overflow:hidden;text-overflow:ellipsis;
               white-space:nowrap;max-width:180px}
.kichat-hdr .btn{margin-left:auto;padding:3px 9px;font-size:.65rem}
.kichat-msgs{flex:1;overflow-y:auto;padding:12px 14px;display:flex;
             flex-direction:column;gap:8px;min-height:120px;max-height:300px}
.kichat-msgs::-webkit-scrollbar{width:4px}
.kichat-msgs::-webkit-scrollbar-thumb{background:rgba(167,139,250,.2);border-radius:2px}
.kimsg{padding:8px 12px;border-radius:9px;font-size:.78rem;line-height:1.55;
       max-width:88%;word-break:break-word}
.kimsg.user{background:rgba(0,255,136,.07);border:1px solid rgba(0,255,136,.18);
            border-left:3px solid var(--c2);align-self:flex-end}
.kimsg.bot{background:rgba(167,139,250,.07);border:1px solid rgba(167,139,250,.18);
           border-left:3px solid var(--purple);align-self:flex-start;white-space:pre-wrap}
.kimsg.bot.streaming::after{content:"|";animation:blink .8s step-end infinite;color:var(--purple)}
.kichat-bar{display:flex;gap:8px;align-items:center;padding:8px 14px;
            background:var(--bg3);border-top:1px solid rgba(167,139,250,.18);
            flex-shrink:0}

/* Diff overlay */
.diffov{display:none;position:absolute;inset:0;background:rgba(2,6,23,.97);
        z-index:100;flex-direction:column;overflow:hidden}
.diffov.show{display:flex}
.diffhdr{padding:8px 14px;background:var(--bg2);border-bottom:1px solid var(--border);
         display:flex;align-items:center;gap:8px;font-size:.75rem;font-weight:700;
         color:var(--sub);flex-shrink:0;text-transform:uppercase;letter-spacing:.5px}
.diffbd{flex:1;overflow-y:auto;padding:16px;font-size:.74rem;line-height:1.65;
        font-family:'Courier New',monospace}
.diffbd pre{white-space:pre-wrap;word-break:break-all}
.diffft{padding:8px 14px;background:var(--bg2);border-top:1px solid var(--border);
        display:flex;gap:8px;justify-content:flex-end;flex-shrink:0}
.da{color:var(--c2)}.dd{color:var(--red)}.dm{color:var(--sub)}

/* Buttons */
.btn{padding:6px 14px;border-radius:8px;border:1px solid;cursor:pointer;
     font-size:.76rem;font-family:inherit;font-weight:700;transition:all .2s}
.btn:disabled{opacity:.4;cursor:default}
.bc{color:var(--c);border-color:rgba(0,212,255,.5);background:rgba(0,212,255,.08)}
.bc:hover:not(:disabled){background:rgba(0,212,255,.2)}
.bg{color:var(--c2);border-color:rgba(0,255,136,.5);background:rgba(0,255,136,.08)}
.bg:hover:not(:disabled){background:rgba(0,255,136,.2)}
.bp{color:var(--purple);border-color:rgba(167,139,250,.5);background:rgba(167,139,250,.08)}
.bp:hover:not(:disabled){background:rgba(167,139,250,.2)}
.by{color:var(--cy);border-color:rgba(245,158,11,.5);background:rgba(245,158,11,.08)}
.by:hover:not(:disabled){background:rgba(245,158,11,.2)}
.bs{color:var(--sub);border-color:var(--border);background:transparent}
.bs:hover:not(:disabled){color:var(--text);border-color:rgba(0,212,255,.3)}

/* Spinner */
.spin{display:inline-block;width:10px;height:10px;border:2px solid rgba(0,212,255,.2);
      border-top-color:var(--c);border-radius:50%;animation:rot .7s linear infinite;
      margin-right:6px;vertical-align:middle}
.spin.y{border-top-color:var(--cy);border-color:rgba(245,158,11,.2)}
@keyframes rot{to{transform:rotate(360deg)}}
</style>
</head>
<body>

<!-- Daten-Container: kein JS, kein HTML-Parser-Konflikt -->
<div id="treeB64" data-t="{{ tree_b64|safe }}" style="display:none"></div>

<div class="app">

<div class="hdr">
  <div class="logo">
    <div class="logo-icon">{{ bot_i }}</div>
    <div><div class="logo-name">{{ bot }}</div><div class="logo-sub">LAB / ORCHESTRATOR</div></div>
  </div>
  <nav class="nav">
    <button class="nbtn act-y" id="nP" onclick="setMode('plan')">&#x1F4AC; Plan</button>
    <button class="nbtn"       id="nM" onclick="setMode('mission')">&#x26A1; Mission</button>
    <button class="nbtn"       id="nB" onclick="setMode('brain')">&#x1F9E0; Brain</button>
  </nav>
  <div class="hright">
    {% if orch_ok %}
      <span class="ost ok">orchestrator.py</span>
    {% else %}
      <span class="ost warn">Fallback</span>
    {% endif %}
    <a href="/" class="back">&#x2190; Dashboard</a>
  </div>
</div>

<div class="modebar">
  <div class="mpill plan" id="mpill"><div class="mdot"></div><span id="mlabel">PLANMODUS</span></div>
  <span style="color:var(--border)">|</span>
  <span id="mdesc" style="color:var(--sub)">Besprich was gebaut werden soll</span>
  <span class="mstep" id="mstep"></span>
</div>

<div class="body">

  <div class="sidebar">
    <div class="sbhdr">
      <span>Explorer</span>
      <span class="rld" onclick="reloadTree()" title="Aktualisieren">&#8635;</span>
    </div>
    <div class="tree" id="tree">
      <div style="padding:12px 14px;color:var(--sub);font-size:.75rem">Laedt...</div>
    </div>
  </div>

  <div class="workspace">
    <div class="panels" id="panels">

      <!-- Plan -->
      <div class="panel" id="pPlan">
        <div class="phdr">
          <div class="pdot y"></div><span>Planmodus</span>
          <div style="margin-left:auto">
            <button class="btn bs" onclick="resetPlan()" style="font-size:.65rem">&#8635; Neu</button>
          </div>
        </div>
        <div class="pbody">
          <div class="chat-wrap">
            <div class="chat-msgs" id="chatMsgs">
              <div class="msg bot">Was soll ich bauen? Beschreib einfach deine Idee — ich stelle Rückfragen und wenn der Plan steht, starte ich die Mission automatisch.</div>
            </div>
            <div class="plan-banner" id="planBanner">
              &#x2705; <b>Plan fertig:</b> <span id="planBannerText"></span><br>
              <button class="btn by" style="margin-top:8px" onclick="launchMission()">&#x26A1; Mission starten</button>
            </div>
          </div>
        </div>
      </div>

      <!-- Mission -->
      <div class="panel" id="pM" style="display:none">
        <div class="phdr">
          <div class="pdot"></div><span>Missionsmodus — Terminal</span>
          <span id="tSt" style="margin-left:auto;font-size:.65rem"></span>
        </div>
        <div class="pbody" id="tBody">
          <div class="term" id="term">
            <div class="tl idle">{{ bot }} Orchestrator bereit — Mission eingeben.</div>
          </div>
        </div>
      </div>

      <!-- Brain/Datei-Viewer -->
      <div class="panel" id="pV" style="display:none;position:relative">
        <div class="phdr">
          <div class="pdot p"></div>
          <span id="vTitle" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:200px">Datei</span>
          <div style="margin-left:auto;display:flex;gap:6px;flex-shrink:0">
            <button class="btn bp" id="kiTog" style="display:none" onclick="toggleKi()">&#x2728; KI-Edit</button>
            <button class="btn bg"  id="saveB" style="display:none" onclick="saveFile()">&#x1F4BE; Speichern</button>
            <button class="btn bs"             onclick="closeViewer()">&#x2715;</button>
          </div>
        </div>
        <div class="pbody" id="vBody"><div class="fempty">Datei im Explorer wählen.</div></div>
        <div class="kichat" id="kichat">
          <div class="kichat-hdr">
            <span style="color:var(--purple)">&#x2728; KI-EDIT CHAT</span>
            <span class="kichat-hdr-fn" id="kichatFn"></span>
            <button class="btn bs" id="kichatRst" onclick="resetKi()">&#x21BB; Neu</button>
          </div>
          <div class="kichat-msgs" id="kichatMsgs">
            <div class="kimsg bot">Was soll an dieser Datei geändert werden? Sag's mir locker — ich frag nach was unklar ist und wenn du mit "übernimm" oder "passt" bestätigst, schlag ich den fertigen Datei-Stand vor.</div>
          </div>
          <div class="kichat-bar">
            <span style="color:var(--purple);flex-shrink:0">&#x2728;</span>
            <input type="text" class="kii" id="kichatInp"
                   placeholder="Aenderung beschreiben oder Ruecksprache..."
                   onkeydown="if(event.key==='Enter')sendKi()">
            <button class="btn bp" id="kichatBtn" onclick="sendKi()">Senden &#x2192;</button>
          </div>
        </div>
        <div class="kibar" id="kibar">
          <span style="color:var(--purple);flex-shrink:0">&#x2728;</span>
          <input type="text" class="kii" id="kii" placeholder="Aenderung beschreiben..."
                 onkeydown="if(event.key==='Enter')runKi()">
          <button class="btn bp" id="kiRunB" onclick="runKi()">OK</button>
        </div>
        <div class="diffov" id="diffOv">
          <div class="diffhdr">
            <div class="pdot y"></div><span>Diff — Änderungen prüfen</span>
            <span id="diffSt" style="margin-left:8px;color:var(--cy)"></span>
          </div>
          <div class="diffbd" id="diffBd"></div>
          <div class="diffft">
            <button class="btn bs" onclick="closeDiff()">&#x274C; Verwerfen</button>
            <button class="btn bg" onclick="applyKi()">&#x2705; Übernehmen</button>
          </div>
        </div>
      </div>

    </div>

    <div class="ibar" id="ibarPlan">
      <span class="prompt" style="color:var(--cy)">&#x1F4AC;</span>
      <input type="text" class="mi pi" id="planInp"
             placeholder="Beschreib was gebaut werden soll..."
             onkeydown="if(event.key==='Enter')sendPlan()">
      <button class="rb snd" id="planBtn" onclick="sendPlan()">Senden &#x2192;</button>
    </div>
    <div class="ibar" id="ibarM" style="display:none">
      <span class="prompt">$</span>
      <input type="text" class="mi" id="mI" placeholder="Mission eingeben..."
             onkeydown="if(event.key==='Enter')runM()">
      <button class="rb run" id="rb" onclick="runM()">&#x25B6; RUN</button>
    </div>
    <div class="ibar" id="ibarB" style="display:none"></div>
  </div>

</div>
</div>

<script>
"use strict";

// State
var curMode   = "plan";
var curFile   = null;
var kiPend    = null;
var splitOpen = false;
var planId    = "p_" + Date.now();
var planTask  = null;
// KI-Edit Chat State
var kiSessId      = null;   // session_id im Backend
var kiSessFile    = null;   // an welche Datei gebunden
var kiStreaming   = false;
var rb, tSt, term, tBody;

// Tree aus hidden div lesen
var _TREE = [];
try {
    var _el = document.getElementById("treeB64");
    if (_el) { _TREE = JSON.parse(atob(_el.getAttribute("data-t") || "W10=")); }
} catch(e) { console.warn("Tree parse:", e); }

// Mode
function setMode(m) {
    curMode = m;
    var ids = ["nP","nM","nB"];
    ids.forEach(function(id){ var el=document.getElementById(id); if(el) el.className="nbtn"; });

    hide("pPlan"); hide("pM");
    hide("ibarPlan"); hide("ibarM"); hide("ibarB");

    if (m === "plan") {
        document.getElementById("nP").className = "nbtn act-y";
        show("pPlan","flex"); show("ibarPlan","flex");
        if (splitOpen && curFile) show("pV","flex"); else hide("pV");
    } else if (m === "mission") {
        document.getElementById("nM").className = "nbtn act-c";
        show("pM","flex"); show("ibarM","flex");
        if (splitOpen && curFile) show("pV","flex"); else hide("pV");
    } else {
        document.getElementById("nB").className = "nbtn act-p";
        show("pV","flex"); show("ibarB","flex");
        document.getElementById("panels").classList.remove("split");
        splitOpen = false;
        if (!curFile) brainWelcome();
    }
    updateModeBar(m, null);
}

function show(id, d) { var el=document.getElementById(id); if(el) el.style.display=d||"block"; }
function hide(id)    { var el=document.getElementById(id); if(el) el.style.display="none"; }

function updateModeBar(m, building) {
    var pill=document.getElementById("mpill");
    var lbl=document.getElementById("mlabel");
    var dsc=document.getElementById("mdesc");
    if (!pill) return;
    pill.className = "mpill";
    if (building) {
        pill.classList.add("building"); lbl.textContent="BUILDING"; dsc.textContent=building;
    } else if (m==="plan") {
        pill.classList.add("plan"); lbl.textContent="PLANMODUS";
        dsc.textContent = planTask ? "Plan bereit" : "Besprich was gebaut werden soll";
    } else if (m==="mission") {
        pill.classList.add("mission"); lbl.textContent="MISSIONSMODUS";
        dsc.textContent = "Orchestrator wartet";
    } else {
        pill.classList.add("brain"); lbl.textContent="BRAIN EDITOR";
        dsc.textContent = curFile ? curFile.name : "memory/brain/";
    }
}
function setStep(t) { var el=document.getElementById("mstep"); if(el) el.textContent=t||""; }

// Plan Chat
function sendPlan() {
    var inp = document.getElementById("planInp");
    var msg = inp.value.trim(); if (!msg) return;
    inp.value = "";
    // Plan ist nicht mehr "fertig" sobald eine neue Nachricht kommt — Banner weg.
    // Wenn der LLM erneut PLAN_FERTIG generiert, wird es danach wieder gesetzt.
    planTask = null;
    var pb = document.getElementById("planBanner");
    if (pb) pb.classList.remove("show");
    addMsg(msg, "user");
    var btn = document.getElementById("planBtn");
    btn.disabled = true; btn.innerHTML = '<span class="spin y"></span>';
    var botDiv = addMsg("", "bot streaming");

    fetch("/lab/plan", {method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({plan_id:planId, message:msg})
    }).then(function(r) {
        var reader = r.body.getReader(); var dec = new TextDecoder(); var buf = "";
        function read() {
            reader.read().then(function(res) {
                if (res.done) { botDiv.classList.remove("streaming"); return; }
                buf += dec.decode(res.value, {stream:true});
                var lines = buf.split("\\n"); buf = lines.pop();
                lines.forEach(function(line) {
                    if (!line.startsWith("data: ")) return;
                    try {
                        var d = JSON.parse(line.slice(6));
                        if (d.token) { botDiv.textContent += d.token; scrollMsgs(); }
                        if (d.plan_ready) { planTask=d.plan_ready; showBanner(d.plan_ready); updateModeBar("plan",null); }
                        if (d.done) botDiv.classList.remove("streaming");
                    } catch(e) {}
                });
                read();
            });
        }
        read();
    }).catch(function(e) {
        botDiv.textContent = "Fehler: "+e.message; botDiv.classList.remove("streaming");
    }).finally(function() { btn.disabled=false; btn.textContent="Senden"; });
}

function addMsg(t, cls) {
    var msgs = document.getElementById("chatMsgs");
    var d = document.createElement("div"); d.className="msg "+cls; d.textContent=t;
    msgs.appendChild(d); scrollMsgs(); return d;
}
function scrollMsgs() { document.getElementById("chatMsgs").scrollTop=999999; }
function showBanner(task) {
    document.getElementById("planBannerText").textContent=task;
    document.getElementById("planBanner").classList.add("show");
}
function launchMission() {
    if (!planTask) return;
    document.getElementById("mI").value = planTask;
    setMode("mission"); runM();
}
function resetPlan() {
    fetch("/lab/plan/reset",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({plan_id:planId})});
    planId="p_"+Date.now(); planTask=null;
    document.getElementById("chatMsgs").innerHTML='<div class="msg bot">Neuer Plan. Was soll gebaut werden?</div>';
    document.getElementById("planBanner").classList.remove("show");
    updateModeBar("plan",null);
}

// Mission Terminal
function addL(h,c) {
    if(!term) return;
    var d=document.createElement("div"); d.className="tl "+(c||"info"); d.innerHTML=h;
    term.appendChild(d); if(tBody) tBody.scrollTop=tBody.scrollHeight;
}
function clearTerm() { if(term) term.innerHTML=""; }

function runM() {
    var mi=document.getElementById("mI"); var task=mi.value.trim(); if(!task) return;
    // Wenn der Mission-Text dem fertigen Plan entspricht, plan_id mitsenden,
    // damit der Backend den ganzen Plan-Verlauf an den Builder weitergibt.
    var planIdToSend = (planTask && task === planTask) ? planId : null;
    clearTerm();
    if(rb) { rb.disabled=true; rb.innerHTML='<span class="spin"></span>Running'; }
    if(tSt) tSt.innerHTML='<span style="color:var(--cy)">&#9679; RUNNING</span>';
    updateModeBar("mission","Starte...");
    addL("$ "+esc(task),"step");

    fetch("/lab/run",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({task:task, plan_id:planIdToSend})
    }).then(function(r){return r.json();}).then(function(data){
        if(!data.mission_id){addL("Fehler: "+esc(data.error||"?"),"error");resetRb();return;}
        var es=new EventSource("/lab/stream/"+data.mission_id);
        es.onmessage=function(e){
            var msg=JSON.parse(e.data);
            if(msg.type==="ping") return;
            if(msg.type==="done"){addL("───────────────────","done");addL(esc(msg.text),"done");es.close();resetRb();setStep("");return;}
            if(msg.type==="action"){renderAbar(msg);return;}
            if(msg.type==="step"){setStep(msg.text.replace(/<[^>]*>/g,""));updateModeBar("mission",msg.text.replace(/<[^>]*>/g,""));}
            addL(msg.text||"",msg.type||"info");
        };
        es.onerror=function(){es.close();resetRb();};
    }).catch(function(e){addL("Fehler: "+esc(e.message),"error");resetRb();});
}
function resetRb() {
    if(rb){rb.disabled=false;rb.innerHTML="&#x25B6; RUN";}
    if(tSt) tSt.innerHTML='<span style="color:var(--c2)">&#9679; IDLE</span>';
    updateModeBar("mission",null);
}
function renderAbar(action) {
    var bar=document.createElement("div"); bar.className="abar";
    if(action.mode==="install"){
        bar.appendChild(mkB("&#x1F4E5; Modul installieren","bg",function(){installMod(action.task_id,action.filename,bar.children[0]);}));
        bar.appendChild(mkB("&#x1F441; Code","bs",function(){openWS(action.task_id,"auto");}));
    }
    if(action.mode==="run_script"){
        bar.appendChild(mkB("&#x25B6; Skript ausführen","bc",function(){runSc(action.task_id,bar.children[0]);}));
        bar.appendChild(mkB("&#x1F441; Code","bs",function(){openWS(action.task_id,"script");}));
    }
    var w=document.createElement("div"); w.className="tl"; w.appendChild(bar);
    if(term) term.appendChild(w); if(tBody) tBody.scrollTop=tBody.scrollHeight;
}
function mkB(lbl,cls,fn){var b=document.createElement("button");b.className="btn "+cls;b.innerHTML=lbl;b.onclick=fn;return b;}
function installMod(tid,fname,btn){
    btn.disabled=true; btn.innerHTML='<span class="spin"></span>';
    fetch("/lab/install",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({task_id:tid,filename:fname})})
    .then(function(r){return r.json();}).then(function(d){
        if(d.ok){addL(esc(d.message),"success");btn.innerHTML="&#x2705; Installiert";}
        else{addL("Fehler: "+esc(d.error),"error");btn.disabled=false;btn.textContent="Installieren";}
    });
}
function runSc(tid,btn){
    btn.disabled=true; btn.innerHTML='<span class="spin"></span>';
    fetch("/lab/run-script",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({task_id:tid})})
    .then(function(r){return r.json();}).then(function(d){
        if(d.ok){addL("OUTPUT:\\n<pre>"+esc(d.output)+"</pre>","result");btn.textContent="Erneut";btn.disabled=false;}
        else{addL("Fehler: "+esc(d.error),"error");btn.disabled=false;btn.textContent="Ausführen";}
    });
}
function openWS(tid,pfx){
    var fname=(pfx==="script"?"script_":"auto_")+tid+".py";
    openFile("{{ ws }}/"+fname, fname, false);
}

// File Viewer
function openFile(path,name,isBrain){
    var vb=document.getElementById("vBody");
    var pV=document.getElementById("pV");
    var panels=document.getElementById("panels");
    document.getElementById("vTitle").textContent=name;
    vb.innerHTML='<div class="fempty"><span class="spin"></span>Laedt...</div>';
    if(curMode!=="brain"){
        if(!splitOpen){pV.style.display="flex";panels.classList.add("split");splitOpen=true;}
    }
    document.querySelectorAll(".tfile").forEach(function(el){
        if(el.dataset && el.dataset.path!==undefined) el.classList.toggle("act",el.dataset.path===path);
    });
    fetch("/lab/file?path="+encodeURIComponent(path)).then(function(r){return r.json();}).then(function(d){
        if(d.content===undefined){vb.innerHTML='<div class="fempty">Fehler: '+esc(d.error||"?")+"</div>";return;}
        curFile={path:path,name:name,brain:d.brain||isBrain,content:d.content};
        renderEditor(); updateModeBar(curMode,null);
    }).catch(function(e){vb.innerHTML='<div class="fempty">Fehler: '+esc(e.message)+"</div>";});
}
function renderEditor(){
    var vb=document.getElementById("vBody");
    var saveB=document.getElementById("saveB"); var kiTog=document.getElementById("kiTog");
    if(curFile.brain){
        vb.innerHTML="";
        var w=document.createElement("div"); w.className="feditor";
        var ta=document.createElement("textarea"); ta.className="eta"; ta.id="eta";
        ta.value=curFile.content; ta.addEventListener("input",function(){curFile.content=ta.value;});
        w.appendChild(ta); vb.appendChild(w);
        saveB.style.display="inline-block"; kiTog.style.display="inline-block";
        // Wenn der KI-Chat schon offen war fuer eine andere Datei → Session reset
        var kc = document.getElementById("kichat");
        if (kc && kc.classList.contains("show") && kiSessFile !== curFile.name) {
            resetKiUI(curFile.name);
        }
    } else {
        var pre=document.createElement("div"); pre.className="fview";
        pre.textContent=curFile.content; vb.innerHTML=""; vb.appendChild(pre);
        saveB.style.display="none"; kiTog.style.display="none";
        document.getElementById("kibar").classList.remove("show");
        var kc2 = document.getElementById("kichat");
        if (kc2) kc2.classList.remove("show");
    }
}
function closeViewer(){
    hide("pV"); document.getElementById("panels").classList.remove("split");
    splitOpen=false; curFile=null;
    document.querySelectorAll(".tfile").forEach(function(el){el.classList.remove("act");});
    document.getElementById("kibar").classList.remove("show");
    var kc = document.getElementById("kichat");
    if (kc) kc.classList.remove("show");
    closeDiff(); updateModeBar(curMode,null);
}
function saveFile(){
    if(!curFile||!curFile.brain) return;
    var ta=document.getElementById("eta"); if(ta) curFile.content=ta.value;
    fetch("/lab/brain/save",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({filename:curFile.name,content:curFile.content})})
    .then(function(r){return r.json();}).then(function(d){
        toast(d.ok?"Gespeichert"+(d.backup?" ("+d.backup+")":" "):"Fehler: "+(d.error||"?"),!d.ok);
    });
}
function toggleKi(){
    var kc = document.getElementById("kichat");
    if (!kc) return;
    var nowOn = !kc.classList.contains("show");
    if (nowOn) {
        // Wenn Datei wechselt: Session zuruecksetzen (sonst falscher Kontext)
        if (curFile && kiSessFile !== curFile.name) {
            resetKiUI(curFile.name);
        }
        kc.classList.add("show");
        var fnEl = document.getElementById("kichatFn");
        if (fnEl && curFile) fnEl.textContent = curFile.name;
        var inp = document.getElementById("kichatInp");
        if (inp) setTimeout(function(){ inp.focus(); }, 50);
    } else {
        kc.classList.remove("show");
    }
}

// KI-Edit Chat — neue Variante (mit Verlauf, Streaming, FILE_READY-Trigger)
function resetKiUI(filename){
    // UI zuruecksetzen — neue Session-ID, Verlauf leeren
    kiSessId   = "k_" + Date.now() + "_" + Math.floor(Math.random()*9999);
    kiSessFile = filename || (curFile ? curFile.name : null);
    var msgs = document.getElementById("kichatMsgs");
    if (msgs) {
        msgs.innerHTML = '<div class="kimsg bot">Was soll an <b>'
            + esc(kiSessFile || "dieser Datei")
            + '</b> geändert werden? Beschreib die Änderung locker — ich frag nach was unklar ist und wenn du mit "übernimm" oder "passt" bestätigst, schlag ich den fertigen Datei-Stand vor.</div>';
    }
    var fnEl = document.getElementById("kichatFn");
    if (fnEl) fnEl.textContent = kiSessFile || "";
}

function resetKi(){
    // Backend-Session ebenfalls resetten (best-effort)
    if (kiSessId) {
        fetch("/lab/brain/ki-chat/reset",{method:"POST",headers:{"Content-Type":"application/json"},
            body:JSON.stringify({session_id:kiSessId})}).catch(function(){});
    }
    resetKiUI(curFile ? curFile.name : null);
}

function addKiMsg(text, cls){
    var msgs = document.getElementById("kichatMsgs");
    if (!msgs) return null;
    var d = document.createElement("div");
    d.className = "kimsg " + cls;
    d.textContent = text;
    msgs.appendChild(d);
    msgs.scrollTop = 999999;
    return d;
}

function sendKi(){
    if (kiStreaming) return;
    if (!curFile || !curFile.brain) { toast("Keine Brain-Datei geöffnet", true); return; }
    var inp = document.getElementById("kichatInp");
    var msg = inp.value.trim();
    if (!msg) return;
    inp.value = "";

    // Falls noch keine Session laeuft oder Datei gewechselt: starten
    if (!kiSessId || kiSessFile !== curFile.name) {
        resetKiUI(curFile.name);
    }

    // Live-Editor-Inhalt mitschicken (User koennte manuell editiert haben)
    var ta = document.getElementById("eta");
    var liveContent = ta ? ta.value : curFile.content;
    curFile.content = liveContent;

    addKiMsg(msg, "user");
    var btn = document.getElementById("kichatBtn");
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spin"></span>'; }
    var botDiv = addKiMsg("", "bot streaming");
    kiStreaming = true;

    fetch("/lab/brain/ki-chat", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
            session_id: kiSessId,
            filename:   curFile.name,
            message:    msg,
            content:    liveContent
        })
    }).then(function(r){
        if (!r.ok) {
            return r.json().then(function(j){ throw new Error(j.error || ("HTTP "+r.status)); });
        }
        var reader = r.body.getReader();
        var dec    = new TextDecoder();
        var buf    = "";
        function read(){
            reader.read().then(function(res){
                if (res.done) {
                    botDiv.classList.remove("streaming");
                    kiStreaming = false;
                    if (btn) { btn.disabled = false; btn.textContent = "Senden →"; }
                    return;
                }
                buf += dec.decode(res.value, {stream:true});
                var lines = buf.split("\\n");
                buf = lines.pop();
                lines.forEach(function(line){
                    if (!line.startsWith("data: ")) return;
                    try {
                        var d = JSON.parse(line.slice(6));
                        if (d.token) {
                            botDiv.textContent += d.token;
                            var msgs = document.getElementById("kichatMsgs");
                            if (msgs) msgs.scrollTop = 999999;
                        }
                        if (d.file_ready_pending) {
                            // Status-Hinweis im Bot-Bubble
                            if (!botDiv.textContent.trim()) {
                                botDiv.textContent = "(bereitet Datei vor...)";
                            } else {
                                botDiv.textContent += "\\n\\n(bereitet Datei vor...)";
                            }
                            var msgs2 = document.getElementById("kichatMsgs");
                            if (msgs2) msgs2.scrollTop = 999999;
                        }
                        if (d.file_ready) {
                            // Diff-Overlay aufmachen — Apply laeuft danach ueber /lab/brain/apply
                            kiPend = {
                                original: d.original,
                                modified: d.modified,
                                filename: d.filename
                            };
                            // Status-Hinweis im Bot-Bubble ersetzen
                            botDiv.textContent = (botDiv.textContent.replace(/\\(bereitet Datei vor\\.\\.\\.\\)/g,"").trim() ||
                                                  "Vorschlag fertig — Diff zur Prüfung geöffnet.");
                            showDiff(d.diff, d.changed);
                        }
                        if (d.file_ready_error) {
                            botDiv.textContent += "\\n\\n⚠️ Fehler: " + d.file_ready_error;
                            toast("KI-Output: " + d.file_ready_error, true);
                        }
                        if (d.error) {
                            botDiv.textContent += "\\n\\n⚠️ " + d.error;
                        }
                    } catch(e) {}
                });
                read();
            }).catch(function(e){
                botDiv.textContent += "\\n\\n⚠️ Fehler: " + e.message;
                botDiv.classList.remove("streaming");
                kiStreaming = false;
                if (btn) { btn.disabled = false; btn.textContent = "Senden →"; }
            });
        }
        read();
    }).catch(function(e){
        botDiv.textContent = "Fehler: " + e.message;
        botDiv.classList.remove("streaming");
        kiStreaming = false;
        if (btn) { btn.disabled = false; btn.textContent = "Senden →"; }
    });
}

// Legacy single-shot KI-Edit (kibar) — bleibt erhalten, aktuell nicht
// vom UI getriggert (toggleKi macht jetzt den Chat auf). Nur als
// Fallback falls etwas extern noch runKi() ruft.
function runKi(){
    if(!curFile||!curFile.brain) return;
    var cr=document.getElementById("kii").value.trim(); if(!cr){toast("Beschreibung eingeben",true);return;}
    var ta=document.getElementById("eta"); if(ta) curFile.content=ta.value;
    var btn=document.getElementById("kiRunB"); btn.disabled=true; btn.innerHTML='<span class="spin"></span>';
    fetch("/lab/brain/ki-edit",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({filename:curFile.name,content:curFile.content,change_req:cr})})
    .then(function(r){return r.json();}).then(function(d){
        btn.disabled=false; btn.textContent="OK";
        if(!d.ok){toast("Fehler: "+(d.error||"?"),true);return;}
        kiPend={original:d.original,modified:d.modified,filename:curFile.name};
        showDiff(d.diff,d.changed);
    }).catch(function(e){btn.disabled=false;btn.textContent="OK";toast("Fehler: "+e.message,true);});
}
function showDiff(diff,changed){
    var ov=document.getElementById("diffOv"); var bd=document.getElementById("diffBd");
    document.getElementById("diffSt").textContent=changed+" Zeilen";
    var lines=diff.split("\\n"); var h="";
    lines.forEach(function(l){
        var cls="";
        if(l.startsWith("+++")||l.startsWith("---")) cls="dm";
        else if(l.startsWith("+")) cls="da";
        else if(l.startsWith("-")) cls="dd";
        else if(l.startsWith("@@")) cls="dm";
        h+=cls?'<span class="'+cls+'">'+esc(l)+"</span>\\n":esc(l)+"\\n";
    });
    bd.innerHTML="<pre>"+h+"</pre>"; ov.classList.add("show");
}
function closeDiff(){document.getElementById("diffOv").classList.remove("show");kiPend=null;}
function applyKi(){
    if(!kiPend) return;
    fetch("/lab/brain/apply",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({filename:kiPend.filename,content:kiPend.modified})})
    .then(function(r){return r.json();}).then(function(d){
        if(d.ok){
            curFile.content=kiPend.modified;
            var ta=document.getElementById("eta"); if(ta) ta.value=kiPend.modified;
            var fname = kiPend.filename;
            var bk    = d.backup ? (" (Backup: "+d.backup+")") : "";
            closeDiff();
            document.getElementById("kibar").classList.remove("show");
            document.getElementById("kii").value="";
            // Bestaetigung im KI-Chat anzeigen — Chat bleibt offen,
            // User kann direkt weiter editieren wenn er will.
            var kc = document.getElementById("kichat");
            if (kc && kc.classList.contains("show")) {
                addKiMsg("✅ Übernommen — '" + fname + "' gespeichert" + bk + ".", "bot");
            }
            toast("KI-Edit angewendet"+(d.backup?" ("+d.backup+")":""));
        } else toast("Fehler: "+(d.error||"?"),true);
    });
}
function brainWelcome(){
    document.getElementById("vBody").innerHTML=
        '<div class="fempty"><div style="margin-bottom:10px;color:var(--purple);font-size:.95rem">&#x1F9E0; Brain Editor</div>'+
        '<div style="color:var(--sub);margin-bottom:14px">Datei im Explorer wählen oder neu erstellen.</div>'+
        '<button class="btn bp" onclick="newBrainFile()">+ Neue Datei</button></div>';
    document.getElementById("saveB").style.display="none";
    document.getElementById("kiTog").style.display="none";
    var kc = document.getElementById("kichat");
    if (kc) kc.classList.remove("show");
    var kb = document.getElementById("kibar");
    if (kb) kb.classList.remove("show");
}
function newBrainFile(){
    var name=prompt("Dateiname (z.B. notes.txt):"); if(!name) return;
    fetch("/lab/brain/new",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({filename:name.trim(),content:""})})
    .then(function(r){return r.json();}).then(function(d){
        if(d.ok){toast("Erstellt");loadTree();openFile(d.path,name.trim(),true);}
        else toast("Fehler: "+(d.error||"?"),true);
    });
}

// Tree — zeigt memory/brain/ + workspace/ (beide löschbar)
function loadTree(){
    var c=document.getElementById("tree");
    var tree=_TREE;
    c.innerHTML="";
    if(!Array.isArray(tree)||tree.length===0){
        c.innerHTML='<div style="padding:12px 14px;color:var(--sub);font-size:.75rem">Keine Dateien</div>';
        return;
    }
    tree.forEach(function(section){
        var isBrain     = !!section.brain;
        var isWorkspace = !!section.workspace;
        if(!isBrain && !isWorkspace) return;

        var hdr=document.createElement("div");
        hdr.className="tdir"+(isBrain?" brain":"");
        hdr.textContent=section.name||"";
        c.appendChild(hdr);

        if(!section.children || section.children.length===0){
            var empty=document.createElement("div");
            empty.style.cssText="padding:6px 22px;color:var(--sub);font-size:.7rem;font-style:italic";
            empty.textContent="(leer)";
            c.appendChild(empty);
            return;
        }

        section.children.forEach(function(f){
            if(f.type!=="file") return;
            var el=document.createElement("div");
            el.className="tfile"+(isBrain?" brain-f":"");
            if(f.path) el.dataset.path=f.path;

            // Icon: 🖱️ für .app, 📝 für brain, 🐍 für sonstige
            var icon = f.is_app ? "&#x1F5B1;&#xFE0F; "
                      : isBrain   ? "&#x1F4DD; "
                      : f.name.endsWith(".command") ? "&#x1F517; "
                      : "&#x1F40D; ";

            // Name in eigenem Span damit Trash daneben passt
            var nameSpan=document.createElement("span");
            nameSpan.className="tfile-name";
            nameSpan.innerHTML=icon+esc(f.name);
            nameSpan.onclick=function(){ openFile(f.path,f.name,isBrain); };
            el.appendChild(nameSpan);

            // Trash-Button
            var trash=document.createElement("span");
            trash.className="tfile-trash";
            trash.innerHTML="&#x1F5D1;";  // 🗑
            trash.title="In den Papierkorb verschieben";
            trash.onclick=function(ev){
                ev.stopPropagation();
                trashFile(f.path, f.name, el);
            };
            el.appendChild(trash);

            c.appendChild(el);
        });
    });
}

function trashFile(path, name, rowEl){
    if(!confirm("'"+name+"' in den Papierkorb verschieben?")) return;
    fetch("/lab/trash",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({path:path})})
    .then(function(r){return r.json();}).then(function(d){
        if(d.ok){
            // Wenn die gelöschte Datei gerade im Viewer offen ist → schließen
            if(curFile && curFile.path===path) closeViewer();
            if(rowEl && rowEl.parentNode) rowEl.parentNode.removeChild(rowEl);
            toast("'"+name+"' im Papierkorb");
            // Tree neu laden damit Sektion wieder konsistent ist
            reloadTree();
        } else {
            toast("Fehler: "+(d.error||"?"),true);
        }
    }).catch(function(e){ toast("Fehler: "+e.message,true); });
}

async function reloadTree(){
    try{
        var r=await fetch("/lab/files");
        var t=await r.json();
        if(Array.isArray(t)){_TREE=t;loadTree();}
    }catch(e){console.warn("reloadTree:",e);}
}

// Toast
function toast(msg,isErr){
    var t=document.getElementById("toast");
    if(!t){t=document.createElement("div");t.id="toast";t.style.cssText="position:fixed;bottom:16px;right:16px;padding:8px 16px;border-radius:10px;font-size:.8rem;z-index:9999;transition:opacity .3s;border:1px solid";document.body.appendChild(t);}
    t.textContent=msg;
    t.style.background=isErr?"rgba(255,107,107,.15)":"rgba(0,255,136,.12)";
    t.style.borderColor=isErr?"rgba(255,107,107,.4)":"rgba(0,255,136,.35)";
    t.style.color=isErr?"var(--red)":"var(--c2)";
    t.style.opacity="1"; clearTimeout(t._t);
    t._t=setTimeout(function(){t.style.opacity="0";},3000);
}
function esc(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");}

window.onerror = function(msg, src, line, col, err){
    var d = document.createElement("div");
    d.style.cssText = "position:fixed;top:0;left:0;right:0;background:#ff0000;color:#fff;padding:10px;z-index:99999;font-family:monospace;font-size:12px";
    d.textContent = "JS-FEHLER Zeile " + line + ": " + msg;
    document.body.appendChild(d);
    return false;
};
document.addEventListener("DOMContentLoaded",function(){
    try {
        console.log("DOMContentLoaded fired");
        console.log("_TREE length:", _TREE.length);
    rb    = document.getElementById("rb");
    tSt   = document.getElementById("tSt");
    term  = document.getElementById("term");
    tBody = document.getElementById("tBody");
    loadTree();
    setMode("plan");
    if(tSt) tSt.innerHTML='<span style="color:var(--c2)">&#9679; IDLE</span>';
    console.log("Lab OK");
    } catch(initErr) {
        var d = document.createElement("div");
        d.style.cssText = "position:fixed;top:0;left:0;right:0;background:#ff6600;color:#fff;padding:10px;z-index:99999;font-family:monospace;font-size:12px";
        d.textContent = "INIT-FEHLER: " + initErr.message;
        document.body.appendChild(d);
    }
});
</script>
</body>
</html>"""

    return render_template_string(
        template,
        tree_b64 = tree_b64,
        bot      = safe_bot,
        bot_i    = (bot_name or "R")[0],
        orch_ok  = orch_ok,
        ws       = ws_path
    )