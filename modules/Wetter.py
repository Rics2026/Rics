import os

BOT_NAME = os.getenv("BOT_NAME", "RICS")
import requests
import re
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler
from telegram.error import BadRequest

def escape_md(text: str) -> str:
    """Escaped kritische MarkdownV2 Zeichen."""
    if text is None: return ""
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!])", r"\\\1", str(text))

# --- HILFSFUNKTIONEN FÜR DATEN-ABRUF ---
async def get_weather_data_raw(city: str = None):
    """Holt das aktuelle Wetter. city=None → WOHNORT aus .env."""
    token = os.getenv("WETTER_TOKEN")
    if not city:
        city = os.getenv("WOHNORT")
    if not token or not city:
        return None

    try:
        url = f"http://api.openweathermap.org/data/2.5/weather?q={city}&appid={token}&units=metric&lang=de"
        res = requests.get(url, timeout=5).json()
        if str(res.get("cod")) == "200":
            return res
    except:
        return None
    return None

async def get_forecast_data_raw(city: str = None):
    """Holt die 5-Tage/3h-Vorhersage. city=None → WOHNORT aus .env."""
    token = os.getenv("WETTER_TOKEN")
    if not city:
        city = os.getenv("WOHNORT")
    if not token or not city:
        return None

    try:
        url = f"http://api.openweathermap.org/data/2.5/forecast?q={city}&appid={token}&units=metric&lang=de"
        res = requests.get(url, timeout=5).json()
        if str(res.get("cod")) == "200":
            return res
    except:
        return None
    return None

def _get_day_summary(forecast_data, day_offset):
    """
    Extrahiert Min/Max-Temp, häufigste Wetterbeschreibung und 
    durchschnittlichen Wind für den Tag (0=heute, 1=morgen).
    """
    target_date = (datetime.now() + timedelta(days=day_offset)).date()
    temps = []
    winds = []
    desc_counts = {}
    for item in forecast_data.get("list", []):
        item_date = datetime.fromtimestamp(item["dt"]).date()
        if item_date == target_date:
            temps.append(item["main"]["temp"])
            winds.append(item["wind"]["speed"])
            desc = item["weather"][0]["description"]
            desc_counts[desc] = desc_counts.get(desc, 0) + 1
    if not temps:
        return None
    temp_min = min(temps)
    temp_max = max(temps)
    avg_wind = sum(winds) / len(winds) if winds else 0
    main_desc = max(desc_counts, key=desc_counts.get)
    return {
        "temp_min": round(temp_min, 1),
        "temp_max": round(temp_max, 1),
        "description": main_desc.capitalize(),
        "wind": round(avg_wind, 1)
    }

def build_forecast_text(city_name, forecast_data, day_offset):
    summary = _get_day_summary(forecast_data, day_offset)
    if not summary:
        return None
    if day_offset == 0:
        day_label = "heute"
    elif day_offset == 1:
        day_label = "morgen"
    else:
        day_label = f"in {day_offset} Tagen"
    text = (
        f"▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔\n"
        f"📅 **Vorhersage {day_label} für {escape_md(city_name)}:**\n"
        f"🌡 Min `{escape_md(str(summary['temp_min']))}°C` / Max `{escape_md(str(summary['temp_max']))}°C`\n"
        f"☁ {escape_md(summary['description'])}\n"
        f"🌬 Wind ~ `{escape_md(str(summary['wind']))} m/s`\n"
        f"▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔"
    )
    return text

# --- DER BEFEHL FÜR TELEGRAM /wetter [ort] [morgen] ---
async def weather_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_chat_action("typing")

    args = " ".join(context.args).strip() if context.args else ""
    # Prüfen auf "morgen" als eigenes Wort (nicht Teilstring)
    if re.search(r'\bmorgen\b', args.lower()):
        day_offset = 1
        city = re.sub(r'\smorgen\s*$', '', args, flags=re.IGNORECASE).strip()
        if not city:
            city = None
    else:
        day_offset = 0
        city = args if args else None

    weather_res = await get_weather_data_raw(city=city)
    if not weather_res:
        ort_hint = city or os.getenv("WOHNORT", "?")
        try:
            return await update.message.reply_text(
                f"❌ Wetter-Daten für *{escape_md(ort_hint)}* nicht verfügbar\\.",
                parse_mode="MarkdownV2"
            )
        except BadRequest:
            return await update.message.reply_text(
                f"❌ Wetter-Daten für {ort_hint} nicht verfügbar."
            )
    city_name = weather_res["name"]

    forecast_res = await get_forecast_data_raw(city=city)
    forecast_text = None
    if forecast_res:
        forecast_text = build_forecast_text(city_name, forecast_res, day_offset)

    if day_offset == 0:
        # Aktuelles Wetter + Vorhersage für heute
        temp = weather_res["main"]["temp"]
        feels_like = weather_res["main"]["feels_like"]
        desc = weather_res["weather"][0]["description"].capitalize()
        hum = weather_res["main"]["humidity"]
        wind = weather_res["wind"]["speed"]
        press = weather_res["main"]["pressure"]

        msg = (
            f"🛰 *{BOT_NAME} METEO REPORT*\n"
            f"▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔\n"
            f"📍 *Ort:* {escape_md(city_name)}\n"
            f"🌡 *Temperatur:* `{escape_md(str(temp))}°C`\n"
            f"🤔 *Gefühlt:* `{escape_md(str(feels_like))}°C`\n"
            f"☁ *Zustand:* _{escape_md(desc)}_\n"
            f"▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔\n"
            f"💧 *Feuchte:* `{hum}%`\n"
            f"🌬 *Wind:* `{escape_md(str(wind))} m/s`\n"
            f"⏲ *Luftdruck:* `{press} hPa`\n"
            f"▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔\n"
            f"📅 `Update: {datetime.now().strftime('%H:%M:%S')}`\n"
        )
        if forecast_text:
            msg += "\n" + forecast_text
    else:
        # Nur Vorhersage für morgen
        if forecast_text:
            msg = forecast_text
        else:
            msg = f"❌ Für *{escape_md(city_name)}* ist keine Vorhersage für morgen verfügbar\\."

    try:
        await update.message.reply_text(msg, parse_mode='MarkdownV2')
    except BadRequest:
        await update.message.reply_text(msg, parse_mode=None)

# --- SCHNITTSTELLE FÜR BOT (Proactive Brain / Session Manager) ---
async def get_status():
    res = await get_weather_data_raw()
    if not res:
        return "Wetterdienst nicht erreichbar."
    temp = res["main"]["temp"]
    desc = res["weather"][0]["description"].capitalize()
    city_name = res["name"]
    return f"Wetter in {city_name}: {temp}°C, {desc}."

# --- METADATEN FÜR HILFE / MODULE ---
weather_command.description = "Wetter-Report — /wetter oder /wetter [Stadt] oder /wetter morgen oder /wetter [Stadt] morgen"
weather_command.category = "Wetter"

# --- SETUP FÜR BOT ---
def setup(app):
    app.add_handler(CommandHandler("wetter", weather_command))