# gps.py - RICS GPS / Live-Location-Modul
# Befehle: /gps_start, /gps_stop, /gps_status, /gps_merke <name>, /gps_orte
#
# Benoetigt in bot.py (eine Zeile aendern):
#   app.run_polling(allowed_updates=[
#       "message", "callback_query", "message_reaction", "edited_message"
#   ])

import os
import json
import html
import math
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

# Radius in Metern um einen bekannten Ort damit er als "dort" gilt
MATCH_RADIUS_M = 150

# ─────────────────────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────────────────────

def _load() -> dict:
    if MEMORY_FILE.exists():
        try:
            return json.loads(MEMORY_FILE.read_text())
        except Exception:
            pass
    return {
        "aktiv":          False,
        "lat":            None,
        "lng":            None,
        "adresse":        None,
        "letzter_abruf":  None,
        "ort_name":       None,   # z.B. "zuhause", "arbeitsplatz"
        "bekannte_orte":  {},     # {"zuhause": {"adresse":..,"lat":..,"lng":..}, ...}
    }


def _save(data: dict):
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    MEMORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


# ─────────────────────────────────────────────────────────────
# HILFSFUNKTIONEN
# ─────────────────────────────────────────────────────────────

def _distanz_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Haversine-Abstand in Metern."""
    R = 6371000
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2 +
         math.cos(lat1 * p) * math.cos(lat2 * p) *
         math.sin((lng2 - lng1) * p / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _finde_ort(lat: float, lng: float, bekannte_orte: dict) -> str | None:
    """Gibt den Namen des naechstgelegenen bekannten Ortes zurueck (wenn < MATCH_RADIUS_M)."""
    best_name = None
    best_dist = float("inf")
    for name, ort in bekannte_orte.items():
        if ort.get("lat") is None or ort.get("lng") is None:
            continue
        d = _distanz_m(lat, lng, ort["lat"], ort["lng"])
        if d < best_dist:
            best_dist = d
            best_name = name
    if best_dist <= MATCH_RADIUS_M:
        return best_name
    return None


# ─────────────────────────────────────────────────────────────
# OEFFENTLICHE HILFSFUNKTION fuer andere Module
# ─────────────────────────────────────────────────────────────

def get_standort_kontext() -> dict:
    """
    Gibt einen Dict zurueck den proactive_brain und andere Module nutzen koennen:
    {
        "adresse":    "Weichselstrasse, Toeging am Inn, Bayern",
        "ort_name":   "zuhause" | "arbeitsplatz" | None,
        "unterwegs":  True | False,
        "lat":        48.263,
        "lng":        12.598,
        "letzter_abruf": "06.05.2026 18:27:43"
    }
    """
    data = _load()
    if not data.get("aktiv") or not data.get("adresse"):
        return {}
    return {
        "adresse":       data.get("adresse"),
        "ort_name":      data.get("ort_name"),
        "unterwegs":     data.get("ort_name") not in ("zuhause", None) if data.get("ort_name") else True,
        "lat":           data.get("lat"),
        "lng":           data.get("lng"),
        "letzter_abruf": data.get("letzter_abruf"),
    }


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

    adresse  = await _reverse_geocode(lat, lng)
    jetzt    = datetime.now(TIMEZONE).strftime("%d.%m.%Y %H:%M:%S")
    ort_name = _finde_ort(lat, lng, data.get("bekannte_orte", {}))

    data.update({
        "lat":           lat,
        "lng":           lng,
        "adresse":       adresse,
        "letzter_abruf": jetzt,
        "ort_name":      ort_name,
    })
    _save(data)

    ort_info = f" [{ort_name}]" if ort_name else ""
    print(f"[GPS] {adresse}{ort_info} ({lat:.5f}, {lng:.5f})")


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
    data     = _load()
    lat      = data.get("lat")
    lng      = data.get("lng")
    adresse  = data.get("adresse") or "noch kein Standort"
    letzter  = data.get("letzter_abruf") or "noch kein Abruf"
    aktiv    = "Aktiv" if data.get("aktiv") else "Deaktiviert"
    ort_name = data.get("ort_name")
    coords   = f"{lat:.5f}, {lng:.5f}" if (lat is not None and lng is not None) else "unbekannt"
    maps     = (
        f'<a href="https://maps.google.com/?q={lat},{lng}">Google Maps</a>'
        if (lat is not None and lng is not None) else "kein Standort"
    )
    ort_zeile = f"\nErkannter Ort: <b>{html.escape(ort_name)}</b>" if ort_name else ""

    bekannte = data.get("bekannte_orte", {})
    orte_zeile = ""
    if bekannte:
        orte_zeile = "\n\n<b>Bekannte Orte:</b>\n" + "\n".join(
            f"  {html.escape(n)}: {html.escape(o.get('adresse', '?'))}"
            for n, o in bekannte.items()
        )

    text = (
        f"<b>GPS-Status</b>\n\n"
        f"Tracking: {aktiv}\n\n"
        f"<b>Letzter Standort:</b>\n"
        f"{html.escape(adresse)}\n"
        f"<code>{coords}</code>\n"
        f"{maps}"
        f"{ort_zeile}\n\n"
        f"Letzter Abruf: {html.escape(letzter)}"
        f"{orte_zeile}"
    )
    await update.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)

cmd_gps_status.description = "Letzten Standort und bekannte Orte anzeigen"
cmd_gps_status.category    = "GPS"


async def cmd_gps_merke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Speichert den aktuellen Standort unter einem frei waehlbaren Namen."""
    args = context.args
    if not args:
        await update.message.reply_text(
            "Bitte einen Namen angeben.\nBeispiel: /gps_merke arbeitsplatz"
        )
        return

    name = " ".join(args).lower().strip()
    data = _load()

    if not data.get("lat") or not data.get("lng"):
        await update.message.reply_text("Noch kein GPS-Standort vorhanden.")
        return

    bekannte = data.setdefault("bekannte_orte", {})
    bekannte[name] = {
        "adresse": data["adresse"],
        "lat":     data["lat"],
        "lng":     data["lng"],
        "gespeichert": datetime.now(TIMEZONE).strftime("%d.%m.%Y %H:%M"),
    }
    _save(data)

    await update.message.reply_text(
        f"Gespeichert: <b>{html.escape(name)}</b>\n{html.escape(data['adresse'])}",
        parse_mode="HTML"
    )

cmd_gps_merke.description = "Aktuellen Standort merken — /gps_merke arbeitsplatz"
cmd_gps_merke.category    = "GPS"


async def cmd_gps_orte(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Listet alle gespeicherten Orte auf."""
    data     = _load()
    bekannte = data.get("bekannte_orte", {})

    if not bekannte:
        await update.message.reply_text(
            "Noch keine Orte gespeichert.\nMit /gps_merke <name> einen Ort speichern."
        )
        return

    lines = ["<b>Bekannte Orte:</b>\n"]
    for name, ort in bekannte.items():
        lines.append(
            f"<b>{html.escape(name)}</b>\n"
            f"  {html.escape(ort.get('adresse', '?'))}\n"
            f"  gespeichert: {ort.get('gespeichert', '?')}\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

cmd_gps_orte.description = "Alle gespeicherten Orte anzeigen"
cmd_gps_orte.category    = "GPS"


# ─────────────────────────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────────────────────────

def setup(app: Application):
    app.add_handler(CommandHandler("gps_start",  cmd_gps_start))
    app.add_handler(CommandHandler("gps_stop",   cmd_gps_stop))
    app.add_handler(CommandHandler("gps_status", cmd_gps_status))
    app.add_handler(CommandHandler("gps_merke",  cmd_gps_merke))
    app.add_handler(CommandHandler("gps_orte",   cmd_gps_orte))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    # edited_message: LOCATION-Check im Handler selbst, nicht per Filter
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, handle_live_update))
    print("GPS-Modul geladen")