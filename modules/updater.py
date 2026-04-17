#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
RICS Updater — GitHub Version (stable)
======================================
- lädt version.txt von GitHub
- vergleicht Version
- lädt gelistete Dateien direkt
- ersetzt lokale Dateien
- holt update_notes.txt und zeigt sie im Chat
- Telegram /update + Restart Button (erst NACH den Notes)
"""

import os
import sys
import logging
import asyncio
import requests

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, CallbackQueryHandler, ContextTypes

logger = logging.getLogger(__name__)

# ════════════════════════════════════════
# ⚙️ GITHUB CONFIG
# ════════════════════════════════════════

GITHUB_BASE = "https://raw.githubusercontent.com/Rics2026/Rics/main"

# ════════════════════════════════════════

PROJECT_DIR  = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
VERSION_FILE = os.path.join(PROJECT_DIR, "version.txt")


# ─────────────────────────────────────────
# VERSION LOCAL
# ─────────────────────────────────────────

def _local_version() -> str:
    try:
        return open(VERSION_FILE).read().strip().replace("Version ", "")
    except Exception:
        return "0.0"


# ─────────────────────────────────────────
# PARSER VERSION.TXT
# ─────────────────────────────────────────

def _parse_version_txt(text: str):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return "0.0", []

    version = lines[0].replace("Version", "").strip()
    files = lines[1:]
    return version, files


# ─────────────────────────────────────────
# UPDATE NOTES FETCHER
# ─────────────────────────────────────────

def _fetch_update_notes() -> str:
    """Holt update_notes.txt von GitHub. Gibt '' zurück bei Fehler."""
    try:
        r = requests.get(f"{GITHUB_BASE}/update_notes.txt", timeout=10)
        r.raise_for_status()
        return r.text.strip()
    except Exception as e:
        logger.warning(f"[Updater] update_notes.txt nicht abrufbar: {e}")
        return ""


# ─────────────────────────────────────────
# CORE UPDATER
# ─────────────────────────────────────────

def check_and_update() -> dict:
    local_ver = _local_version()

    try:
        r = requests.get(f"{GITHUB_BASE}/version.txt", timeout=10)
        r.raise_for_status()
    except Exception as e:
        return {"status": "error", "msg": f"GitHub Fehler: {e}"}

    remote_ver, file_list = _parse_version_txt(r.text)

    if remote_ver == local_ver:
        return {"status": "up_to_date", "version": local_ver}

    updated, failed = [], []

    for rel_path in file_list:
        try:
            file_url = f"{GITHUB_BASE}/{rel_path}"

            fr = requests.get(file_url, timeout=10)
            fr.raise_for_status()

            dst = os.path.join(PROJECT_DIR, rel_path)
            os.makedirs(os.path.dirname(dst), exist_ok=True)

            with open(dst, "wb") as f:
                f.write(fr.content)

            updated.append(rel_path)
            logger.info(f"[Updater] ✅ {rel_path}")

        except Exception as e:
            logger.error(f"[Updater] ❌ {rel_path}: {e}")
            failed.append(rel_path)

    if updated:
        with open(VERSION_FILE, "w") as f:
            f.write(f"Version {remote_ver}")

    # Update Notes holen (nach erfolgreichem Update)
    notes = _fetch_update_notes()

    return {
        "status": "updated",
        "old": local_ver,
        "new": remote_ver,
        "updated": updated,
        "failed": failed,
        "notes": notes,
    }


# ─────────────────────────────────────────
# TELEGRAM COMMAND /update
# ─────────────────────────────────────────

async def update_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔍 Prüfe auf Updates...")

    result = await asyncio.get_event_loop().run_in_executor(
        None, check_and_update
    )

    status = result.get("status")

    if status == "error":
        await msg.edit_text(f"❌ Fehler:\n{result['msg']}")
        return

    if status == "up_to_date":
        await msg.edit_text(f"✅ Alles aktuell — Version {result['version']}")
        return

    # ── Update erfolgreich ──
    updated_list = "\n".join(f"• {f}" for f in result["updated"])
    failed_list  = "\n".join(f"• {f}" for f in result["failed"]) if result["failed"] else ""
    notes        = result.get("notes", "")

    text = (
        f"🚀 <b>Update installiert!</b>\n"
        f"Version {result['old']} → <b>{result['new']}</b>\n\n"
        f"<b>Aktualisierte Dateien:</b>\n{updated_list}"
    )

    if failed_list:
        text += f"\n\n⚠️ <b>Fehler:</b>\n{failed_list}"

    if notes:
        text += f"\n\n📋 <b>Was ist neu:</b>\n{notes}"

    # ── In Webchat pushen ──
    try:
        from modules.web_app import web_push
        # Plaintext-Version für den Webchat (kein HTML)
        plain = (
            f"🚀 Update installiert! Version {result['old']} → {result['new']}\n\n"
            f"Aktualisierte Dateien:\n{updated_list}"
        )
        if failed_list:
            plain += f"\n\n⚠️ Fehler:\n{failed_list}"
        if notes:
            plain += f"\n\n📋 Was ist neu:\n{notes}"
        web_push(plain)
    except Exception as e:
        logger.warning(f"[Updater] web_push fehlgeschlagen: {e}")

    # ── Restart-Button erst jetzt ──
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("♻️ Kernel neu starten", callback_data="updater:restart")
    ]])

    await msg.edit_text(text, parse_mode="HTML", reply_markup=keyboard)


# ─────────────────────────────────────────
# RESTART HANDLER
# ─────────────────────────────────────────

async def restart_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.edit_message_text("♻️ Restart läuft...")

    await asyncio.sleep(1)

    # ── Neuen Bot-Prozess starten (komplett frisch, neue PID) ──
    # subprocess.Popen mit close_fds=True vererbt keine File-Descriptors.
    # start_new_session=True macht den neuen Prozess unabhängig vom alten.
    # Das ist sauberer als os.execv() weil:
    #  - Neue PID
    #  - Keine inherited Sockets (insbesondere Flask-Port 5001)
    #  - Kein FD-Vererbungs-Problem
    import subprocess
    bot_path = os.path.join(PROJECT_DIR, "bot.py")

    subprocess.Popen(
        [sys.executable, bot_path],
        cwd=PROJECT_DIR,
        close_fds=True,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
    )

    # ── Alten Prozess hart beenden ─────────────────────────────
    # os._exit() statt sys.exit() — überspringt Python-Cleanup,
    # schließt aber alle Sockets via Kernel-Exit.
    # Flask-Listening-Socket wird sofort frei (kein TIME_WAIT weil
    # keine aktiven Verbindungen auf dem Listen-Socket).
    os._exit(0)


# ─────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────
update_command.description = "Prüft GitHub auf Updates und installiert sie"
update_command.category = "LLM"

def setup(app):
    app.add_handler(CommandHandler("update", update_command))
    app.add_handler(CallbackQueryHandler(restart_callback, pattern="^updater:restart$"))

    logger.info("[Updater] ✅ GitHub Updater geladen")