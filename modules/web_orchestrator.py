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
orch_blueprint   = Blueprint("lab", __name__)
_active_missions = {}
_missions_lock   = threading.Lock()
# Plan-Sessions: plan_id → list of messages
_plan_sessions: dict = {}
_plan_lock = threading.Lock()
ALLOWED_EXT = {".json", ".txt", ".md", ".yaml", ".yml", ".csv", ".py", ".sh", ".log"}


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
        try:
            r = await asyncio.get_event_loop().run_in_executor(None, lambda: subprocess.run(
                [sys.executable, "-c", code], capture_output=True, text=True, timeout=30))
            return r.stdout.strip(), r.stderr.strip()
        except subprocess.TimeoutExpired:
            return "", "Timeout (>30s)"
        except Exception as e:
            return "", str(e)

    def validate_module(self, code):
        for kw, err in {"setup(app)": "Keine setup(app)", "CommandHandler": "Kein CommandHandler",
                        "async def": "Keine async-Funktion"}.items():
            if kw not in code: return False, err
        return True, "OK"

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
            try:
                self.log("🤖 LLM generiert Recherche-Code...", "info")
                raw = await self._llm([{"role":"system","content":AGENT_SYSTEM},
                    {"role":"user","content":f"Recherchiere fuer: {task}\nAusgabe: print('RESULT:',...)\nNur Code."}])
                code = extract_code(raw)
                self.log(f"✅ Code empfangen ({len(code.splitlines())} Zeilen) — führe aus...", "info")
                stdout, stderr = await self.run_code(code)
                research_result = stdout if stdout else f"Kein Output. Stderr: {stderr[:300]}"
                self.log(f"🔍 Recherche-Ergebnis:\n<pre>{html.escape(research_result[:1200])}</pre>", "result")
            except Exception as e:
                research_result = f"Fehlgeschlagen: {e}"
                self.log(f"⚠️ {research_result}", "warn")
            self.log("🔀 <b>Schritt 3/4</b> — Modul generieren...", "step")
            task = f"{original_task}\n\nRECHERCHE-ERGEBNIS:\n{research_result}"
            mode = "builder"

        if mode == "builder":
            step_n = "3/4" if original_task != task else "2/3"
            self.log(f"🔨 <b>Schritt {step_n}</b> — Telegram-Modul generieren...", "step")
            catalog  = get_module_catalog()
            if catalog:
                self.log(f"📦 Modul-Katalog geladen ({len(catalog.splitlines())} Einträge)", "info")
            messages = [{"role":"system","content":BUILDER_SYSTEM},
                        {"role":"user","content":
                         f"Erstelle ein vollstaendiges Telegram-Bot-Modul:\n\n{task}\n\n"
                         f"- Reiner Python-Code, keine Backticks\n- Vollstaendig implementiert\n"
                         f"- setup(app) mit allen CommandHandlern\n- Deutsche Ausgaben\n"
                         f"- chat_id IMMER os.getenv('CHAT_ID')\n\n{catalog}"}]
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
                    fname = f"{cmd_m.group(1)}.py" if cmd_m else f"auto_{self.task_id}.py"
                    prev  = "\n".join(code.splitlines()[:25])
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
    if not task: return jsonify({"error":"Keine Aufgabe"}), 400
    mid   = task_id_safe(task + str(datetime.now().timestamp()))
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
        return jsonify({"ok":True,"message":f"✅ Installiert als modules/{fname} — Neustart erforderlich!"})
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

    with _plan_lock:
        if plan_id not in _plan_sessions:
            _plan_sessions[plan_id] = []
        _plan_sessions[plan_id].append({"role": "user", "content": user_msg})
        messages = [{"role": "system", "content": PLAN_SYSTEM}] + list(_plan_sessions[plan_id][-20:])

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
                            json={"model":DS_MODEL,"messages":messages,"stream":True,
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
    with _plan_lock: _plan_sessions.pop(plan_id, None)
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

/* KI bar */
.kibar{display:none;padding:8px 14px;background:var(--bg3);
       border-top:1px solid rgba(167,139,250,.3);gap:8px;align-items:center;flex-shrink:0}
.kibar.show{display:flex}
.kii{flex:1;background:var(--bg2);border:1px solid rgba(167,139,250,.4);border-radius:8px;
     padding:7px 12px;color:var(--text);font-family:inherit;font-size:.78rem;outline:none}
.kii:focus{border-color:var(--purple)}.kii::placeholder{color:var(--sub)}

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
    clearTerm();
    if(rb) { rb.disabled=true; rb.innerHTML='<span class="spin"></span>Running'; }
    if(tSt) tSt.innerHTML='<span style="color:var(--cy)">&#9679; RUNNING</span>';
    updateModeBar("mission","Starte...");
    addL("$ "+esc(task),"step");

    fetch("/lab/run",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({task:task})
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
    } else {
        var pre=document.createElement("div"); pre.className="fview";
        pre.textContent=curFile.content; vb.innerHTML=""; vb.appendChild(pre);
        saveB.style.display="none"; kiTog.style.display="none";
        document.getElementById("kibar").classList.remove("show");
    }
}
function closeViewer(){
    hide("pV"); document.getElementById("panels").classList.remove("split");
    splitOpen=false; curFile=null;
    document.querySelectorAll(".tfile").forEach(function(el){el.classList.remove("act");});
    document.getElementById("kibar").classList.remove("show");
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
    document.getElementById("kibar").classList.toggle("show");
    if(document.getElementById("kibar").classList.contains("show")) document.getElementById("kii").focus();
}
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
            closeDiff(); document.getElementById("kibar").classList.remove("show");
            document.getElementById("kii").value="";
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