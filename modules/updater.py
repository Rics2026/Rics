#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
RICS Updater — GitHub Version (stable)
======================================
- lädt version.txt von GitHub
- vergleicht Version
- zeigt Update-Info + "Jetzt installieren"-Button
- erst nach Bestätigung wird heruntergeladen & ersetzt
- holt update_notes.txt und zeigt sie im Chat
- Restart-Button erscheint nach erfolgreicher Installation
- DELETE: Präfix in version.txt löscht Dateien lokal
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
    """
    Gibt (version, update_files, delete_files) zurück.
    Zeilen mit 'DELETE:' Präfix → löschen, Rest → herunterladen.
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return "0.0", [], []

    version = lines[0].replace("Version", "").strip()
    update_files = []
    delete_files = []

    for line in lines[1:]:
        if line.upper().startswith("DELETE:"):
            path = line[7:].strip()
            if path:
                delete_files.append(path)
        else:
            update_files.append(line)

    return version, update_files, delete_files


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
# STUFE 1: NUR PRÜFEN (kein Download)
# ─────────────────────────────────────────

def check_for_updates() -> dict:
    """Prüft ob ein Update verfügbar ist — lädt nichts herunter."""
    local_ver = _local_version()

    try:
        r = requests.get(f"{GITHUB_BASE}/version.txt", timeout=10)
        r.raise_for_status()
    except Exception as e:
        return {"status": "error", "msg": f"GitHub Fehler: {e}"}

    remote_ver, file_list, delete_list = _parse_version_txt(r.text)

    if remote_ver == local_ver:
        return {"status": "up_to_date", "version": local_ver}

    # Update Notes vorab holen (nur zur Anzeige)
    notes = _fetch_update_notes()

    return {
        "status": "available",
        "local": local_ver,
        "remote": remote_ver,
        "files": file_list,
        "delete": delete_list,
        "notes": notes,
    }


# ─────────────────────────────────────────
# STUFE 2: TATSÄCHLICH INSTALLIEREN
# ─────────────────────────────────────────

def do_install() -> dict:
    """Lädt die Update-Dateien herunter, ersetzt sie lokal und löscht markierte Dateien."""
    local_ver = _local_version()

    try:
        r = requests.get(f"{GITHUB_BASE}/version.txt", timeout=10)
        r.raise_for_status()
    except Exception as e:
        return {"status": "error", "msg": f"GitHub Fehler: {e}"}

    remote_ver, file_list, delete_list = _parse_version_txt(r.text)

    if remote_ver == local_ver:
        return {"status": "up_to_date", "version": local_ver}

    updated, failed = [], []

    # ── Dateien herunterladen & ersetzen ──
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

    # ── Dateien löschen ──
    deleted, delete_failed = [], []

    for rel_path in delete_list:
        dst = os.path.join(PROJECT_DIR, rel_path)
        try:
            if os.path.exists(dst):
                os.remove(dst)
                deleted.append(rel_path)
                logger.info(f"[Updater] 🗑️ Gelöscht: {rel_path}")
            else:
                logger.info(f"[Updater] ⚠️ Nicht gefunden (skip): {rel_path}")
                deleted.append(f"{rel_path} (nicht vorhanden)")
        except Exception as e:
            logger.error(f"[Updater] ❌ Löschen fehlgeschlagen {rel_path}: {e}")
            delete_failed.append(rel_path)

    if updated or deleted:
        with open(VERSION_FILE, "w") as f:
            f.write(f"Version {remote_ver}")

    notes = _fetch_update_notes()

    return {
        "status": "updated",
        "old": local_ver,
        "new": remote_ver,
        "updated": updated,
        "failed": failed,
        "deleted": deleted,
        "delete_failed": delete_failed,
        "notes": notes,
    }


# ─────────────────────────────────────────
# TELEGRAM COMMAND /update  → nur Check
# ─────────────────────────────────────────

async def update_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔍 Prüfe auf Updates...")

    result = await asyncio.get_event_loop().run_in_executor(
        None, check_for_updates
    )

    status = result.get("status")

    if status == "error":
        await msg.edit_text(f"❌ Fehler:\n{result['msg']}")
        return

    if status == "up_to_date":
        await msg.edit_text(f"✅ Alles aktuell — Version {result['version']}")
        return

    # ── Update verfügbar → Infos anzeigen + Button ──
    file_count   = len(result["files"])
    delete_count = len(result.get("delete", []))
    notes        = result.get("notes", "")

    text = (
        f"🆕 <b>Update verfügbar!</b>\n"
        f"Version {result['local']} → <b>{result['remote']}</b>\n\n"
        f"📦 <b>{file_count} Datei(en) werden aktualisiert</b>"
    )

    if delete_count:
        delete_preview = "\n".join(f"• {f}" for f in result["delete"])
        text += f"\n🗑️ <b>{delete_count} Datei(en) werden gelöscht:</b>\n{delete_preview}"

    if notes:
        text += f"\n\n📋 <b>Was ist neu:</b>\n{notes}"

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("⬇️ Jetzt installieren", callback_data="updater:install"),
        InlineKeyboardButton("❌ Abbrechen",           callback_data="updater:cancel"),
    ]])

    await msg.edit_text(text, parse_mode="HTML", reply_markup=keyboard)


# ─────────────────────────────────────────
# INSTALL CALLBACK → tatsächliche Installation
# ─────────────────────────────────────────

async def install_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.edit_message_text("⬇️ Installation läuft...")

    result = await asyncio.get_event_loop().run_in_executor(
        None, do_install
    )

    status = result.get("status")

    if status == "error":
        await query.edit_message_text(f"❌ Fehler:\n{result['msg']}")
        return

    if status == "up_to_date":
        await query.edit_message_text(f"✅ War bereits aktuell — Version {result['version']}")
        return

    # ── Installation erfolgreich ──
    updated_list      = "\n".join(f"• {f}" for f in result["updated"])
    failed_list       = "\n".join(f"• {f}" for f in result["failed"]) if result["failed"] else ""
    deleted_list      = "\n".join(f"• {f}" for f in result.get("deleted", []))
    delete_failed_list= "\n".join(f"• {f}" for f in result.get("delete_failed", [])) if result.get("delete_failed") else ""
    notes             = result.get("notes", "")

    text = (
        f"🚀 <b>Update installiert!</b>\n"
        f"Version {result['old']} → <b>{result['new']}</b>"
    )

    if updated_list:
        text += f"\n\n<b>Aktualisierte Dateien:</b>\n{updated_list}"

    if deleted_list:
        text += f"\n\n🗑️ <b>Gelöschte Dateien:</b>\n{deleted_list}"

    if failed_list:
        text += f"\n\n⚠️ <b>Fehler (Download):</b>\n{failed_list}"

    if delete_failed_list:
        text += f"\n\n⚠️ <b>Fehler (Löschen):</b>\n{delete_failed_list}"

    if notes:
        text += f"\n\n📋 <b>Was ist neu:</b>\n{notes}"

    # ── In Webchat pushen ──
    try:
        from modules.web_app import web_push
        plain = f"🚀 Update installiert! Version {result['old']} → {result['new']}"
        if updated_list:
            plain += f"\n\nAktualisierte Dateien:\n{updated_list}"
        if deleted_list:
            plain += f"\n\nGelöschte Dateien:\n{deleted_list}"
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

    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)


# ─────────────────────────────────────────
# CANCEL CALLBACK
# ─────────────────────────────────────────

async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("❌ Update abgebrochen.")


# ─────────────────────────────────────────
# RESTART HANDLER
# ─────────────────────────────────────────

async def restart_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.edit_message_text("♻️ Restart läuft...")

    await asyncio.sleep(1)

    # ── Exit-Code 42 → Watchdog in bot.py startet neuen Prozess ──
    os._exit(42)


# ─────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────
update_command.description = "Prüft GitHub auf Updates und fragt vor Installation"
update_command.category = "LLM"

def setup(app):
    app.add_handler(CommandHandler("update", update_command))
    app.add_handler(CallbackQueryHandler(install_callback, pattern="^updater:install$"))
    app.add_handler(CallbackQueryHandler(cancel_callback,  pattern="^updater:cancel$"))
    app.add_handler(CallbackQueryHandler(restart_callback, pattern="^updater:restart$"))

    logger.info("[Updater] ✅ GitHub Updater geladen")