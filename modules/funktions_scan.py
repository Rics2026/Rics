import os
import re
from collections import defaultdict
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

# ────────────────────────────────────────────────────────────────
# CAPABILITIES CACHE — wird beim ersten Chat-Aufruf befüllt
# und bei Änderung der Handler-Anzahl automatisch aktualisiert
# ────────────────────────────────────────────────────────────────
_cache = {"text": "", "handler_count": -1}


def _get_jarvis(context):
    return context.application.bot_data.get("jarvis")


def _parse_handlers(app) -> list[tuple[str, str, str]]:
    """Gibt sortierte Liste von (kategorie, befehl, beschreibung) zurück."""
    entries = []
    for group in app.handlers.values():
        for handler in group:
            if isinstance(handler, CommandHandler):
                cmd  = sorted(handler.commands)[0]          # primärer Befehl
                desc = getattr(handler.callback, "description", "")
                cat  = getattr(handler.callback, "category",    "Allgemein")
                if desc:
                    entries.append((cat, cmd, desc))
    return sorted(entries, key=lambda x: (x[0], x[1]))


# ────────────────────────────────────────────────────────────────
# PUBLIC: Für den System-Prompt in bot.py
# ────────────────────────────────────────────────────────────────
def get_capabilities_context(app) -> str:
    """
    Gibt eine kompakte, LLM-freundliche Übersicht aller RICS-Fähigkeiten zurück.
    Wird in den System-Prompt injiziert damit RICS seine eigenen Funktionen kennt.
    Gecacht — wird nur neu gebaut wenn sich die Handler-Anzahl ändert.
    """
    # Alle Handler zählen (als Cache-Key)
    current_count = sum(len(g) for g in app.handlers.values())
    if _cache["handler_count"] == current_count and _cache["text"]:
        return _cache["text"]

    entries = _parse_handlers(app)
    if not entries:
        return ""

    grouped = defaultdict(list)
    for cat, cmd, desc in entries:
        grouped[cat].append(f"/{cmd}: {desc}")

    lines = ["### MEINE FÄHIGKEITEN & BEFEHLE:"]
    for cat in sorted(grouped):
        lines.append(f"[{cat}]")
        for item in grouped[cat]:
            lines.append(f"  {item}")

    # ── Hintergrunddienste (keine Commands, aber echte Fähigkeiten) ───────────
    lines.append("\n[Autonome Hintergrunddienste — laufen automatisch]")
    background = [
        ("Sprachnachrichten", "Ich verstehe Sprachnachrichten — René kann mir Audios schicken, ich transkribiere und antworte"),
        ("Proaktives Denken", "Ich überwache selbstständig: Solar/Energie-Schwellen, Wetter, Agenda, Ressourcen — und melde mich wenn etwas wichtig ist"),
        ("Moltbook", "Alle 30 Minuten poste und kommentiere ich automatisch auf Moltbook (KI-Social-Network) unter u/rics"),
        ("PayPal-Monitor", "Ich überwache Renés PayPal-Konto via E-Mail und melde eingehende Zahlungen automatisch"),
        ("Selbstreflexion", "Täglich reflektiere ich meine Gespräche und speichere Erkenntnisse ins Langzeitgedächtnis"),
        ("Stimmungserkennung", "Ich erkenne Renés Stimmung aus dem Gesprächsverlauf und passe meinen Ton automatisch an"),
        ("Langzeitgedächtnis", "Ich lerne aus jeder Unterhaltung — Fakten über René werden automatisch gespeichert"),
    ]
    for name, desc in background:
        lines.append(f"  {name}: {desc}")

    text = "\n".join(lines)
    _cache["text"] = text
    _cache["handler_count"] = current_count
    return text


# ────────────────────────────────────────────────────────────────
# /funktionen — Telegram-Kommando für den Nutzer
# ────────────────────────────────────────────────────────────────
async def funktionen_liste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    app     = context.application
    entries = _parse_handlers(app)

    if not entries:
        return await update.message.reply_text("❌ Keine Befehle gefunden.")

    grouped = defaultdict(list)
    for cat, cmd, desc in entries:
        grouped[cat].append((cmd, desc))

    text = "🛠️ <b>RICS FUNKTIONSREGISTER</b>\n──────────────────────────"
    for cat in sorted(grouped):
        text += f"\n\n<b>{cat}</b>"
        for cmd, desc in sorted(grouped[cat]):
            text += f"\n/{cmd} — {desc}"

    MAX    = 3800
    chunks = [text[i:i + MAX] for i in range(0, len(text), MAX)]
    for i, chunk in enumerate(chunks):
        prefix = f"<b>({i+1}/{len(chunks)})</b>\n" if len(chunks) > 1 else ""
        await update.message.reply_text(f"{prefix}{chunk}", parse_mode="HTML")


funktionen_liste.description = "Zeigt alle verfügbaren Befehle und Module"
funktionen_liste.category    = "System"


def setup(app):
    app.add_handler(CommandHandler("funktionen", funktionen_liste))