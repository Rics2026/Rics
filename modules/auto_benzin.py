#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv
from duckduckgo_search import DDGS

load_dotenv()

# -----------------------------
# 🔧 CONFIG
# -----------------------------
# Key aus .env holen & sauber bereinigen
def clean_key(key: str) -> str:
    return key.strip().strip("'").strip('"')

API_KEY = clean_key(os.getenv("TANKERKOENIG_API_KEY", ""))

# Wohnort dynamisch aus .env
WOHNORT = os.getenv("WOHNORT", "")

# Radius für Tankstellen (km)
RADIUS = 5

# Kraftstofftypen — alles andere in Args gilt als Ortsname
FUEL_TYPES = {"e5", "e10", "diesel"}

# -----------------------------
# 🔧 Hilfsfunktionen
# -----------------------------
def safe_price(val):
    try:
        if val is None:
            return "-"
        return f"{float(val):.2f} €"
    except:
        return "-"

def extract_all_prices(text):
    matches = re.findall(r"\d{1,2}[.,]\d{2}", text)
    prices = []
    for m in matches:
        value = float(m.replace(",", "."))
        if 1.3 <= value <= 2.5:
            prices.append(value)
    return prices

def avg(lst):
    return round(sum(lst) / len(lst), 2) if lst else None

def parse_args(args: list) -> tuple:
    """
    Trennt Args in Ortsname und Kraftstofftyp.
    Rückgabe: (city_or_None, fuel_type)
    Beispiele:
      []                    -> (None, "all")
      ["München"]           -> ("München", "all")
      ["e5"]                -> (None, "e5")
      ["München", "e5"]     -> ("München", "e5")
      ["e5", "München"]     -> ("München", "e5")
      ["Töging", "am Inn"]  -> ("Töging am Inn", "all")
    """
    fuel = "all"
    ort_parts = []
    for a in args:
        if a.lower() in FUEL_TYPES:
            fuel = a.lower()
        else:
            ort_parts.append(a)
    city = " ".join(ort_parts).strip() or None
    return city, fuel

# -----------------------------
# 🔧 Funktion zur Ortskoordinate (Nominatim, kein API-Key nötig)
# -----------------------------
_coord_cache: dict = {}

def get_coordinates(ort: str) -> tuple[float, float] | None:
    """Koordinaten per Nominatim-Geocoding ermitteln. Ergebnis wird gecacht."""
    if ort in _coord_cache:
        return _coord_cache[ort]
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": ort, "format": "json", "limit": 1},
            headers={"User-Agent": "RICS-Bot/1.0"},
            timeout=5,
        )
        data = resp.json()
        if data:
            coords = (float(data[0]["lat"]), float(data[0]["lon"]))
            _coord_cache[ort] = coords
            return coords
    except Exception:
        pass
    return None

# -----------------------------
# 🤖 Haupt-Handler
# -----------------------------
async def handle_benzin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    city_arg, fuel_type = parse_args(context.args or [])

    # Ort: aus Args oder Fallback auf .env
    wohnort = city_arg or os.getenv("WOHNORT", "")

    # -----------------------------
    # 🔥 Mit API
    # -----------------------------
    if API_KEY:
        coords = get_coordinates(wohnort) if wohnort else None
        if not coords:
            await update.message.reply_text(
                f"❌ Koordinaten für '{wohnort}' nicht gefunden.\n"
                "Bitte WOHNORT in der .env prüfen oder Ort als Argument angeben."
            )
            return
        lat, lng = coords

        await update.message.reply_text(f"⛽ Lade Live-Daten für {wohnort}...")

        # API sort-Regeln
        sort = "&sort=dist" if fuel_type == "all" else "&sort=price"

        url = (
            "https://creativecommons.tankerkoenig.de/json/list.php"
            f"?lat={lat}&lng={lng}&rad={RADIUS}&type={fuel_type}{sort}&apikey={API_KEY}"
        )

        try:
            res = requests.get(url, timeout=10)
            if res.status_code != 200:
                await update.message.reply_text(f"❌ HTTP Fehler: {res.status_code}")
                return

            data = res.json()

            if not data.get("ok"):
                await update.message.reply_text("⚠️ API Problem → nutze Fallback")
                api_key = None
            else:
                stations = data.get("stations", [])
                if not stations:
                    await update.message.reply_text(f"❌ Keine Tankstellen in {wohnort} gefunden.")
                    return

                msg = f"⛽ *Top Tankstellen ({fuel_type.upper()}) in {wohnort}*\n"
                msg += "-----------------\n"

                for s in stations[:3]:
                    msg += f"📍 *{s['name']}*\n"
                    msg += f"• E5: {safe_price(s.get('e5'))}\n"
                    msg += f"• E10: {safe_price(s.get('e10'))}\n"
                    msg += f"• Diesel: {safe_price(s.get('diesel'))}\n\n"

                msg += "-----------------\n_Quelle: Tankerkönig_"
                await update.message.reply_text(msg, parse_mode="Markdown")
                return

        except Exception as e:
            await update.message.reply_text(f"❌ Fehler: {e}")
            return

    # -----------------------------
    # ⚠️ Fallback: Web-Schätzung
    # -----------------------------
    await update.message.reply_text(f"⚠️ Nutze Web-Schätzung für {wohnort}...")

    search_queries = [
        f"Spritpreise {wohnort} aktuell",
        f"Benzinpreis {wohnort} heute",
        f"Dieselpreis {wohnort} aktuell"
    ]

    all_prices = []

    try:
        with DDGS() as ddgs:
            for query in search_queries:
                results = list(ddgs.text(query, max_results=5))
                for r in results:
                    text = r.get("body", "")
                    all_prices.extend(extract_all_prices(text))
    except Exception as e:
        await update.message.reply_text(f"❌ Web-Fehler: {e}")
        return

    avg_price = avg(all_prices)

    msg = f"⛽ *Geschätzte Preise in {wohnort}*\n"
    msg += "-----------------\n"
    if avg_price:
        msg += f"📊 Durchschnitt: {avg_price} €\n"
        msg += f"🔎 Gefundene Werte: {len(all_prices)}\n"
    else:
        msg += "❌ Keine Preise gefunden\n"

    msg += "-----------------\n_Quelle: Web-Schätzung_"
    await update.message.reply_text(msg, parse_mode="Markdown")

# -----------------------------
# 🔧 Raw-Daten für brain_log / RICS
# -----------------------------
async def get_benzin_raw() -> dict | None:
    """Gibt günstigste Spritpreise im Umkreis zurück — für brain_log & RICS-Kontext."""
    if not API_KEY:
        return None
    wohnort = os.getenv("WOHNORT", "")
    coords  = get_coordinates(wohnort) if wohnort else None
    if not coords:
        return None
    lat, lng = coords
    try:
        url = (
            "https://creativecommons.tankerkoenig.de/json/list.php"
            f"?lat={lat}&lng={lng}&rad={RADIUS}&type=all&sort=price&apikey={API_KEY}"
        )
        res = requests.get(url, timeout=5).json()
        if not res.get("ok"):
            return None
        stations = res.get("stations", [])
        if not stations:
            return None

        def cheapest(fuel):
            vals = [s.get(fuel) for s in stations if s.get(fuel)]
            return round(min(vals), 3) if vals else None

        return {
            "e5_min":     cheapest("e5"),
            "e10_min":    cheapest("e10"),
            "diesel_min": cheapest("diesel"),
        }
    except Exception:
        return None

# -----------------------------
# ⚙️ Status
# -----------------------------
async def get_status():
    return {
        "status": "active",
        "module": "benzin_preis",
        "features": ["live_api", "fallback", "multi_fuel", "dynamic_location"]
    }

# -----------------------------
# 🔌 Setup
# -----------------------------
handle_benzin.description = "Tankpreise — /benzin, /benzin [Stadt], /benzin [e5|e10|diesel], /benzin [Stadt] [Kraftstoff]"
handle_benzin.category = "Autonom"

def setup(app: Application):
    app.add_handler(CommandHandler("benzin", handle_benzin))