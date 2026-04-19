import os
import re
import html as _html
from dotenv import load_dotenv
load_dotenv()

BOT_NAME = os.getenv("BOT_NAME", "RICS")

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler

# ── Kategorie-Gruppierung ──────────────────────────────────────
CATEGORY_GROUPS = {
    "🧠 KI & Agenten":     ["KI", "Persönlichkeit", "Kategorie"],
    "🧩 Gedächtnis":       ["Gedächtnis"],
    "📅 Planung & Jobs":   ["Agenda", "Briefing", "Jobs"],
    "👁 Vision & Medien":  ["Vision", "Content"],
    "🌐 Info & Recherche": ["Monitor", "Wetter", "Energie", "Autonom", "Recherche"],
    "📱 Social & Discord": ["Social", "Discord"],
    "💰 Finance":          ["Finance"],
    "💻 System & LLM":     ["System", "LLM", f"Dateisystem {BOT_NAME}"],
}

_RAW_TO_GROUP: dict = {}
for _group, _raws in CATEGORY_GROUPS.items():
    for _r in _raws:
        _RAW_TO_GROUP[_r] = _group

def _collect_commands(context):
    groups = {g: [] for g in CATEGORY_GROUPS}
    groups["❓ Sonstiges"] = []
    for handler_group in context.application.handlers.values():
        for handler in handler_group:
            if not isinstance(handler, CommandHandler):
                continue
            cmd  = sorted(handler.commands)[0]
            desc = getattr(handler.callback, "description", "Keine Beschreibung")
            raw  = getattr(handler.callback, "category", "Sonstiges")
            group = _RAW_TO_GROUP.get(raw, "❓ Sonstiges")
            groups[group].append((cmd, desc))
    return {g: sorted(cmds) for g, cmds in groups.items() if cmds}

def _main_keyboard(groups):
    buttons = []
    for group in groups:
        label = f"{group}  ({len(groups[group])})"
        buttons.append([InlineKeyboardButton(label, callback_data=f"help_cat:{group}")])
    buttons.append([InlineKeyboardButton("📋 Alle auf einmal", callback_data="help_cat:__ALL__")])
    return InlineKeyboardMarkup(buttons)

def _back_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("◀️ Zurück zur Übersicht", callback_data="help_cat:__BACK__")
    ]])

def _category_text(group, cmds):
    lines = [f"<b>{_html.escape(group)}</b>\n──────────────────"]
    for cmd, desc in cmds:
        lines.append(f"/{_html.escape(cmd)} — <i>{_html.escape(str(desc))}</i>")
    return "\n".join(lines)

def _all_text(groups, limit: int = 3800) -> str:
    """Baut den Gesamt-Text, bricht sauber an Blockgrenzen ab."""
    sections = []
    total = 0
    for g, c in groups.items():
        block = _category_text(g, c)
        if total + len(block) + 2 > limit:
            sections.append("…(weitere Kategorien gekürzt)")
            break
        sections.append(block)
        total += len(block) + 2
    return "\n\n".join(sections)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    groups = _collect_commands(context)
    intro = (
        f"🤖 <b>{BOT_NAME} — BEFEHLSZENTRALE</b>\n"
        "──────────────────────────────\n"
        "Wähle eine Kategorie, Sir:\n"
    )
    await update.message.reply_text(intro, parse_mode="HTML", reply_markup=_main_keyboard(groups))

async def help_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chosen = query.data.replace("help_cat:", "")
    groups = _collect_commands(context)
    if chosen == "__BACK__":
        await query.edit_message_text(
            f"🤖 <b>{BOT_NAME} — BEFEHLSZENTRALE</b>\n──────────────────────────────\nWähle eine Kategorie, Sir:\n",
            parse_mode="HTML",
            reply_markup=_main_keyboard(groups),
        )
        return
    if chosen == "__ALL__":
        text = "📋 <b>ALLE BEFEHLE</b>\n\n" + _all_text(groups)
    elif chosen in groups:
        text = _category_text(chosen, groups[chosen])
    else:
        text = "❌ Kategorie nicht gefunden."
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=_back_keyboard())

help_command.description = "Interaktives Hilfe-Menü mit Kategorien"
help_command.category    = "System"

def setup(app):
    app.add_handler(CommandHandler("hilfe", help_command))
    app.add_handler(CallbackQueryHandler(help_category_callback, pattern=r"^help_cat:"))