"""
behavior_engine.py — Lernender Verhaltens-Assistent für RICS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Analysiert den Chatverlauf alle 8 Stunden via LLM und extrahiert
Verhaltensregeln automatisch.

Befehle:
  /verhalten          — Profil anzeigen
  /verhalten_analyse  — sofort analysieren
"""

import os
import json
import asyncio
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

load_dotenv()

BOT_NAME   = os.getenv("BOT_NAME",  "RICS")
TIMEZONE   = ZoneInfo(os.getenv("TIMEZONE", "Europe/Berlin"))
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "..", "memory", "behavior_patterns.json")
LOG_DIR    = os.path.join(BASE_DIR, "..", "logs")

ANALYSE_INTERVAL_H = 8
MAX_LOG_LINES      = 300
MAX_RULES          = 20

_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════════
# STATE
# ═══════════════════════════════════════════════════════════════

def _now() -> datetime:
    return datetime.now(TIMEZONE)


def _load() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("learned_rules", [])
            data.setdefault("meta", {})
            return data
        except Exception:
            pass
    return {
        "learned_rules": [],
        "meta": {
            "last_analysis":  None,
            "total_analyses": 0,
            "created":        _now().isoformat(),
        }
    }


def _save(state: dict):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════
# LOG LESEN
# ═══════════════════════════════════════════════════════════════

def _read_recent_log(max_lines: int = MAX_LOG_LINES) -> str:
    today = _now()
    lines = []
    for days_ago in (1, 0):
        date_str = today.strftime("%Y-%m-%d") if days_ago == 0 else \
                   today.replace(day=today.day - 1).strftime("%Y-%m-%d")
        path = os.path.join(LOG_DIR, f"{date_str}.log")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    lines = f.read().splitlines() + lines
            except Exception:
                pass
    chat_lines = [l for l in lines if l.startswith("USER:") or l.startswith("BOT:")]
    return "\n".join(chat_lines[-max_lines:])


# ═══════════════════════════════════════════════════════════════
# LLM ANALYSE
# ═══════════════════════════════════════════════════════════════

ANALYSE_PROMPT = """Du analysierst einen Chatverlauf zwischen einem Nutzer und seinem KI-Assistenten namens {BOT_NAME}.

BESTEHENDE REGELN (die du kennen musst):
{existing}

WICHTIG — Widersprüche erkennen und korrigieren:
Wenn der Nutzer im Chatverlauf einer bestehenden Regel widerspricht, MUSS die alte Regel
ersetzt oder gelöscht werden. Beispiele:
- Alte Regel: "Nicht mit Sir anreden" + Nutzer sagt "Sir ist auch okay" → Regel löschen oder anpassen
- Alte Regel: "Kurze Antworten" + Nutzer sagt "lieber ausführlicher" → Regel ersetzen
Widersprechende alte Regeln NIEMALS einfach übernehmen.

Deine Aufgabe: Erstelle die vollständige, aktuelle Regelliste.
- Bestehende Regeln die noch stimmen → übernehmen
- Bestehende Regeln die widerlegt wurden → löschen oder korrigieren
- Neue Muster aus dem Chatverlauf → ergänzen

Antworte NUR mit einem JSON-Array, maximal 15 klare, umsetzbare Regeln:
["Regel 1", "Regel 2", ...]

Keine leeren Einträge, keine Duplikate, keine Meta-Kommentare.
Wenn gar nichts erkennbar und keine bestehenden Regeln: []

CHATVERLAUF:
{log}"""


async def _run_analysis(existing_rules: list) -> list:
    log = _read_recent_log()
    if len(log.strip()) < 80 and not existing_rules:
        print("[behavior_engine] Zu wenig Log-Inhalt.")
        return []
    try:
        from core.llm_client import get_client
        client = get_client()

        existing_str = "\n".join(f"• {r['text']}" for r in existing_rules) if existing_rules else "Keine"
        prompt = (ANALYSE_PROMPT
                  .replace("{BOT_NAME}", BOT_NAME)
                  .replace("{existing}", existing_str)
                  .replace("{log}", log))

        result = await client.chat_json(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=800,
        )
        if isinstance(result, list):
            return [str(r).strip() for r in result if isinstance(r, str) and len(r.strip()) > 8][:15]
        if isinstance(result, dict):
            for key in ("rules", "regeln", "items", "data"):
                if key in result and isinstance(result[key], list):
                    return [str(r).strip() for r in result[key] if len(str(r).strip()) > 8][:15]
    except Exception as e:
        print(f"[behavior_engine] Analyse-Fehler: {e}")
    return existing_rules and [r["text"] for r in existing_rules] or []


async def run_analysis_cycle() -> tuple:
    print("[behavior_engine] Starte Log-Analyse...")
    with _lock:
        state = _load()
        existing = state.get("learned_rules", [])

    new_rules = await _run_analysis(existing)
    now = _now().isoformat()

    with _lock:
        state = _load()
        old_count = len(state["learned_rules"])
        # LLM liefert die komplette neue Liste — direkt übernehmen
        state["learned_rules"] = [{"text": r, "created": now} for r in new_rules]
        new_count = len(state["learned_rules"])
        state["meta"]["last_analysis"]  = now
        state["meta"]["total_analyses"] = state["meta"].get("total_analyses", 0) + 1
        _save(state)

    diff = new_count - old_count
    print(f"[behavior_engine] Neu: {new_count} Regeln (vorher {old_count}).")
    return new_count, diff


# ═══════════════════════════════════════════════════════════════
# SCHEDULER
# ═══════════════════════════════════════════════════════════════

async def _scheduler_loop():
    await asyncio.sleep(120)
    while True:
        try:
            with _lock:
                state = _load()
            last = state["meta"].get("last_analysis")
            run_now = True
            if last:
                try:
                    last_dt = datetime.fromisoformat(last).astimezone(TIMEZONE)
                    run_now = (_now() - last_dt).total_seconds() / 3600 >= ANALYSE_INTERVAL_H
                except Exception:
                    pass
            if run_now:
                await run_analysis_cycle()
        except Exception as e:
            print(f"[behavior_engine] Scheduler-Fehler: {e}")
        await asyncio.sleep(3600)


# ═══════════════════════════════════════════════════════════════
# SYSTEM-PROMPT INJECTION
# ═══════════════════════════════════════════════════════════════

def get_behavior_section() -> str:
    with _lock:
        state = _load()
    learned = state.get("learned_rules", [])
    if not learned:
        return ""
    lines = ["\n### 🧠 VERHALTENS-PROFIL (gelernt aus Gesprächen):"]
    for r in learned:
        lines.append(f"• {r['text']}")
    return "\n".join(lines) + "\n"


# ═══════════════════════════════════════════════════════════════
# TELEGRAM COMMANDS
# ═══════════════════════════════════════════════════════════════

async def cmd_verhalten(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with _lock:
        state = _load()
    learned = state.get("learned_rules", [])
    meta    = state.get("meta", {})
    lines   = ["🧠 *Verhaltens-Profil*\n"]
    if learned:
        lines.append("*💡 Gelernte Muster:*")
        for i, r in enumerate(learned, 1):
            lines.append(f"  {i}. {r['text']}")
        lines.append("")
    else:
        lines.append("📭 Noch keine Muster gelernt.")
    last  = meta.get("last_analysis", "—")
    total = meta.get("total_analyses", 0)
    lines.append(f"_Analysen: {total} | Letzte: {str(last)[:16]}_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

cmd_verhalten.description = "Zeigt das gelernte Verhaltens-Profil"
cmd_verhalten.category    = "Gedächtnis"


async def cmd_verhalten_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/verhalten_del <nr> — löscht eine gelernte Regel per Nummer."""
    if not context.args:
        await update.message.reply_text("Verwendung: /verhalten_del <nr>")
        return
    try:
        idx = int(context.args[0]) - 1
    except ValueError:
        await update.message.reply_text("Bitte eine Zahl angeben.")
        return
    with _lock:
        state = _load()
        rules = state.get("learned_rules", [])
        if idx < 0 or idx >= len(rules):
            await update.message.reply_text(f"Regel #{idx+1} nicht gefunden. Aktuell {len(rules)} Regeln.")
            return
        removed = rules.pop(idx)
        _save(state)
    await update.message.reply_text(f"🗑️ Regel #{idx+1} gelöscht:\n{removed['text']}")

cmd_verhalten_del.description = "Löscht eine gelernte Regel per Nummer"
cmd_verhalten_del.category    = "Gedächtnis"


async def cmd_verhalten_analyse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Analysiere Chatverlauf...")
    try:
        found, added = await run_analysis_cycle()
        await update.message.reply_text(
            f"Analyse fertig — {found} Muster erkannt, {added} neu gespeichert."
        )
    except Exception as e:
        await update.message.reply_text(f"Fehler: {e}")

cmd_verhalten_analyse.description = "Startet sofort eine Verhaltens-Analyse"
cmd_verhalten_analyse.category    = "Gedächtnis"


# ═══════════════════════════════════════════════════════════════
# SETUP
# ═══════════════════════════════════════════════════════════════

def setup(app):
    app.add_handler(CommandHandler("verhalten",         cmd_verhalten))
    app.add_handler(CommandHandler("verhalten_analyse", cmd_verhalten_analyse))
    app.add_handler(CommandHandler("verhalten_del",     cmd_verhalten_del))
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(_scheduler_loop())
        print(f"✅ behavior_engine geladen — Analyse alle {ANALYSE_INTERVAL_H}h automatisch")
    except RuntimeError:
        threading.Thread(target=lambda: asyncio.run(_scheduler_loop()), daemon=True).start()
        print(f"✅ behavior_engine geladen (Thread-Modus)")