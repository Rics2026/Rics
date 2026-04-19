import os

BOT_NAME = os.getenv("BOT_NAME", "RICS")
import requests
import re
from datetime import datetime
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

def escape_md(text: str) -> str:
    """Escaped kritische MarkdownV2 Zeichen."""
    if text is None: return ""
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!])", r"\\\1", str(text))

# --- HILFSFUNKTION FÜR DATEN-ABRUF ---
async def get_weather_data_raw(city: str = None):
    """Holt die reinen Wetterdaten. city=None → WOHNORT aus .env."""
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

# --- DER BEFEHL FÜR TELEGRAM /wetter [ort] ---
async def weather_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_chat_action("typing")

    # Ort aus Args oder Fallback auf .env
    city = " ".join(context.args).strip() if context.args else None
    res = await get_weather_data_raw(city=city)

    if not res:
        ort_hint = city or os.getenv("WOHNORT", "?")
        return await update.message.reply_text(
            f"❌ Wetter-Daten für *{escape_md(ort_hint)}* nicht verfügbar\\.",
            parse_mode="MarkdownV2"
        )

    temp = res["main"]["temp"]
    feels_like = res["main"]["feels_like"]
    desc = res["weather"][0]["description"].capitalize()
    hum = res["main"]["humidity"]
    wind = res["wind"]["speed"]
    press = res["main"]["pressure"]
    city_name = res["name"]

    msg = (
        f"🛰 *{BOT_NAME} METEO REPORT*\n"
        "￣￣￣￣￣￣￣￣￣￣￣￣￣\n"
        f"📍 *Ort:* {escape_md(city_name)}\n"
        f"🌡 *Temperatur:* `{temp}°C`\n"
        f"🤔 *Gefühlt wie:* `{feels_like}°C`\n"
        f"✨ *Zustand:* _{escape_md(desc)}_\n"
        "￣￣￣￣￣￣￣￣￣￣￣￣￣\n"
        f"💧 *Feuchte:* `{hum}%`\n"
        f"🌬 *Wind:* `{wind} m/s`\n"
        f"⏲ *Luftdruck:* `{press} hPa`\n"
        "￣￣￣￣￣￣￣￣￣￣￣￣￣\n"
        f"📅 `Update: {datetime.now().strftime('%H:%M:%S')}`"
    )

    await update.message.reply_text(msg, parse_mode='MarkdownV2')

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
weather_command.description = "Wetter-Report — /wetter oder /wetter [Stadt]"
weather_command.category = "Wetter"

# --- SETUP FÜR BOT ---
def setup(app):
    app.add_handler(CommandHandler("wetter", weather_command))