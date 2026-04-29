#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hotline.py — Lokaler Echtzeit-Trigger für RICS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Pollt alle 2 Sekunden memory/hotline.json auf neue Events.
Kein Internet, kein API-Call, kein Spam.

Externe Skripte schreiben einfach einen Eintrag:
    python3 -c "
    import json, os
    f = 'memory/hotline.json'
    data = json.load(open(f)) if os.path.exists(f) else []
    data.append({'msg': 'Solar unter 100W', 'icon': '☀️'})
    json.dump(data, open(f, 'w'))
    "

Oder per Bash-Shortcut (siehe /hotline_help):
    echo '{"msg": "Rover sieht Bewegung", "icon": "🤖"}' | python3 modules/hotline.py --push

Commands:
    /hotline        → Status + letzter Event
    /hotline_help   → Zeigt Bash-Snippet zum Pushen
"""

import os
import sys
import json
import logging
from datetime import datetime
from dotenv import load_dotenv
from telegram.ext import ContextTypes, CommandHandler

load_dotenv()

log = logging.getLogger(__name__)

# ── Pfade ──────────────────────────────────────────────────────────────────────
_THIS   = os.path.dirname(os.path.abspath(__file__))
PROJECT = _THIS if not _THIS.endswith("modules") else os.path.dirname(_THIS)

HOTLINE_FILE  = os.path.join(PROJECT, "memory", "hotline.json")
CHAT_ID       = os.getenv("CHAT_ID", "")
POLL_INTERVAL = 2  # Sekunden

NIGHT_START = int(os.getenv("PAYPAL_NIGHT_START", "22"))
NIGHT_END   = int(os.getenv("PAYPAL_NIGHT_END",   "8"))

# ── Zustand ────────────────────────────────────────────────────────────────────
_last_event: dict = {}


# ══════════════════════════════════════════════════════════════════════════════
# DATEI-HELFER
# ══════════════════════════════════════════════════════════════════════════════

def _read() -> list:
    if not os.path.exists(HOTLINE_FILE):
        return []
    try:
        with open(HOTLINE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write(data: list):
    os.makedirs(os.path.dirname(HOTLINE_FILE), exist_ok=True)
    with open(HOTLINE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _web_push(msg: str):
    try:
        from modules.web_app import web_push
        web_push(msg)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# POLLING JOB (läuft alle 2 Sekunden via job_queue)
# ══════════════════════════════════════════════════════════════════════════════

async def _poll(context: ContextTypes.DEFAULT_TYPE):
    global _last_event

    stunde = datetime.now().hour
    if NIGHT_START <= stunde or stunde < NIGHT_END:
        return

    entries = _read()
    if not entries:
        return

    pending   = [e for e in entries if not e.get("processed", False)]
    processed = [e for e in entries if e.get("processed", False)]

    if not pending:
        return

    for entry in pending:
        icon = entry.get("icon", "🔔")
        msg  = entry.get("msg", "").strip()
        src  = entry.get("source", "")
        ts   = entry.get("ts", datetime.now().strftime("%H:%M:%S"))

        if not msg:
            entry["processed"] = True
            continue

        source_line = f"\n_von: {src}_" if src else ""
        text = f"{icon} *Hotline* `[{ts}]`\n{msg}{source_line}"

        try:
            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=text,
                parse_mode="Markdown"
            )
            _web_push(f"{icon} {msg}")
            _last_event = {"msg": msg, "icon": icon, "ts": ts}
            log.info(f"Hotline gesendet: {msg[:60]}")
        except Exception as e:
            log.warning(f"Hotline send-Fehler: {e}")

        entry["processed"] = True

    # Nur die letzten 50 verarbeiteten behalten (kein unbegrenztes Wachstum)
    all_done = processed + [e for e in pending if e.get("processed")]
    _write(all_done[-50:])


# ══════════════════════════════════════════════════════════════════════════════
# COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

async def hotline_status(update, context: ContextTypes.DEFAULT_TYPE):
    """Zeigt Hotline-Status und letzten Event."""
    entries = _read()
    pending = [e for e in entries if not e.get("processed", False)]

    if _last_event:
        last = f"Letzter Event: {_last_event.get('icon','')} {_last_event.get('msg','')[:60]} `[{_last_event.get('ts','')}]`"
    else:
        last = "Noch kein Event empfangen."

    msg = (
        f"📡 *Hotline Status*\n"
        f"──────────────────\n"
        f"Polling: alle {POLL_INTERVAL}s\n"
        f"Datei: `memory/hotline.json`\n"
        f"Ausstehend: {len(pending)} Events\n"
        f"──────────────────\n"
        f"{last}\n\n"
        f"_/hotline\\_help für Bash-Snippet_"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def hotline_help(update, context: ContextTypes.DEFAULT_TYPE):
    """Zeigt Bash-Snippet zum Pushen von Events."""
    snippet = (
        "📡 *Hotline — Event pushen*\n\n"
        "Minimaler Push per Python:\n"
        "```python\n"
        "import json, os\n"
        "f = '/Users/rics/qwen-bot/memory/hotline.json'\n"
        "d = json.load(open(f)) if os.path.exists(f) else []\n"
        "d.append({'msg': 'Deine Nachricht', 'icon': '🔔'})\n"
        "json.dump(d, open(f,'w'))\n"
        "```\n\n"
        "Optionale Felder:\n"
        "`icon` — Emoji (Standard: 🔔)\n"
        "`source` — Herkunft z.B. `solar`, `rover`\n"
        "`ts` — Zeitstempel (wird automatisch gesetzt wenn leer)\n\n"
        "Vom Terminal direkt:\n"
        "```bash\n"
        "python3 modules/hotline.py --push 'Solar 50W' '☀️' 'solar'\n"
        "```"
    )
    await update.message.reply_text(snippet, parse_mode="Markdown")


hotline_status.description = "Hotline Status + letzter Event"
hotline_status.category    = "Monitor"
hotline_help.description   = "Bash-Snippet für Hotline-Push"
hotline_help.category      = "Monitor"


# ══════════════════════════════════════════════════════════════════════════════
# SETUP (wird von bot.py automatisch aufgerufen)
# ══════════════════════════════════════════════════════════════════════════════

def setup(app):
    if app.job_queue:
        app.job_queue.run_repeating(
            _poll,
            interval=POLL_INTERVAL,
            first=5,
            name="hotline_poll"
        )
        log.info(f"Hotline: Polling alle {POLL_INTERVAL}s aktiv")

    app.add_handler(CommandHandler("hotline",      hotline_status))
    app.add_handler(CommandHandler("hotline_help", hotline_help))


# ══════════════════════════════════════════════════════════════════════════════
# CLI-MODUS: python3 hotline.py --push "Nachricht" "Icon" "Source"
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if "--push" in sys.argv:
        idx  = sys.argv.index("--push")
        args = sys.argv[idx + 1:]
        msg  = args[0] if len(args) > 0 else "Event"
        icon = args[1] if len(args) > 1 else "🔔"
        src  = args[2] if len(args) > 2 else ""
        ts   = datetime.now().strftime("%H:%M:%S")

        data = _read()
        data.append({"msg": msg, "icon": icon, "source": src, "ts": ts, "processed": False})
        _write(data)
        print(f"✅ Hotline-Event gepusht: {icon} {msg}")
    else:
        print("Nutze: python3 hotline.py --push 'Nachricht' '🔔' 'source'")