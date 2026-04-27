import os
import re
import json
import asyncio
import subprocess
import sys
import html
import hashlib
import httpx
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler

load_dotenv()

BOT_NAME   = os.getenv("BOT_NAME", "RICS")
DS_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DS_URL     = "https://api.deepseek.com/v1/chat/completions"
DS_MODEL   = "deepseek-chat"

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE   = os.path.join(PROJECT_DIR, "workspace")
MODULES_DIR = os.path.join(PROJECT_DIR, "modules")
os.makedirs(WORKSPACE, exist_ok=True)
os.makedirs(MODULES_DIR, exist_ok=True)

# ─────────────────────────────────────────────
# SYSTEM-PROMPT FÜR BUILDER-MODE
# Erzeugt vollständige, direkt installierbare Telegram-Module
# ─────────────────────────────────────────────
BUILDER_SYSTEM = """Du bist RICS, ein autonomer KI-Agent auf macOS.
Deine Aufgabe: Schreibe vollständige, sofort lauffähige Python-Module für einen Telegram-Bot.

REGELN (ABSOLUT PFLICHT):
1. Antworte NUR mit reinem Python-Code — kein Markdown, keine Backticks, keine Erklärungen.
2. Das Modul muss SOFORT importierbar und nutzbar sein.
3. ALLE Telegram-Imports müssen enthalten sein.
4. JEDE Funktion braucht vollständiges Fehlerhandling mit try/except.
5. Rückgaben immer als lesbarer deutscher Text formatiert.
6. setup(app) MUSS am Ende stehen und ALLE Handler registrieren.
7. Metadaten (description, category) für jede Handler-Funktion.

ERLAUBTE KATEGORIE-WERTE (wähle den passendsten):
- "KI"          → KI-Funktionen, Agenten, Modelle
- "Gedächtnis"  → Erinnerungen, Notizen, Wissen
- "Agenda"      → Kalender, Termine, Erinnerungen, Planung
- "Briefing"    → Zusammenfassungen, Tagesberichte
- "Jobs"        → Cronjobs, geplante Aufgaben
- "Monitor"     → Überwachung, Alerts, Status
- "Wetter"      → Wetterdaten, Forecast
- "Energie"     → Solar, Strom, Verbrauch
- "Autonom"     → Autonome Prozesse
- "Discord"     → Discord-Integration
- "Social"      → Social Media
- "Content"     → Inhalte, Medien, YouTube
- "Vision"      → Bildanalyse, Screenshots
- "Recherche"   → Websuche, Daten sammeln
- "System"      → Systemsteuerung, macOS
- "LLM"         → Sprachmodelle, APIs
- "Finance"     → Finanzen, PayPal, Preise

PFLICHT-STRUKTUR für jedes Modul:
```
import os, re, subprocess, json
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

async def main_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Argument-Parsing
    args = context.args or []
    sub = args[0].lower() if args else ""
    
    if sub == "heute":
        result = get_today()
    elif sub == "erstellen":
        result = create_item(args[1:])
    else:
        result = "Verfügbare Befehle: ..."
    
    await update.message.reply_text(result)

def get_today() -> str:
    try:
        # Implementierung
        return "Ergebnis"
    except Exception as e:
        return f"❌ Fehler: {e}"

def create_item(args: list) -> str:
    try:
        # Parameter aus args parsen
        params = {}
        for arg in args:
            if "=" in arg:
                k, v = arg.split("=", 1)
                params[k.strip()] = v.strip()
        # Implementierung
        return "✅ Erstellt"
    except Exception as e:
        return f"❌ Fehler: {e}"

main_handler.description = "Kurze Beschreibung"
main_handler.category = "Kategorie"

def setup(app):
    app.add_handler(CommandHandler("befehl", main_handler))
```

FÜR APPLESCRIPT/MACOS-MODULE:
- Nutze subprocess.run(['osascript', '-e', script], capture_output=True, text=True)
- Prüfe immer result.returncode == 0
- Zeige stderr bei Fehler an

FÜR WEB-MODULE:
- Nutze requests oder httpx
- Nutze BeautifulSoup für HTML-Parsing
- Timeout immer setzen (timeout=10)

FÜR SYSTEM-MODULE:
- Nutze subprocess, os, pathlib
- Prüfe Pfade und Berechtigungen

FÜR STATUS/BALANCE-MODULE (APIs mit Guthaben, Kontostand, Verbrauch):
- Berechne Verbrauch immer selbst: verbrauch = (aufgeladen + bonus) - gesamt
- Wenn kein tages-spezifischer Verbrauch verfügbar: nutze diese Näherung
- NIEMALS schreiben "API gibt Verbrauch nicht aus" — immer berechnen!
- Verwende dieses visuelle Design für Status-Ausgaben:
```
💎 [Titel]
￣￣￣￣￣￣￣￣￣￣￣￣￣
💰 Wert1: ...
📥 Wert2: ...
🎁 Wert3: ...
￣￣￣￣￣￣￣￣￣￣￣￣￣
📊 Verbrauch: ...
￣￣￣￣￣￣￣￣￣￣￣￣￣
📅 Stand: HH:MM:SS
```
- Trennlinie immer: "￣￣￣￣￣￣￣￣￣￣￣￣￣"
- Jeder Block (Guthaben / Verbrauch / Timestamp) durch Trennlinie abgeteilt

FÜR JOB-QUEUE-CALLBACKS (heartbeat, cron, run_repeating):
- NIEMALS context.job.chat_id verwenden — das existiert nur in CommandHandlern.
- Stattdessen immer: chat_id = os.getenv("TELEGRAM_CHAT_ID")
- Zeitwerte aus .env (z.B. PAYPAL_NIGHT_START) können ohne Minuten kommen ("22" statt "22:00").
  Immer beide Formate parsen:
  for fmt in ["%H:%M", "%H"]:
      try: t = datetime.strptime(val, fmt).time(); break
      except: continue
- chat_id immer exakt so laden (CHAT_ID ist der korrekte Key in dieser Umgebung):
  chat_id = os.getenv("CHAT_ID")
- NIEMALS os.getenv("TELEGRAM_CHAT_ID") verwenden — dieser Key existiert nicht.
- NIEMALS os.getenv("TELEGRAM_TOKEN") verwenden — der Bot-Token heißt hier TELEGRAM_TOKEN, aber für chat_id immer CHAT_ID.
- Immer prüfen: if not chat_id: logger.error(...); return

FÜR COMMAND-REGISTRIERUNG:
- Wenn die Mission explizite Command-Namen vorgibt (z.B. /wetteralarm_status, /wetteralarm_on), diese EXAKT als separate CommandHandler registrieren.
- NIEMALS als Subcommands eines einzelnen Handlers umbauen — das entspricht nicht der Mission.
- Jede Handler-Funktion bekommt ihren eigenen CommandHandler-Eintrag in setup(app).

FÜR PERSISTENTE DATEN (Listen, Dicts, Status die Neustarts überleben müssen):
- NIEMALS nur als globale Python-Variable im RAM speichern — geht bei Neustart verloren.
- Immer in einer JSON-Datei im Ordner memory/ persistieren.
- Beim Modulstart aus der JSON laden, bei jeder Änderung sofort zurückschreiben.
- Beispiel: pending_alerts nicht als _pending_alerts = [] sondern als memory/wetteralarm_pending.json

FÜR EXTERNE API-MODULE (KRITISCH — HALLUZINATIONS-SCHUTZ):
- NIEMALS Felder verwenden die du nicht mit Sicherheit kennst. Wenn du ein Feld nicht kennst → .get() mit Fallback.
- Deduplication NIEMALS über ein halluziniertes 'id'-Feld. Immer zusammengesetzten Key aus sicheren Feldern bauen:
  dedup_key = f"{item.get('field1','')}_{item.get('field2',0)}_{item.get('field3',0)}"
- Wenn ein RECHERCHE-ERGEBNIS vorliegt: Verwende NUR die Felder die dort tatsächlich aufgetaucht sind.
- Wenn kein Recherche-Ergebnis: Nutze nur Felder die in der offiziellen Dokumentation explizit genannt werden.
- Wenn ein RECHERCHE-ERGEBNIS vorliegt das einen funktionierenden Endpoint (HTTP 200) nennt: diesen verwenden.
- Wenn das Recherche-Ergebnis einen 401/403-Fehler zeigt: KEINEN kostenpflichtigen Endpoint verwenden,
  sondern den kostenlosen Fallback-Endpoint aus dem Recherche-Ergebnis nehmen.
- Bei API-Modulen ohne Recherche-Ergebnis: Absichern mit .get() und sinnvollem Fallback für JEDEN Feldzugriff.

FÜR API-MODULE MIT USER-INPUT (KRITISCH — robuste Eingabe-Behandlung):
- URL-Parameter NIEMALS in den URL-String f-stringen — bricht bei Umlauten, Leerzeichen, Sonderzeichen.
  FALSCH: client.get(f"{API}/search?q={user_input}&limit=5")
  RICHTIG: client.get(f"{API}/search", params={"q": user_input, "limit": 5})
  httpx/requests encoden über params={} automatisch korrekt.
- Wenn ein User-Argument ein freitextlicher Wert sein kann (Name, Ort, Titel, Begriff, der Leerzeichen enthalten darf), dann NIEMALS stur args[0]=A, args[1]=B nehmen.
  Stattdessen in dieser Reihenfolge parsen:
    1. Vom Ende strukturierte Tokens abschneiden (Zeit HH:MM, Datum YYYY-MM-DD, Zahlen, Flags).
    2. Im Rest expliziten Trenner suchen: " - ", " → ", " nach ", " bis ", " vs ".
    3. Erst dann heuristisch splitten (z.B. bei genau zwei Wörtern Wort/Wort).
    4. Wenn unklar: KEIN raten, sondern Fehlermeldung mit Trenner-Hinweis.
- Such-/List-Endpoints liefern oft gemischte Treffer-Typen (z.B. Treffer mit unterschiedlichem .get('type')).
  NIEMALS blind data[0] nehmen — könnte ein Typ ohne die benötigten Felder sein (z.B. ohne 'id').
  IMMER: Erst nach erwartetem type filtern, falls die API einen Filter-Parameter anbietet diesen mitsenden, dann im Code zusätzlich per .get('type') validieren bevor Felder gelesen werden.
- Wenn ein User-Lookup nichts findet, NIEMALS nur "nicht gefunden" antworten — IMMER Vorschläge anzeigen.
  Pattern: bei Nicht-Fund einen zweiten Such-Call (oft mit fuzzy=true oder weiteren Treffern) machen und die ersten Treffer-Namen als "Meintest du: a, b, c?" zurückgeben.
- Datum/Zeit für API-Calls: NIEMALS getrennte Strings senden ("date=2025-06-15" + "time=14:30"). Erst zu einem datetime-Objekt kombinieren, dann .isoformat() — die meisten APIs erwarten ISO-8601.
- User-Input mit `params={}` bedeutet auch: keine Notwendigkeit für manuelles urlencode/quote — wenn du quote() siehst, hast du wahrscheinlich den f-String-Pfad genommen → wechsle auf params={}.

DEFENSIVE FELD-ZUGRIFFE (KRITISCH — Anti-"'str' object has no attribute 'get'"):
- NIEMALS .get() auf einem Feld aufrufen ohne zu wissen ob es ein dict ist.
  JSON-APIs liefern oft Felder die je nach Variante String, Zahl, dict oder None sind.
  Beispiel: ein Feld kann bei API-Variante A ein einfacher Wert sein (String/Zahl), bei
  Variante B ein verschachteltes Objekt mit eigenen Unterfeldern.
- Wenn das RECHERCHE-ERGEBNIS einen Feldwert als String/Zahl zeigt, NIE im Code
  `obj["feldname"].get(...)` schreiben — das crasht sofort.
- IMMER mit isinstance() absichern wenn ein Feld komplex sein KÖNNTE:
    val = obj.get("feldname")
    if isinstance(val, dict):
        inner = val.get("subkey1") or val.get("subkey2")
    elif isinstance(val, str):
        inner = val
    else:
        inner = None
- Alternative Pattern fuer kompakten Code:
    def _safe_get(obj, *keys):
        # holt verschachtelte Felder, bricht sicher ab wenn ein Zwischenwert kein dict ist
        for k in keys:
            if not isinstance(obj, dict):
                return None
            obj = obj.get(k)
        return obj
- Wenn das Recherche-Ergebnis NUR den Endpoint nennt aber KEINE Response-Probe enthaelt:
  Im Modul defensiv programmieren — JEDEN Feldzugriff mit isinstance-Check oder _safe_get().
- Im try/except der Karten-/Block-Formatierung: NIEMALS schweigend "?" zurueckgeben und 5x den
  gleichen Fehler in der Ausgabe stapeln. Bei wiederholtem gleichen Fehler einmal melden, dann return.

RESILIENZ BEI HTTP-FEHLERN (KRITISCH — APIs flackern):
- Cloud-APIs (Caddy/Kubernetes/Cloudflare/AWS) liefern regelmaessig kurzzeitige 5xx
  (502 Bad Gateway, 503 Service Unavailable, 504 Gateway Timeout).
- Das fertige Modul MUSS bei 5xx und Timeouts AUTOMATISCH 2x nachversuchen, nicht sofort aufgeben.
- Pattern (kompakt, generisch fuer JEDE API):
    async def _api_get(client, url, params=None, max_retries=2):
        for attempt in range(max_retries + 1):
            try:
                resp = await client.get(url, params=params)
                if resp.status_code in (502, 503, 504):
                    if attempt < max_retries:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                resp.raise_for_status()
                return resp.json()
            except (httpx.TimeoutException, httpx.RequestError):
                if attempt < max_retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise
        return None
- Erst NACH dem Retry-Loop den User mit "❌ API-Fehler: 5xx" benachrichtigen.
- 4xx-Fehler (400, 401, 403, 404) NICHT wiederholen — die sind permanent (Bug, falscher Param, fehlender Key).

CODE-GRÖSSE (KRITISCH gegen Token-Limit-Abbruch):
- Halte das Modul KOMPAKT — Ziel: 100–250 Zeilen, Maximum 500.
- Wenn du nahe am Limit bist, weglassen statt abschneiden: weniger Hilfsfunktionen, kürzere Hilfetexte, weniger Defensive-Kommentare.
- Strings IN EINER Zeile oder mit Triple-Quotes ''' '''. NIE Backslash-Continuation für lange Strings.
- Lange URLs IMMER über params={} bauen, niemals als f-String über mehrere Zeilen.
- Bevor du eine weitere Hilfsfunktion hinzufuegst: pruefe ob das Modul schon alles Wesentliche hat.

NOCHMAL: Nur reiner Python-Code. Kein Markdown. Keine Backticks. Keine Kommentare außerhalb des Codes."""

# ─────────────────────────────────────────────
# SYSTEM-PROMPT FÜR AGENT-MODE
# Führt Recherchen aus, gibt RESULT: ... aus
# ─────────────────────────────────────────────
AGENT_SYSTEM = """Du bist RICS, ein autonomer KI-Agent.
Deine Aufgabe: Schreibe Python-Code der Daten recherchiert und das Ergebnis ausgibt.

REGELN (ABSOLUT PFLICHT):
1. Antworte NUR mit reinem Python-Code — kein Markdown, keine Backticks.
2. Nutze requests, BeautifulSoup, re oder duckduckgo_search für Recherche.
3. Das Ergebnis MUSS mit print("RESULT:", ...) ausgegeben werden.
4. Timeout immer setzen.
5. Fehlerhandling mit try/except.
6. KEIN Telegram-Code.
7. KEIN import von Telegram-Modulen.

BEISPIEL-STRUKTUR:
```
import requests
from bs4 import BeautifulSoup

try:
    r = requests.get("https://...", timeout=10, headers={"User-Agent": "Mozilla/5.0"})
    soup = BeautifulSoup(r.text, "html.parser")
    data = soup.find("...").text.strip()
    print("RESULT:", data)
except Exception as e:
    print("RESULT: Fehler:", e)
```

Nur reiner Python-Code. Keine Backticks. Keine Erklärungen."""


# ─────────────────────────────────────────────
# SYSTEM-PROMPT FÜR SCRIPT-MODE
# Erzeugt standalone Python-Skripte ohne Telegram-Abhängigkeit
# ─────────────────────────────────────────────
SCRIPT_SYSTEM = """Du bist eine KI, ein autonomer KI-Agent auf macOS.
Deine Aufgabe: Schreibe vollständige, sofort lauffähige Python-Skripte die lokal auf macOS ausgeführt werden.

REGEL 0 — METADATEN-HEADER (PFLICHT, EXAKT DIESES FORMAT, ALLERERSTE ZEILEN):
Die ersten 3 Zeilen MÜSSEN exakt so aussehen:
# FILENAME: <kurz_und_sprechend>.py
# REQUIREMENTS: paket1, paket2
# RUNNABLE: gui | terminal | button

Regeln zu den Headern:
- FILENAME: nur Kleinbuchstaben/Ziffern/Unterstriche, kein Umlaut. Beispiele: passwortmanager.py, ordner_aufraeumen.py
- REQUIREMENTS: kommagetrennte pip-Pakete. Wenn nur Stdlib → schreibe "REQUIREMENTS: keine"
  WICHTIG: NIEMALS tkinter, sqlite3, json, os, sys, re, subprocess etc. nennen — das ist Stdlib!
- RUNNABLE — exakt einer dieser drei Werte:
    gui      → Skript hat eine grafische Oberfläche (tkinter, PyQt, customtkinter).
               Doppelklick öffnet NUR das GUI-Fenster, kein Terminal.
    terminal → Skript braucht ein Terminal-Fenster (input(), print(), interaktive Eingaben).
               Doppelklick öffnet ein Terminal-Fenster mit dem Skript darin.
    button   → Skript läuft einmal durch und gibt Output zurück (Recherche, Berechnung, Backup).
               Kein Doppelklick-Wrapper nötig, nur "Skript ausführen"-Button.

Beispiel-Header für eine GUI-App:
# FILENAME: passwortmanager.py
# REQUIREMENTS: keine
# RUNNABLE: gui

SIGNAL-PARSING — wähle RUNNABLE basierend auf der Task-Beschreibung:
- Enthält "GUI-App", "GUI", "tkinter", "Fenster", "Doppelklick-App" → RUNNABLE: gui
- Enthält "Terminal", "Konsole", "interaktiv", "input()", "Eingabe" → RUNNABLE: terminal
- Enthält "Recherche", "ein Mal", "einmalig", "Backup", "Berechnung" → RUNNABLE: button

REGELN (ABSOLUT PFLICHT):
1. Nach den 3 Header-Zeilen: NUR reiner Python-Code — kein Markdown, keine Backticks.
2. KEIN Telegram-Code, KEINE Telegram-Imports.
3. Das Skript muss direkt mit 'python3 skript.py' ausführbar sein.
4. JEDE Funktion braucht vollständiges Fehlerhandling mit try/except.
5. Ergebnisse als lesbare deutsche Ausgabe mit print().
6. Für macOS-Interaktion: subprocess.run(['osascript', '-e', script], ...) nutzen.
7. Für Web-Zugriff: requests oder httpx mit timeout=15 nutzen.
8. Am Ende: if __name__ == '__main__': main() Aufruf.
9. GUI erlaubt: tkinter (Stdlib), PyQt6, customtkinter — wenn der User danach fragt oder eine GUI sinnvoll ist.

PFLICHT-STRUKTUR (nach den 3 Header-Zeilen):
import os, sys, re, json, subprocess
import requests  # falls Web-Zugriff nötig

def main():
    try:
        result = do_something()
        print(result)
    except Exception as e:
        print(f"Fehler: {e}")
        sys.exit(1)

def do_something():
    try:
        return "Ergebnis"
    except Exception as e:
        return f"❌ Fehler: {e}"

if __name__ == '__main__':
    main()

Nur reiner Python-Code. Kein Markdown. Keine Backticks. Keine Kommentare außerhalb des Codes."""

def task_id_safe(task: str) -> str:
    clean      = re.sub(r"[^a-z0-9_]", "_", task.lower())[:32]
    short_hash = hashlib.sha1(task.encode()).hexdigest()[:8]
    return f"{clean}_{short_hash}"


def extract_code(text: str) -> str:
    """Extrahiert reinen Python-Code aus LLM-Antworten (entfernt Markdown-Fences falls vorhanden)."""
    # Markdown-Fences entfernen falls das Modell sie trotzdem schreibt
    text = re.sub(r"```python\s*", "", text)
    text = re.sub(r"```\s*", "", text)
    # Thinking-Tags entfernen (DeepSeek-Reasoner)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    return text.strip()


def get_module_catalog() -> str:
    """
    Scannt das Projektverzeichnis (nicht modules/) nach .py-Dateien
    und extrahiert öffentliche async/sync Funktionen pro Modul.
    Gibt einen formatierten Katalog-String für den Builder-Prompt zurück.
    """
    skip = {"__init__", "setup", "bot", "orchestrator", "llm_client",
            "event_bus", "session_manager", "web_app", "brain",
            "proactive_brain", "self_reflection", "funktions_scan", "updater"}

    entries = []
    try:
        for fname in sorted(os.listdir(PROJECT_DIR)):
            if not fname.endswith(".py"):
                continue
            stem = fname[:-3]
            if stem in skip:
                continue
            fpath = os.path.join(PROJECT_DIR, fname)
            try:
                with open(fpath, encoding="utf-8", errors="ignore") as f:
                    src = f.read()
                # Top-level async def und def extrahieren
                fns = re.findall(r"^(?:async )?def (\w+)\s*\(", src, re.MULTILINE)
                # Interne Helper weglassen (Unterstriche)
                fns = [fn for fn in fns if not fn.startswith("_") and fn not in ("setup", "main")]
                if fns:
                    fn_list = ", ".join(fns[:8])  # Max 8 Funktionen zeigen
                    entries.append(f"  {fname:<25} → {fn_list}")
            except Exception:
                pass
    except Exception:
        pass

    if not entries:
        return ""

    catalog = (
        "VERFÜGBARE PROJEKTMODULE (importierbar im generierten Modul):\n"
        + "\n".join(entries)
        + "\n\n"
        "IMPORT-PATTERN (Pfad zum Projektverzeichnis):\n"
        "  import sys, os as _os\n"
        "  sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))\n"
        "  from Wetter import get_forecast   # Beispiel — passe Modul+Funktion an\n"
        "\n"
        "Nutze dieses Pattern wenn das neue Modul Funktionen aus bestehenden Modulen braucht.\n"
        "Importiere NUR was wirklich benötigt wird."
    )
    return catalog


class Orchestrator:
    def __init__(self, jarvis, update, context):
        self.jarvis  = jarvis
        self.update  = update
        self.context = context
        self.total_code = ""
        self.results    = []

    # ─────────────────────────────────────────
    # MODUS-ERKENNUNG (LLM-basiert)
    # agent   = nur Daten recherchieren / ausgeben
    # builder = Telegram-Modul erstellen
    # hybrid  = erst recherchieren, dann Modul bauen
    # ─────────────────────────────────────────
    async def detect_mode(self, task: str) -> str:
        # ── Schnelle Vorab-Prüfung: explizite Modul/Telegram-Keywords ──
        # Nur wenn diese Begriffe im Task auftauchen, ist BUILDER/HYBRID
        # überhaupt im Spiel. Sonst: lokales Skript oder Recherche.
        task_lower = task.lower()
        explicit_module_kw = [
            "telegram-modul", "telegram modul", "telegram-befehl", "telegram befehl",
            "/befehl", "bot-modul", "bot modul",
            "modul ", "modul:", "modul für", "modul fuer",
            "command-handler", "commandhandler",
            "als modul", "als telegram", "im bot",
        ]
        has_module_intent = any(k in task_lower for k in explicit_module_kw)

        if not has_module_intent:
            # Kein Modul-Hinweis → entscheide nur zwischen SCRIPT und AGENT
            prompt = (
                f"Analysiere diese Aufgabe und antworte NUR mit einem Wort:\n\n"
                f"AGENT  → Nur Daten recherchieren/ausgeben (einmalige Web-Recherche, kein User-Input nötig)\n"
                f"SCRIPT → Lokales Python-Programm/Tool/App, das der User mehrfach ausführt oder mit dem er interagiert\n\n"
                f"Aufgabe: {task}\n\n"
                f"Beispiele:\n"
                f"'Finde aktuellen Benzinpreis' → AGENT\n"
                f"'Was kostet der DAX heute' → AGENT\n"
                f"'Passwort-Manager mit GUI' → SCRIPT\n"
                f"'Tool um Ordner aufzuräumen' → SCRIPT\n"
                f"'Skript zum Backup machen' → SCRIPT\n"
                f"'Doppelklick-App für Notizen' → SCRIPT\n\n"
                f"Antworte NUR mit: AGENT oder SCRIPT"
            )
            try:
                result = (await self.llm_call([{"role":"user","content":prompt}])).strip().upper()
                if "AGENT" in result:  return "agent"
                return "script"
            except Exception:
                # Fallback: einmalige Recherche-Keywords → agent, sonst script
                research_kw = ["finde", "suche", "was kostet", "wie ist", "aktuell",
                               "wetter heute", "preis", "kurs"]
                if any(k in task_lower for k in research_kw): return "agent"
                return "script"

        # ── Modul explizit gewünscht: zwischen BUILDER und HYBRID entscheiden ──
        prompt = (
            f"Der User will ein Telegram-Bot-Modul. Antworte NUR mit einem Wort:\n\n"
            f"BUILDER → Modul ohne Web-Recherche (rein lokale Logik, AppleScript, Berechnungen)\n"
            f"HYBRID  → Modul das eine Web-API/Online-Daten nutzt (Wetter, Fahrplan, Preise...)\n\n"
            f"Aufgabe: {task}\n\n"
            f"Antworte NUR mit: BUILDER oder HYBRID"
        )
        try:
            result = (await self.llm_call([{"role":"user","content":prompt}])).strip().upper()
            if "HYBRID" in result: return "hybrid"
            return "builder"
        except Exception:
            api_kw = ["api", "online", "web", "abruf", "fahrplan", "wetter", "preis", "kurs"]
            if any(k in task_lower for k in api_kw): return "hybrid"
            return "builder"

    # ─────────────────────────────────────────
    # TELEGRAM LOG-HELPER
    # ─────────────────────────────────────────
    async def log(self, text: str):
        safe = html.escape(str(text))
        return await self.context.bot.send_message(
            chat_id=self.update.effective_chat.id,
            text=safe,
            parse_mode="HTML"
        )

    # ─────────────────────────────────────────
    # LLM-CALL — DeepSeek API, Ollama als Notfall
    # ─────────────────────────────────────────
    async def llm_call(self, messages: list, use_json: bool = False) -> str:
        """
        Ruft DeepSeek API auf.
        Fällt auf Ollama zurück nur wenn API nicht verfügbar oder Rate Limit.
        """
        # ── 1. DeepSeek API ──
        if DS_API_KEY:
            try:
                payload = {
                    "model":       DS_MODEL,
                    "messages":    messages,
                    "stream":      False,
                    "max_tokens":  4096,
                    "temperature": 0.2,  # Niedrig für Code-Generierung
                }
                if use_json:
                    payload["response_format"] = {"type": "json_object"}

                async with httpx.AsyncClient(timeout=120) as client:
                    response = await client.post(
                        DS_URL,
                        headers={
                            "Authorization": f"Bearer {DS_API_KEY}",
                            "Content-Type":  "application/json",
                        },
                        json=payload
                    )

                if response.status_code == 429:
                    raise Exception("DeepSeek Rate Limit")
                if response.status_code == 402:
                    raise Exception("DeepSeek Guthaben leer")
                if response.status_code != 200:
                    raise Exception(f"DeepSeek HTTP {response.status_code}")

                content = response.json()["choices"][0]["message"]["content"].strip()
                if use_json:
                    content = re.sub(r"```json|```", "", content).strip()
                    match   = re.search(r'\{.*\}', content, re.DOTALL)
                    return json.loads(match.group() if match else content)
                return content

            except Exception as e:
                await self.log(f"⚠️ DeepSeek Fehler: {e} → Ollama Notfall-Fallback")

        # ── 2. Ollama Notfall-Fallback ──
        try:
            import ollama
            ollama_model = os.getenv("OLLAMA_MODEL", "qwen3:8b")
            loop = asyncio.get_event_loop()
            kwargs = {"model": ollama_model, "messages": messages}
            if use_json:
                kwargs["format"] = "json"
            res     = await loop.run_in_executor(None, lambda: ollama.chat(**kwargs))
            content = res["message"]["content"].strip()
            content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
            if use_json:
                match = re.search(r'\{.*\}', content, re.DOTALL)
                return json.loads(match.group() if match else content)
            return content
        except Exception as e:
            raise Exception(f"Alle LLM-Provider fehlgeschlagen: {e}")

    # ─────────────────────────────────────────
    # BRAIN-KONTEXT
    # ─────────────────────────────────────────
    async def get_brain_context(self, task: str, mode: str) -> str:
        if mode == "builder":
            return "Builder-Modus: Kein Brain-Kontext nötig."
        context_parts = []
        brain = self.context.application.bot_data.get("brain")
        if brain:
            try:
                historical = brain.get_historical(task)
                if historical and historical != "KEINE DATEN":
                    context_parts.append(f"Brain-Historie: {historical}")
            except:
                pass
        if self.jarvis and self.jarvis.memory:
            try:
                past = self.jarvis.memory.search_user(f"MISSION: {task}")
                if past:
                    context_parts.append(f"Frühere Missionen:\n{past}")
            except:
                pass
        return "\n\n".join(context_parts) if context_parts else "Keine Vorgeschichte."

    # ─────────────────────────────────────────
    # CODE AUSFÜHREN (nur Agent-Mode)
    # ─────────────────────────────────────────
    async def run_agent_code(self, code: str) -> tuple[str, str]:
        """Führt Recherche-Code in Subprocess aus. Gibt (stdout, stderr) zurück."""
        # load_dotenv() immer voranstellen damit .env Variablen verfügbar sind
        dotenv_header = "from dotenv import load_dotenv; load_dotenv()\n"
        if "load_dotenv" not in code:
            code = dotenv_header + code
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    [sys.executable, "-c", code],
                    capture_output=True,
                    text=True,
                    timeout=30
                )
            )
            return result.stdout.strip(), result.stderr.strip()
        except subprocess.TimeoutExpired:
            return "", "Timeout: Code lief länger als 30 Sekunden"
        except Exception as e:
            return "", str(e)

    # ─────────────────────────────────────────
    # MODUL-CODE VALIDIEREN (Builder-Mode)
    # ─────────────────────────────────────────
    def validate_module_code(self, code: str) -> tuple[bool, str]:
        """Prüft ob generierter Code eine valide Modul-Struktur hat."""
        checks = {
            "setup(app)":         "Keine setup(app)-Funktion gefunden",
            "CommandHandler":     "Kein CommandHandler gefunden",
            "async def":          "Keine async Handler-Funktion gefunden",
            ".description":       "Keine Metadaten (.description) gefunden",
        }
        for keyword, error in checks.items():
            if keyword not in code:
                return False, error
        return True, "OK"

    # ─────────────────────────────────────────
    # LOGIK-REVIEW (Builder-Mode)
    # Zweiter LLM-Pass der Halluzinationen und Logik-Fehler prüft
    # ─────────────────────────────────────────
    async def review_module_code(self, code: str, task: str) -> tuple[bool, str]:
        """
        Prüft generierten Code auf Logik-Fehler, halluzinierte API-Felder
        und kaputte Dedup-Logik. Gibt (ok, feedback) zurück.
        """
        review_prompt = (
            f"Aufgabe war: {task}\n\n"
            f"Generierter Code:\n{code[:3000]}\n\n"
            f"WICHTIGE KONTEXT-REGELN (nicht als Fehler melden!):\n"
            f"- OWM data/2.5/weather ist korrekt und gewollt. Warnungen werden aus Wettercodes abgeleitet — kein Fehler!\n"
            f"- data/3.0/onecall und data/2.5/onecall sind GESPERRT — falls verwendet: Fehler melden.\n"
            f"- chat_id muss über os.getenv('CHAT_ID') geladen werden — TELEGRAM_CHAT_ID ist falsch.\n"
            f"- Dedup über zusammengesetzte Keys aus echten Feldern (z.B. id_main_description) ist korrekt.\n\n"
            f"Prüfe NUR folgende echte Probleme:\n"
            f"1. Kommt der String '3.0/onecall' oder '2.5/onecall' irgendwo im Code vor? Auch als zweiter Call nach einem ersten erfolgreichen Call — IMMER gesperrt, IMMER Fehler melden!\n"
            f"2. Wird TELEGRAM_CHAT_ID statt CHAT_ID verwendet?\n"
            f"3. Werden Daten nur im RAM gehalten statt in memory/ als JSON persistiert?\n"
            f"4. Fehlt setup(app) oder sind Commands falsch registriert?\n"
            f"5. Werden API-Felder verwendet die nicht im Recherche-Ergebnis vorkamen?\n\n"
            f"Antworte NUR mit diesem JSON (kein Markdown, keine Backticks):\n"
            f"{{\"ok\": true/false, \"issues\": [\"Problem 1\", \"Problem 2\"]}}\n"
            f"ok=true wenn keine Probleme gefunden. ok=false + issues wenn Probleme vorhanden."
        )
        try:
            result = await self.llm_call([{"role": "user", "content": review_prompt}], use_json=True)
            if isinstance(result, dict):
                ok = result.get("ok", True)
                issues = result.get("issues", [])
                if not ok and issues:
                    return False, " | ".join(issues)
            return True, "OK"
        except Exception as e:
            logger.warning(f"Review-Pass fehlgeschlagen (ignoriert): {e}")
            return True, "OK"  # Bei Fehler im Review: nicht blockieren

    # ─────────────────────────────────────────
    # HAUPTMETHODE
    # ─────────────────────────────────────────
    async def execute_mission(self, task: str):
        self.context.application.bot_data["mission_running"] = True
        original_task = task
        mode = await self.detect_mode(task)

        mode_labels = {"builder": "🔨 BUILDER", "agent": "🔍 AGENT", "hybrid": "🔀 HYBRID", "script": "📝 SCRIPT"}
        await self.log(
            f"🚀 <b>MISSION START</b>\n"
            f"📋 Task: {task}\n"
            f"⚡ Modus: <b>{mode_labels.get(mode, mode.upper())}</b>\n"
            f"🧠 LLM: DeepSeek API {'(Ollama Fallback bereit)' if not DS_API_KEY else ''}"
        )

        brain_context = await self.get_brain_context(task, mode)

        # ─── HYBRID MODE: Erst recherchieren, dann Modul bauen ───
        if mode == "hybrid":
            await self.log("🔀 <b>Hybrid Schritt 1/2</b> — API recherchieren...")
            research_messages = [
                {"role": "system", "content": AGENT_SYSTEM},
                {"role": "user",   "content": (
                    f"PFLICHT: Erste Zeilen im Code müssen immer sein:\n"
                    f"from dotenv import load_dotenv; load_dotenv()\n"
                    f"Damit sind alle .env Variablen verfügbar.\n\n"
                    f"Recherchiere für folgende Aufgabe:\n{task}\n\n"
                    f"Finde heraus:\n"
                    f"- Welche öffentliche API/URL kann genutzt werden?\n"
                    f"- Wie sieht ein konkreter API-Call aus?\n"
                    f"- Welche Parameter/Keys sind nötig?\n"
                    f"PFLICHT BEI API-CALLS:\n"
                    f"- Prüfe den HTTP-Statuscode: nur 200 = funktioniert. Bei 401/403/404: anderen Endpoint suchen!\n"
                    f"- Gib den Statuscode immer mit print() aus damit er sichtbar ist.\n"
                    f"- Wenn ein Endpoint 401/403 zurückgibt: sofort Fallback auf kostenlosen Endpoint testen.\n"
                    f"- Nur einen Endpoint als RESULT ausgeben der tatsächlich 200 zurückgegeben hat.\n"
                    f"Schreibe Python-Code der die API testet und mit print('RESULT:', ...) ausgibt.\n"
                    f"PFLICHT: Gib die komplette Response-Struktur aus (print die Keys/Felder).\n"
                    f"Verwende im Modul NUR Felder die tatsächlich im Response vorhanden sind — niemals erfinden.\n"
                    f"Nur reiner Python-Code, keine Backticks."
                )}
            ]
            try:
                raw_research   = await self.llm_call(research_messages)
                research_code  = extract_code(raw_research)
                stdout, stderr = await self.run_agent_code(research_code)
                research_result = stdout if stdout else f"Kein Output. Stderr: {stderr[:300]}"
                await self.log(f"🔍 API-Recherche:\n{html.escape(research_result[:600])}")
            except Exception as e:
                research_result = f"Recherche fehlgeschlagen: {e}"
                await self.log(f"⚠️ {research_result}")

            # Recherche-Ergebnis in Builder-Task einbauen
            await self.log("🔀 <b>Hybrid Schritt 2/2</b> — Modul generieren...")
            task = (
                f"{original_task}\n\n"
                f"RECHERCHE-ERGEBNIS (nutze diese API-Infos direkt im Modul):\n"
                f"{research_result}"
            )
            mode = "builder"

        # ─── BUILDER MODE: Telegram-Modul generieren ───
        if mode == "builder":
            await self.log("🔨 Generiere vollständiges Telegram-Modul...")

            module_catalog = get_module_catalog()
            messages = [
                {"role": "system", "content": BUILDER_SYSTEM},
                {"role": "user",   "content": (
                    f"Erstelle ein vollständiges Telegram-Bot-Modul für folgende Aufgabe:\n\n"
                    f"{task}\n\n"
                    f"WICHTIG:\n"
                    f"- Reiner Python-Code, KEINE Backticks, KEIN Markdown\n"
                    f"- Vollständig implementiert, nicht nur Stubs\n"
                    f"- Alle Funktionen komplett ausimplementiert\n"
                    f"- setup(app) mit allen CommandHandlern am Ende\n"
                    f"- Deutsche Ausgaben\n\n"
                    + (f"{module_catalog}\n" if module_catalog else "")
                )}
            ]

            max_attempts = 2
            for attempt in range(1, max_attempts + 1):
                try:
                    raw = await self.llm_call(messages)
                    code = extract_code(raw)

                    # Validierung
                    valid, reason = self.validate_module_code(code)
                    if not valid and attempt < max_attempts:
                        await self.log(f"⚠️ Versuch {attempt}: {reason} — Nachbessern...")
                        messages.append({"role": "assistant", "content": raw})
                        messages.append({"role": "user", "content": (
                            f"Fehler: {reason}\n"
                            f"Bitte korrigiere den Code. Stelle sicher dass enthalten ist:\n"
                            f"- setup(app) Funktion\n"
                            f"- CommandHandler\n"
                            f"- async def Handler-Funktion\n"
                            f"- .description Metadaten\n"
                            f"Nur reiner Python-Code, keine Backticks."
                        )})
                        continue

                    # Harte Code-Checks (kein LLM nötig, nur generische Regeln)
                    hard_issues = []
                    if "TELEGRAM_CHAT_ID" in code:
                        hard_issues.append("Falscher Key TELEGRAM_CHAT_ID — muss CHAT_ID heißen!")
                    if hard_issues and attempt < max_attempts:
                        feedback = " | ".join(hard_issues)
                        await self.log(f"🚫 Hard-Check Versuch {attempt}: {feedback} — Korrigiere...")
                        messages.append({"role": "assistant", "content": raw})
                        messages.append({"role": "user", "content": (
                            f"KRITISCHE FEHLER:\n{feedback}\n\n"
                            f"Nur reiner Python-Code, keine Backticks."
                        )})
                        continue

                    # Logik-Review (Halluzinations-Check)
                    review_ok, review_feedback = await self.review_module_code(code, task)
                    if not review_ok and attempt < max_attempts:
                        await self.log(f"🔍 Review Versuch {attempt}: {review_feedback} — Korrigiere...")
                        messages.append({"role": "assistant", "content": raw})
                        messages.append({"role": "user", "content": (
                            f"Der Code hat folgende Logik-Probleme:\n{review_feedback}\n\n"
                            f"Bitte korrigiere. Nur Felder verwenden die sicher existieren, "
                            f"Dedup über zusammengesetzte Keys (nie halluziniertes \'id\'-Feld).\n"
                            f"Nur reiner Python-Code, keine Backticks."
                        )})
                        continue

                    # Code speichern
                    self.total_code = code
                    task_id = task_id_safe(task)
                    workspace_path = os.path.join(WORKSPACE, f"auto_{task_id}.py")
                    with open(workspace_path, "w", encoding="utf-8") as f:
                        f.write(code)

                    self.context.bot_data[f"code_{task_id}"]    = code
                    self.context.bot_data[f"results_{task_id}"] = [f"Modul generiert: {workspace_path}"]

                    # Vorschau
                    preview_lines = code.split("\n")[:20]
                    preview = "\n".join(preview_lines)
                    await self.log(
                        f"✅ <b>MODUL GENERIERT</b> ({len(code.split(chr(10)))} Zeilen)\n\n"
                        f"<pre>{html.escape(preview)}...</pre>"
                    )

                    self.context.application.bot_data["mission_running"] = False

                    keyboard = [[
                        InlineKeyboardButton("✅ Modul installieren", callback_data=f"evolve_{task_id}"),
                        InlineKeyboardButton("👀 Code anzeigen",      callback_data=f"showcode_{task_id}")
                    ]]
                    await self.context.bot.send_message(
                        chat_id=self.update.effective_chat.id,
                        text=(
                            f"🏁 <b>MISSION ERFOLGREICH</b>\n"
                            f"📦 Modul bereit zur Installation.\n"
                            f"Klicke <b>Modul installieren</b> um es zu aktivieren."
                        ),
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        parse_mode="HTML"
                    )
                    return

                except Exception as e:
                    await self.log(f"❌ Versuch {attempt} fehlgeschlagen: {e}")

            await self.log("❌ Modul-Generierung fehlgeschlagen nach allen Versuchen.")

        # ─── SCRIPT MODE: Lokales Python-Skript generieren ───
        if mode == "script":
            await self.log("📝 Generiere lokales Python-Skript...")

            messages = [
                {"role": "system", "content": SCRIPT_SYSTEM},
                {"role": "user",   "content": (
                    f"Erstelle ein vollständiges lokales Python-Skript für folgende Aufgabe:\n\n"
                    f"{task}\n\n"
                    f"WICHTIG:\n"
                    f"- Reiner Python-Code, KEINE Backticks, KEIN Markdown\n"
                    f"- KEIN Telegram-Code\n"
                    f"- Vollständig implementiert, sofort ausführbar\n"
                    f"- Deutsche Ausgaben\n"
                    f"- if __name__ == '__main__': main() am Ende"
                )}
            ]

            max_attempts = 2
            for attempt in range(1, max_attempts + 1):
                try:
                    raw  = await self.llm_call(messages)
                    code = extract_code(raw)

                    # Skript speichern
                    task_id       = task_id_safe(task)
                    script_name   = f"script_{task_id}.py"
                    script_path   = os.path.join(WORKSPACE, script_name)
                    with open(script_path, "w", encoding="utf-8") as f:
                        f.write(code)

                    self.context.bot_data[f"code_{task_id}"] = code

                    preview_lines = code.split("\n")[:20]
                    preview       = "\n".join(preview_lines)
                    await self.log(
                        f"✅ <b>SKRIPT GENERIERT</b> ({len(code.split(chr(10)))} Zeilen)\n\n"
                        f"<pre>{html.escape(preview)}...</pre>"
                    )

                    self.context.application.bot_data["mission_running"] = False

                    keyboard = [[
                        InlineKeyboardButton("▶️ Skript ausführen", callback_data=f"runscript_{task_id}"),
                        InlineKeyboardButton("👀 Code anzeigen",    callback_data=f"showcode_{task_id}")
                    ]]
                    await self.context.bot.send_message(
                        chat_id=self.update.effective_chat.id,
                        text=(
                            f"🏁 <b>SKRIPT BEREIT</b>\n"
                            f"📁 <code>{html.escape(script_path)}</code>\n\n"
                            f"Klicke <b>Skript ausführen</b> um es zu starten, Sir."
                        ),
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        parse_mode="HTML"
                    )
                    return

                except Exception as e:
                    await self.log(f"❌ Versuch {attempt} fehlgeschlagen: {e}")
                    if attempt < max_attempts:
                        messages.append({"role": "assistant", "content": raw if 'raw' in locals() else ""})
                        messages.append({"role": "user", "content": f"Fehler: {e}\nBitte korrigiere. Nur reiner Python-Code."})

            self.context.application.bot_data["mission_running"] = False
            await self.log("❌ Skript-Generierung fehlgeschlagen nach allen Versuchen.")
            return

        # ─── AGENT MODE: Recherche-Code ausführen ───
        else:
            await self.log("🔍 Starte Recherche...")

            messages = [
                {"role": "system", "content": AGENT_SYSTEM},
                {"role": "user",   "content": (
                    f"Recherche-Aufgabe: {task}\n"
                    f"Kontext: {brain_context}\n\n"
                    f"Schreibe Python-Code der die Aufgabe löst und das Ergebnis mit print('RESULT:', ...) ausgibt.\n"
                    f"Nur reiner Python-Code, keine Backticks."
                )}
            ]

            max_attempts = 3
            for attempt in range(1, max_attempts + 1):
                try:
                    raw  = await self.llm_call(messages)
                    code = extract_code(raw)

                    await self.log(f"⚡ Ausführen (Versuch {attempt})...")
                    stdout, stderr = await self.run_agent_code(code)

                    if stdout and "RESULT:" in stdout:
                        result_text = stdout.split("RESULT:", 1)[1].strip()
                        self.results.append(result_text)

                        task_id = task_id_safe(task)
                        self.context.bot_data[f"results_{task_id}"] = self.results

                        self.context.application.bot_data["mission_running"] = False
                        await self.log(f"✅ <b>ERGEBNIS:</b>\n{html.escape(result_text[:2000])}")
                        return

                    elif stderr:
                        await self.log(f"⚠️ Fehler (Versuch {attempt}): {stderr[:300]}")
                        if attempt < max_attempts:
                            messages.append({"role": "assistant", "content": raw})
                            messages.append({"role": "user", "content": (
                                f"Fehler beim Ausführen:\n{stderr}\n\n"
                                f"Korrigiere den Code. Nur reiner Python-Code."
                            )})
                    else:
                        await self.log(f"⚠️ Kein RESULT in Output (Versuch {attempt})")

                except Exception as e:
                    await self.log(f"❌ Versuch {attempt}: {e}")

            self.context.application.bot_data["mission_running"] = False
            await self.log("❌ Recherche nach allen Versuchen ohne Ergebnis.")


# ─────────────────────────────────────────────
# CALLBACKS
# ─────────────────────────────────────────────
async def runscript_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Führt ein gespeichertes lokales Skript aus und schickt den Output."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("▶️ Skript wird ausgeführt...")

    task_id     = query.data.replace("runscript_", "")
    script_path = os.path.join(WORKSPACE, f"script_{task_id}.py")

    if not os.path.exists(script_path):
        return await query.edit_message_text("❌ Skript nicht gefunden.")

    try:
        result = subprocess.run(
            [sys.executable, script_path],
            capture_output=True, text=True, timeout=60
        )
        output = result.stdout.strip() or result.stderr.strip() or "Kein Output."
        output = output[:3500]
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"📋 <b>Skript-Output:</b>\n<pre>{html.escape(output)}</pre>",
            parse_mode="HTML"
        )
    except subprocess.TimeoutExpired:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⏱️ Skript Timeout (>60s)"
        )
    except Exception as e:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Fehler beim Ausführen: {html.escape(str(e))}"
        )


async def evolve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⚙️ Modul wird installiert...")

    task_id = query.data.replace("evolve_", "")
    code    = context.bot_data.get(f"code_{task_id}")
    if not code:
        return await query.edit_message_text("❌ Kein Code gefunden.")

    # Dateiname aus CommandHandler("befehl", ...) im Code ableiten
    cmd_match = re.search(r'CommandHandler\(["\'](\w+)["\']', code)
    if cmd_match:
        module_filename = f"{cmd_match.group(1)}.py"
    else:
        module_filename = f"auto_{task_id}.py"

    workspace_path = os.path.join(WORKSPACE, f"auto_{task_id}.py")
    module_path    = os.path.join(MODULES_DIR, module_filename)

    try:
        # Falls nicht mehr im Workspace, direkt schreiben
        if not os.path.exists(workspace_path):
            with open(module_path, "w", encoding="utf-8") as f:
                f.write(code)
        else:
            os.rename(workspace_path, module_path)
    except Exception as e:
        return await query.edit_message_text(f"❌ Fehler: {e}")

    keyboard = [[InlineKeyboardButton("🔄 Bot neu starten", callback_data="system_restart")]]
    await query.edit_message_text(
        text=(
            f"✅ <b>MODUL INSTALLIERT</b>\n"
            f"📁 {html.escape(os.path.basename(module_path))}\n\n"
            f"Neustart erforderlich um das Modul zu aktivieren, Sir."
        ),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def showcode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Zeigt generierten Code zur Vorschau."""
    query = update.callback_query
    await query.answer()
    task_id = query.data.replace("showcode_", "")
    code    = context.bot_data.get(f"code_{task_id}", "Kein Code gefunden.")
    # In Chunks senden falls zu lang
    chunks = [code[i:i+3500] for i in range(0, len(code), 3500)]
    for i, chunk in enumerate(chunks[:3]):  # Max 3 Nachrichten
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"<pre>{html.escape(chunk)}</pre>",
            parse_mode="HTML"
        )


async def system_restart_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("🚀 <b>REBOOT...</b> Gleich zurück, Sir.", parse_mode="HTML")
    os._exit(42)


async def mission_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jarvis = context.application.bot_data.get("jarvis")
    if not context.args:
        return await update.message.reply_text(
            "Sir, was ist die Mission?\n\n"
            "Beispiele:\n"
            "`/mission Finde aktuellen Benzinpreis`\n"
            "`/mission Erstelle ein Modul für den macOS Kalender`",
            parse_mode="Markdown"
        )
    orch = Orchestrator(jarvis, update, context)
    await orch.execute_mission(" ".join(context.args))


# ─────────────────────────────────────────────
# METADATEN & SETUP
# ─────────────────────────────────────────────
mission_handler.description = "Autonome Mission: Recherche oder Modul-Erstellung via DeepSeek API"
mission_handler.category    = "KI"


def setup(app):
    app.add_handler(CommandHandler("mission",         mission_handler))
    app.add_handler(CallbackQueryHandler(evolve_callback,          pattern="^evolve_"))
    app.add_handler(CallbackQueryHandler(showcode_callback,        pattern="^showcode_"))
    app.add_handler(CallbackQueryHandler(runscript_callback,       pattern="^runscript_"))
    app.add_handler(CallbackQueryHandler(system_restart_callback,  pattern="^system_restart$"))