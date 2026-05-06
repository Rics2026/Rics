# gps.py - RICS GPS / Live-Location-Modul
# Befehle: /gps_start, /gps_stop, /gps_status
#
# Benoetigt in bot.py (eine Zeile aendern):
#   app.run_polling(allowed_updates=[
#       "message", "callback_query", "message_reaction", "edited_message"
#   ])

import os
import json
import html
import httpx
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.error import BadRequest

TIMEZONE    = ZoneInfo(os.getenv("TIMEZONE", "Europe/Berlin"))
MEMORY_FILE = Path(__file__).parent.parent / "memory" / "gps.json"
CHAT_ID     = int(os.getenv("CHAT_ID", "0"))

# ─────────────────────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────────────────────

def _load() -> dict:
    if MEMORY_FILE.exists():
        try:
            return json.loads(MEMORY_FILE.read_text())
        except Exception:
            pass
    return {"aktiv": False, "lat": None, "lng": None, "adresse": None, "letzter_abruf": None}


def _save(data: dict):
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    MEMORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


# ─────────────────────────────────────────────────────────────
# REVERSE GEOCODING
# ─────────────────────────────────────────────────────────────

async def _reverse_geocode(lat: float, lng: float) -> str:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"lat": lat, "lon": lng, "format": "json", "zoom": 16},
                headers={"User-Agent": "RICS-GPS-Module/1.0"},
            )
            d    = r.json()
            addr = d.get("address", {})
            parts = []
            for key in ("road", "house_number", "suburb", "city", "town", "village", "state"):
                v = addr.get(key)
                if v and v not in parts:
                    parts.append(v)
            return ", ".join(parts[:4]) if parts else d.get("display_name", f"{lat:.5f},{lng:.5f}")
    except Exception:
        return f"{lat:.5f}, {lng:.5f}"


# ─────────────────────────────────────────────────────────────
# KERN: Standort still verarbeiten + initiale Nachricht loeschen
# ─────────────────────────────────────────────────────────────

async def _verarbeite(lat: float, lng: float, bot, msg_id: int | None = None):
    data = _load()
    if not data.get("aktiv"):
        return

    adresse = await _reverse_geocode(lat, lng)
    jetzt   = datetime.now(TIMEZONE).strftime("%d.%m.%Y %H:%M:%S")

    data.update({"lat": lat, "lng": lng, "adresse": adresse, "letzter_abruf": jetzt})
    _save(data)
    print(f"[GPS] {adresse} ({lat:.5f}, {lng:.5f})")

    # Initiale Location-Bubble loeschen
    if msg_id:
        try:
            await bot.delete_message(chat_id=CHAT_ID, message_id=msg_id)
        except (BadRequest, Exception):
            pass


# ─────────────────────────────────────────────────────────────
# HANDLER
# ─────────────────────────────────────────────────────────────

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.location:
        return
    await _verarbeite(msg.location.latitude, msg.location.longitude, context.bot, msg.message_id)


async def handle_live_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.edited_message
    if not msg or not msg.location:
        return
    await _verarbeite(msg.location.latitude, msg.location.longitude, context.bot, msg_id=None)


# ─────────────────────────────────────────────────────────────
# COMMANDS
# ─────────────────────────────────────────────────────────────

async def cmd_gps_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = _load()
    data["aktiv"] = True
    _save(data)
    bot_name = os.getenv("BOT_NAME", "Ich")
    text = (
        f"<b>GPS-Tracking aktiviert</b>\n\n"
        f"Einmalig einrichten:\n\n"
        f"1. Bueroklammer-Symbol antippen\n"
        f"2. Standort waehlen\n"
        f"3. <b>Live-Standort teilen</b>\n"
        f"4. Dauer auf <b>Unbegrenzt</b> stellen\n\n"
        f"Ab dann empfaengt {bot_name} automatisch deinen Standort.\n"
        f"Kein Tippen mehr, nichts im Chat."
    )
    await update.message.reply_text(text, parse_mode="HTML")

cmd_gps_start.description = "GPS-Tracking aktivieren"
cmd_gps_start.category    = "GPS"


async def cmd_gps_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = _load()
    data["aktiv"] = False
    _save(data)
    await update.message.reply_text(
        "GPS-Tracking deaktiviert.\n"
        "Die Live-Location in Telegram kannst du manuell stoppen."
    )

cmd_gps_stop.description = "GPS-Tracking deaktivieren"
cmd_gps_stop.category    = "GPS"


async def cmd_gps_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data    = _load()
    lat     = data.get("lat")
    lng     = data.get("lng")
    adresse = data.get("adresse") or "noch kein Standort"
    letzter = data.get("letzter_abruf") or "noch kein Abruf"
    aktiv   = "Aktiv" if data.get("aktiv") else "Deaktiviert"
    coords  = f"{lat:.5f}, {lng:.5f}" if (lat is not None and lng is not None) else "unbekannt"
    maps    = (
        f'<a href="https://maps.google.com/?q={lat},{lng}">Google Maps</a>'
        if (lat is not None and lng is not None) else "kein Standort"
    )
    text = (
        f"<b>GPS-Status</b>\n\n"
        f"Tracking: {aktiv}\n\n"
        f"<b>Letzter Standort:</b>\n"
        f"{html.escape(adresse)}\n"
        f"<code>{coords}</code>\n"
        f"{maps}\n\n"
        f"Letzter Abruf: {html.escape(letzter)}"
    )
    await update.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)

cmd_gps_status.description = "Letzten Standort anzeigen"
cmd_gps_status.category    = "GPS"


# ─────────────────────────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────────────────────────

def setup(app: Application):
    app.add_handler(CommandHandler("gps_start",  cmd_gps_start))
    app.add_handler(CommandHandler("gps_stop",   cmd_gps_stop))
    app.add_handler(CommandHandler("gps_status", cmd_gps_status))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(
        filters.UpdateType.EDITED_MESSAGE & filters.LOCATION,
        handle_live_update
    ))
    print("GPS-Modul geladen")