import os
import re
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

def _get_jarvis(context):
    return context.application.bot_data.get("jarvis")

async def funktionen_liste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jarvis = _get_jarvis(context)

    if not jarvis:
        return await update.message.reply_text("⚠️ Fehler: Jarvis-Kern konnte nicht geladen werden.")

    raw = jarvis.get_capabilities(context)

    # /['befehl'] [Kategorie]: Beschreibung → sauber parsen
    lines = []
    for match in re.finditer(r"-\s*/\['(\w+)'\]\s*\[([^\]]+)\]:\s*(.+)", raw):
        cmd, cat, desc = match.group(1), match.group(2), match.group(3).strip()
        lines.append((cat, cmd, desc))

    if not lines:
        return await update.message.reply_text("❌ Keine Befehle gefunden.")

    # Nach Kategorie gruppieren
    from collections import defaultdict
    grouped = defaultdict(list)
    for cat, cmd, desc in lines:
        grouped[cat].append((cmd, desc))

    text = "🛠️ <b>SYSTEM-FUNKTIONSREGISTER</b>\n──────────────────────────"
    for cat in sorted(grouped):
        text += f"\n\n<b>{cat}</b>"
        for cmd, desc in sorted(grouped[cat]):
            text += f"\n/{cmd} — {desc}"

    MAX = 3800
    chunks = [text[i:i+MAX] for i in range(0, len(text), MAX)]
    for i, chunk in enumerate(chunks):
        prefix = f"<b>({i+1}/{len(chunks)})</b>\n" if len(chunks) > 1 else ""
        await update.message.reply_text(f"{prefix}{chunk}", parse_mode='HTML')

funktionen_liste.description = "Zeigt eine detaillierte Liste aller verfügbaren Befehle und Tools"
funktionen_liste.category = "System"

def setup(app):
    app.add_handler(CommandHandler("funktionen", funktionen_liste))