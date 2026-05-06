import os
import json
import logging
from datetime import datetime, time
from pathlib import Path
from typing import Optional, Dict, List, Any
import httpx
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler, Application

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MEMORY_DIR = Path("memory")
MEMORY_DIR.mkdir(exist_ok=True)
STATUS_FILE = MEMORY_DIR / "wetteralarm_status.json"
PENDING_ALERTS_FILE = MEMORY_DIR / "wetteralarm_pending.json"

WOHNORT = os.getenv("WOHNORT", "")
WETTER_TOKEN = os.getenv("WETTER_TOKEN", "")
PAYPAL_NIGHT_START_STR = os.getenv("PAYPAL_NIGHT_START", "22")
PAYPAL_NIGHT_END_STR = os.getenv("PAYPAL_NIGHT_END", "06")

NIGHT_START = None
NIGHT_END = None
for fmt in ["%H:%M", "%H"]:
    try:
        NIGHT_START = datetime.strptime(PAYPAL_NIGHT_START_STR, fmt).time()
        break
    except ValueError:
        continue
for fmt in ["%H:%M", "%H"]:
    try:
        NIGHT_END = datetime.strptime(PAYPAL_NIGHT_END_STR, fmt).time()
        break
    except ValueError:
        continue
if NIGHT_START is None:
    NIGHT_START = time(22, 0)
if NIGHT_END is None:
    NIGHT_END = time(6, 0)

def load_status() -> Dict[str, Any]:
    default = {"active": True, "last_check": None, "last_alerts": []}
    if not STATUS_FILE.exists():
        save_status(default)
        return default
    try:
        with open(STATUS_FILE, "r") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return default
            data.setdefault("active", True)
            data.setdefault("last_check", None)
            data.setdefault("last_alerts", [])
            return data
    except Exception as e:
        logger.error(f"Fehler beim Laden des Status: {e}")
        return default

def save_status(status: Dict[str, Any]) -> None:
    try:
        with open(STATUS_FILE, "w") as f:
            json.dump(status, f, indent=2)
    except Exception as e:
        logger.error(f"Fehler beim Speichern des Status: {e}")

def load_pending_alerts() -> List[Dict[str, Any]]:
    if not PENDING_ALERTS_FILE.exists():
        return []
    try:
        with open(PENDING_ALERTS_FILE, "r") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            return []
    except Exception as e:
        logger.error(f"Fehler beim Laden ausstehender Alerts: {e}")
        return []

def save_pending_alerts(alerts: List[Dict[str, Any]]) -> None:
    try:
        with open(PENDING_ALERTS_FILE, "w") as f:
            json.dump(alerts, f, indent=2)
    except Exception as e:
        logger.error(f"Fehler beim Speichern ausstehender Alerts: {e}")

def is_night_time() -> bool:
    now = datetime.now().time()
    if NIGHT_START < NIGHT_END:
        return NIGHT_START <= now < NIGHT_END
    else:
        return now >= NIGHT_START or now < NIGHT_END

async def fetch_weather() -> Optional[Dict[str, Any]]:
    if not WETTER_TOKEN or not WOHNORT:
        logger.error("WETTER_TOKEN oder WOHNORT nicht gesetzt.")
        return None
    url = "https://api.openweathermap.org/data/2.5/weather"
    params = {
        "q": WOHNORT,
        "appid": WETTER_TOKEN,
        "units": "metric",
        "lang": "de"
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            if data.get("cod") != 200:
                logger.error(f"API-Fehler: {data.get('message', 'Unbekannt')}")
                return None
            return data
    except httpx.RequestError as e:
        logger.error(f"Netzwerkfehler bei Wetter-API: {e}")
        return None
    except Exception as e:
        logger.error(f"Unerwarteter Fehler bei Wetter-API: {e}")
        return None

def analyze_weather(data: Dict[str, Any]) -> List[str]:
    alerts = []
    weather_id = data.get("weather", [{}])[0].get("id", 0)
    wind_speed = data.get("wind", {}).get("speed", 0.0)
    if 200 <= weather_id <= 232:
        alerts.append("⚡ Gewitter")
    if weather_id in [502, 503, 504, 511, 522, 531]:
        alerts.append("🌧️ Starkregen")
    if weather_id in [602, 622]:
        alerts.append("❄️ Schnee/Blizzard")
    if weather_id in [771, 781]:
        alerts.append("🌪️ Extremwetter")
    if wind_speed > 10.8:
        alerts.append("💨 Starker Wind (>10.8 m/s)")
    return alerts

async def send_alert(context: ContextTypes.DEFAULT_TYPE, alert_text: str) -> None:
    chat_id = os.getenv("CHAT_ID")
    if not chat_id:
        logger.error("CHAT_ID nicht gesetzt.")
        return
    try:
        await context.bot.send_message(chat_id=chat_id, text=alert_text)
    except Exception as e:
        logger.error(f"Fehler beim Senden der Alert-Nachricht: {e}")

async def heartbeat_wetteralarm(context: ContextTypes.DEFAULT_TYPE) -> None:
    status = load_status()
    if not status.get("active", True):
        return
    data = await fetch_weather()
    if data is None:
        return
    now_str = datetime.now().isoformat()
    status["last_check"] = now_str
    # Aktuelle Warnungen immer speichern
    alerts = analyze_weather(data)
    status["last_alerts"] = alerts
    save_status(status)
    if not alerts:
        return
    last_alerts = status.get("last_alerts", [])
    new_alerts = [a for a in alerts if a not in last_alerts]
    if not new_alerts:
        return
    alert_text = f"⚠️ WETTERALARM für {WOHNORT}:\n" + "\n".join(new_alerts)
    if is_night_time():
        pending = load_pending_alerts()
        pending.append({"time": now_str, "text": alert_text})
        save_pending_alerts(pending)
        logger.info("Nachtruhe - Alert gepuffert.")
    else:
        await send_alert(context, alert_text)

async def flush_pending_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    pending = load_pending_alerts()
    if not pending:
        return
    chat_id = os.getenv("CHAT_ID")
    if not chat_id:
        logger.error("CHAT_ID nicht gesetzt.")
        return
    alert_text = "📬 Ausstehende Wetterwarnungen (Nachtruhe):\n"
    for item in pending:
        alert_text += f"\n{item['text']}"
    try:
        await context.bot.send_message(chat_id=chat_id, text=alert_text)
        save_pending_alerts([])
    except Exception as e:
        logger.error(f"Fehler beim Senden ausstehender Alerts: {e}")

async def wetteralarm_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        status = load_status()
        active = status.get("active", True)
        last_check = status.get("last_check")
        last_alerts = status.get("last_alerts", [])
        pending = load_pending_alerts()
        active_str = "✅ AKTIV" if active else "❌ INAKTIV"
        last_check_str = last_check if last_check else "Nie"
        pending_count = len(pending)

        if last_alerts:
            alert_list = "\n".join(f"  • {a}" for a in last_alerts)
            alert_section = f"⚠️ Aktive Warnungen:\n{alert_list}"
        else:
            alert_section = "⚠️ Aktive Warnungen: Keine"

        text = (
            f"🌤️ Wetteralarm Status\n"
            f"￣￣￣￣￣￣￣￣￣￣￣￣￣\n"
            f"📡 Status: {active_str}\n"
            f"📍 Ort: {WOHNORT}\n"
            f"⏱️ Letzte Prüfung: {last_check_str}\n"
            f"{alert_section}\n"
            f"📥 Gepufferte Warnungen: {pending_count}\n"
            f"🌙 Nachtruhe: {NIGHT_START.strftime('%H:%M')} - {NIGHT_END.strftime('%H:%M')}\n"
            f"￣￣￣￣￣￣￣￣￣￣￣￣￣\n"
            f"📅 Stand: {datetime.now().strftime('%H:%M:%S')}"
        )
        await update.message.reply_text(text)
    except Exception as e:
        await update.message.reply_text(f"❌ Fehler beim Status: {e}")

wetteralarm_status.description = "Zeigt den Status des Wetteralarms an."
wetteralarm_status.category = "Monitor"

async def wetteralarm_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        status = load_status()
        status["active"] = True
        save_status(status)
        await update.message.reply_text("✅ Wetteralarm aktiviert.")
    except Exception as e:
        await update.message.reply_text(f"❌ Fehler: {e}")

wetteralarm_on.description = "Aktiviert den Wetteralarm."
wetteralarm_on.category = "Monitor"

async def wetteralarm_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        status = load_status()
        status["active"] = False
        save_status(status)
        await update.message.reply_text("✅ Wetteralarm deaktiviert.")
    except Exception as e:
        await update.message.reply_text(f"❌ Fehler: {e}")

wetteralarm_off.description = "Deaktiviert den Wetteralarm."
wetteralarm_off.category = "Monitor"

def setup(app: Application):
    app.add_handler(CommandHandler("wetteralarm_status", wetteralarm_status))
    app.add_handler(CommandHandler("wetteralarm_on", wetteralarm_on))
    app.add_handler(CommandHandler("wetteralarm_off", wetteralarm_off))
    job_queue = app.job_queue
    if job_queue:
        job_queue.run_repeating(heartbeat_wetteralarm, interval=1800, first=10)
        now = datetime.now()
        flush_time = datetime.combine(now.date(), NIGHT_END)
        if flush_time < now:
            flush_time = flush_time.replace(day=now.day + 1)
        job_queue.run_daily(flush_pending_alerts, time=flush_time.time())
    else:
        logger.error("JobQueue nicht verfügbar.")