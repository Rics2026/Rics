#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hotline.py — Zentraler Watchdog & Echtzeit-Trigger für RICS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Überwacht eigenständig (kein Modul muss angefasst werden):
    • Solar / Strom   — Netzbezug, Einspeisung
    • Wetter          — Frost, Hitze, Sturm
    • System          — CPU, RAM, Disk
    • Agenda          — Termin in 30 Min
    • Benzin          — Günstiger Preis-Alert (1x täglich)

Zusätzlich zwei externe Trigger-Wege:
    1. memory/hotline.json  — externe Skripte schreiben rein
    2. push_event()         — andere RICS-Module importieren und rufen direkt auf

Nachtruhe: PAYPAL_NIGHT_START / PAYPAL_NIGHT_END aus .env

Commands:
    /hotline        → Status aller Checks + letzter Event
    /hotline_help   → Snippet für externe Trigger
"""

import os
import sys
import json
import logging
import asyncio
import time
import psutil
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram.ext import ContextTypes, CommandHandler

load_dotenv()

log = logging.getLogger(__name__)

# ── Pfade ──────────────────────────────────────────────────────────────────────
_THIS   = os.path.dirname(os.path.abspath(__file__))
PROJECT = _THIS if not _THIS.endswith("modules") else os.path.dirname(_THIS)

HOTLINE_FILE = os.path.join(PROJECT, "memory", "hotline.json")
AGENDA_FILE  = os.path.join(PROJECT, "memory", "agenda.json")
LOG_DIR      = os.path.join(PROJECT, "logs")
CHAT_ID      = os.getenv("CHAT_ID", "")
POLL_INTERVAL = 2  # Sekunden für Datei/Queue-Check

NIGHT_START = int(os.getenv("PAYPAL_NIGHT_START", "22"))
NIGHT_END   = int(os.getenv("PAYPAL_NIGHT_END",   "8"))

# ══════════════════════════════════════════════════════════════════════════════
# SCHWELLWERTE — hier anpassen
# ══════════════════════════════════════════════════════════════════════════════

SOLAR_NETZBEZUG_WARN  = 800     # W  — Alarm wenn Netzbezug über diesem Wert
SOLAR_EINSPEISUNG_MIN = 500     # W  — Info wenn Einspeisung unter diesem Wert fällt

WETTER_FROST_TEMP     = 2       # °C — Alarm wenn Temperatur darunter
WETTER_HITZE_TEMP     = 33      # °C — Alarm wenn Temperatur darüber
WETTER_STURM_WIND     = 12      # m/s — Alarm wenn Wind darüber

STATUS_CPU_WARN       = 85      # %  — Alarm wenn CPU darüber
STATUS_RAM_WARN       = 85      # %  — Alarm wenn RAM darüber
STATUS_DISK_WARN      = 90      # %  — Alarm wenn Disk darüber

AGENDA_VORWARN_MIN    = 30      # Min — Alarm wenn Termin in X Minuten

BENZIN_E10_SCHWELLE   = 1.60    # €  — Alert wenn E10 günstiger als dieser Wert
BENZIN_DIESEL_SCHWELLE= 1.50    # €  — Alert wenn Diesel günstiger als dieser Wert

# ── Check-Intervalle (Sekunden) ────────────────────────────────────────────────
INTERVAL_SOLAR   = 60
INTERVAL_WETTER  = 600   # 10 Min
INTERVAL_STATUS  = 60
INTERVAL_AGENDA  = 300   # 5 Min
INTERVAL_BENZIN  = 21600 # 6 Std

# ── Cooldowns — verhindert Spam (Sekunden) ────────────────────────────────────
COOLDOWN_SOLAR   = 1800  # 30 Min
COOLDOWN_WETTER  = 3600  # 1 Std
COOLDOWN_STATUS  = 3600  # 1 Std
COOLDOWN_AGENDA  = 1800  # 30 Min
COOLDOWN_BENZIN  = 86400 # 1 Tag

# ── Interner Zustand ──────────────────────────────────────────────────────────
_event_queue: list = []
_last_event:  dict = {}
_last_check:  dict = {}   # key → letzter Check-Zeitpunkt
_last_fired:  dict = {}   # key → letzter Alert-Zeitpunkt
_seen_termine: set = set()


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC API — von anderen Modulen nutzbar
# ══════════════════════════════════════════════════════════════════════════════

def push_event(msg: str, icon: str = "🔔", source: str = ""):
    """
    Direkt-Push aus anderen RICS-Modulen:
        from modules.hotline import push_event
        push_event("Nachricht", icon="☀️", source="solar")
    """
    _event_queue.append({
        "msg": msg, "icon": icon, "source": source,
        "ts": datetime.now().strftime("%H:%M:%S"),
    })


# ══════════════════════════════════════════════════════════════════════════════
# TIMING-HELFER
# ══════════════════════════════════════════════════════════════════════════════

def _due(key: str, interval: int) -> bool:
    """True wenn der Check für diesen Key fällig ist."""
    return time.time() - _last_check.get(key, 0) >= interval

def _cooled(key: str, cooldown: int) -> bool:
    """True wenn der Alert-Cooldown abgelaufen ist."""
    return time.time() - _last_fired.get(key, 0) >= cooldown

def _mark_check(key: str):
    _last_check[key] = time.time()

def _mark_fired(key: str):
    _last_fired[key] = time.time()


# ══════════════════════════════════════════════════════════════════════════════
# LOG-HELFER
# ══════════════════════════════════════════════════════════════════════════════

def _log_to_chatlog(text: str):
    try:
        today    = datetime.now().strftime("%Y-%m-%d")
        log_file = os.path.join(LOG_DIR, f"{today}.log")
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"BOT: {text}\n")
    except Exception as e:
        log.warning(f"Hotline Chatlog-Fehler: {e}")

def _log_to_chromadb(text: str):
    try:
        import chromadb
        from chromadb.utils import embedding_functions
        client = chromadb.PersistentClient(path=os.path.join(PROJECT, "memory", "vectors"))
        embed  = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        col    = client.get_or_create_collection(name="user_memory", embedding_function=embed)
        col.add(documents=[f"RICS_ANTWORT: {text}"], ids=[f"hotline_{time.time()}"])
    except Exception as e:
        log.warning(f"Hotline ChromaDB-Fehler: {e}")

def _web_push(msg: str):
    try:
        from modules.web_app import web_push
        web_push(msg)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# SEND-HELPER
# ══════════════════════════════════════════════════════════════════════════════

async def _send(context: ContextTypes.DEFAULT_TYPE, msg: str, icon: str = "🔔", source: str = "", ts: str = ""):
    global _last_event
    if not msg.strip():
        return
    ts = ts or datetime.now().strftime("%H:%M:%S")
    source_line = f"\n_von: {source}_" if source else ""
    text = f"{icon} *Hotline* `[{ts}]`\n{msg}{source_line}"
    try:
        await context.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")
        _log_to_chatlog(f"{icon} {msg}")
        _log_to_chromadb(f"{icon} {msg}")
        _web_push(f"{icon} {msg}")
        _last_event = {"msg": msg, "icon": icon, "ts": ts}
    except Exception as e:
        log.warning(f"Hotline send-Fehler: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# WATCHDOG-CHECKS
# ══════════════════════════════════════════════════════════════════════════════

async def _check_solar(context):
    if not _due("solar", INTERVAL_SOLAR):
        return
    _mark_check("solar")
    try:
        from modules.solar import get_live_power_raw
        data = await get_live_power_raw()
        if not data:
            return
        power = data["power"]

        if power > SOLAR_NETZBEZUG_WARN and _cooled("solar_bezug", COOLDOWN_SOLAR):
            await _send(context,
                f"Hoher Netzbezug: *{power:.0f} W*\nDu ziehst gerade viel Strom aus dem Netz.",
                icon="⚡", source="solar")
            _mark_fired("solar_bezug")

        elif power < 0 and abs(power) < SOLAR_EINSPEISUNG_MIN and _cooled("solar_einsp", COOLDOWN_SOLAR):
            await _send(context,
                f"Geringe Einspeisung: *{abs(power):.0f} W*\nSolar produziert kaum.",
                icon="☀️", source="solar")
            _mark_fired("solar_einsp")

    except Exception as e:
        log.debug(f"Solar-Check Fehler: {e}")


async def _check_wetter(context):
    if not _due("wetter", INTERVAL_WETTER):
        return
    _mark_check("wetter")
    try:
        from modules.Wetter import get_weather_data_raw
        res = await get_weather_data_raw()
        if not res:
            return
        temp = res["main"]["temp"]
        wind = res["wind"]["speed"]
        desc = res["weather"][0]["description"].capitalize()

        if temp <= WETTER_FROST_TEMP and _cooled("wetter_frost", COOLDOWN_WETTER):
            await _send(context,
                f"Frost-Warnung: *{temp:.1f}°C* in {res['name']}\n{desc}",
                icon="🧊", source="wetter")
            _mark_fired("wetter_frost")

        elif temp >= WETTER_HITZE_TEMP and _cooled("wetter_hitze", COOLDOWN_WETTER):
            await _send(context,
                f"Hitze-Warnung: *{temp:.1f}°C* in {res['name']}\n{desc}",
                icon="🔥", source="wetter")
            _mark_fired("wetter_hitze")

        if wind >= WETTER_STURM_WIND and _cooled("wetter_sturm", COOLDOWN_WETTER):
            await _send(context,
                f"Sturm-Warnung: *{wind:.1f} m/s* Wind in {res['name']}",
                icon="🌪️", source="wetter")
            _mark_fired("wetter_sturm")

    except Exception as e:
        log.debug(f"Wetter-Check Fehler: {e}")


async def _check_status(context):
    if not _due("status", INTERVAL_STATUS):
        return
    _mark_check("status")
    try:
        cpu  = psutil.cpu_percent(interval=0.3)
        ram  = psutil.virtual_memory().percent
        disk = psutil.disk_usage("/").percent

        if cpu > STATUS_CPU_WARN and _cooled("status_cpu", COOLDOWN_STATUS):
            await _send(context,
                f"CPU-Auslastung kritisch: *{cpu:.0f}%*",
                icon="🌡️", source="system")
            _mark_fired("status_cpu")

        if ram > STATUS_RAM_WARN and _cooled("status_ram", COOLDOWN_STATUS):
            await _send(context,
                f"RAM-Auslastung kritisch: *{ram:.0f}%*",
                icon="🧠", source="system")
            _mark_fired("status_ram")

        if disk > STATUS_DISK_WARN and _cooled("status_disk", COOLDOWN_STATUS):
            await _send(context,
                f"Disk-Auslastung kritisch: *{disk:.0f}%* — wenig Speicher frei!",
                icon="💽", source="system")
            _mark_fired("status_disk")

    except Exception as e:
        log.debug(f"Status-Check Fehler: {e}")


async def _check_agenda(context):
    if not _due("agenda", INTERVAL_AGENDA):
        return
    _mark_check("agenda")
    try:
        if not os.path.exists(AGENDA_FILE):
            return
        with open(AGENDA_FILE, "r", encoding="utf-8") as f:
            termine = json.load(f)
        now     = datetime.now()
        bald    = now + timedelta(minutes=AGENDA_VORWARN_MIN)

        for t in termine:
            datum_str = t.get("date") or t.get("datum", "")
            zeit_str  = t.get("time") or t.get("zeit", "00:00")
            titel     = t.get("text") or t.get("titel", "Termin")
            tid       = t.get("id") or datum_str + titel

            if not datum_str:
                continue
            try:
                termin_dt = datetime.fromisoformat(f"{datum_str}T{zeit_str}")
            except Exception:
                continue

            if now <= termin_dt <= bald and tid not in _seen_termine:
                if _cooled(f"agenda_{tid}", COOLDOWN_AGENDA):
                    minuten = int((termin_dt - now).total_seconds() / 60)
                    await _send(context,
                        f"Termin in *{minuten} Min*: {titel}\n🕐 {zeit_str} Uhr",
                        icon="📅", source="agenda")
                    _seen_termine.add(tid)
                    _mark_fired(f"agenda_{tid}")

    except Exception as e:
        log.debug(f"Agenda-Check Fehler: {e}")


async def _check_benzin(context):
    if not _due("benzin", INTERVAL_BENZIN):
        return
    _mark_check("benzin")
    try:
        from modules.auto_benzin import get_coordinates, API_KEY, RADIUS, WOHNORT
        if not API_KEY or not WOHNORT:
            return
        coords = get_coordinates(WOHNORT)
        if not coords:
            return
        lat, lng = coords
        url = (
            f"https://creativecommons.tankerkoenig.de/json/list.php"
            f"?lat={lat}&lng={lng}&rad={RADIUS}&type=all&sort=price&apikey={API_KEY}"
        )
        res  = requests.get(url, timeout=8).json()
        if not res.get("ok"):
            return
        stations = res.get("stations", [])
        if not stations:
            return

        # Billigsten E10 und Diesel finden
        e10_preise     = [s["e10"] for s in stations if s.get("e10") and s["e10"] > 0]
        diesel_preise  = [s["diesel"] for s in stations if s.get("diesel") and s["diesel"] > 0]

        alerts = []
        if e10_preise:
            min_e10 = min(e10_preise)
            if min_e10 < BENZIN_E10_SCHWELLE:
                alerts.append(f"E10: *{min_e10:.2f}€* (unter {BENZIN_E10_SCHWELLE:.2f}€)")
        if diesel_preise:
            min_diesel = min(diesel_preise)
            if min_diesel < BENZIN_DIESEL_SCHWELLE:
                alerts.append(f"Diesel: *{min_diesel:.2f}€* (unter {BENZIN_DIESEL_SCHWELLE:.2f}€)")

        if alerts and _cooled("benzin_preis", COOLDOWN_BENZIN):
            await _send(context,
                "Günstiger Sprit in der Nähe:\n" + "\n".join(alerts),
                icon="⛽", source="benzin")
            _mark_fired("benzin_preis")

    except Exception as e:
        log.debug(f"Benzin-Check Fehler: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# DATEI-HELFER
# ══════════════════════════════════════════════════════════════════════════════

def _read() -> list:
    if not os.path.exists(HOTLINE_FILE):
        return []
    try:
        with open(HOTLINE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []

def _write(data: list):
    os.makedirs(os.path.dirname(HOTLINE_FILE), exist_ok=True)
    with open(HOTLINE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ══════════════════════════════════════════════════════════════════════════════
# POLLING JOB
# ══════════════════════════════════════════════════════════════════════════════

async def _poll(context: ContextTypes.DEFAULT_TYPE):
    # Nachtruhe
    stunde = datetime.now().hour
    if NIGHT_START <= stunde or stunde < NIGHT_END:
        return

    # 1. Interner Push-Puffer (push_event() aus Modulen)
    while _event_queue:
        e = _event_queue.pop(0)
        await _send(context, e["msg"], e["icon"], e["source"], e["ts"])

    # 2. Datei-basiert (externe Skripte via hotline.json)
    entries  = _read()
    pending  = [e for e in entries if not e.get("processed", False)]
    done     = [e for e in entries if e.get("processed", False)]
    for e in pending:
        await _send(context, e.get("msg",""), e.get("icon","🔔"), e.get("source",""), e.get("ts",""))
        e["processed"] = True
    if pending:
        _write((done + pending)[-50:])

    # 3. Watchdog-Checks (alle haben eigene Intervalle + Cooldowns)
    await _check_solar(context)
    await _check_wetter(context)
    await _check_status(context)
    await _check_agenda(context)
    await _check_benzin(context)


# ══════════════════════════════════════════════════════════════════════════════
# COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

async def hotline_status(update, context: ContextTypes.DEFAULT_TYPE):
    now = time.time()

    def next_check(key, interval):
        diff = interval - (now - _last_check.get(key, 0))
        return f"in {max(0,int(diff))}s" if diff > 0 else "jetzt fällig"

    def last_alert(key, cooldown):
        diff = now - _last_fired.get(key, 0)
        if diff > cooldown * 2:
            return "—"
        ago = int(diff / 60)
        return f"vor {ago} Min"

    entries = _read()
    pending = [e for e in entries if not e.get("processed", False)]
    last    = (
        f"{_last_event.get('icon','')} {_last_event.get('msg','')[:50]} `[{_last_event.get('ts','')}]`"
        if _last_event else "Noch kein Event."
    )

    msg = (
        f"📡 *Hotline Watchdog*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ Solar     — {next_check('solar',  INTERVAL_SOLAR)}\n"
        f"🌤 Wetter    — {next_check('wetter', INTERVAL_WETTER)}\n"
        f"🖥 System    — {next_check('status', INTERVAL_STATUS)}\n"
        f"📅 Agenda    — {next_check('agenda', INTERVAL_AGENDA)}\n"
        f"⛽ Benzin    — {next_check('benzin', INTERVAL_BENZIN)}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Letzte Alerts:\n"
        f"  Solar:  {last_alert('solar_bezug', COOLDOWN_SOLAR)}\n"
        f"  Wetter: {last_alert('wetter_frost', COOLDOWN_WETTER)}\n"
        f"  System: {last_alert('status_cpu', COOLDOWN_STATUS)}\n"
        f"  Benzin: {last_alert('benzin_preis', COOLDOWN_BENZIN)}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Queue: {len(pending)} Datei | {len(_event_queue)} intern\n"
        f"Nachtruhe: {NIGHT_START}:00–{NIGHT_END:02d}:00\n\n"
        f"Letzter Event:\n{last}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def hotline_help(update, context: ContextTypes.DEFAULT_TYPE):
    snippet = (
        "📡 *Hotline — Event pushen*\n\n"
        "*Terminal:*\n"
        "```bash\n"
        "python3 modules/hotline.py --push 'Nachricht' '🔔' 'quelle'\n"
        "```\n\n"
        "*Aus RICS-Modul:*\n"
        "```python\n"
        "from modules.hotline import push_event\n"
        "push_event('Nachricht', icon='🔔', source='quelle')\n"
        "```\n\n"
        "*Schwellwerte anpassen:*\n"
        "Direkt in `modules/hotline.py` oben unter `SCHWELLWERTE`"
    )
    await update.message.reply_text(snippet, parse_mode="Markdown")


hotline_status.description = "Hotline Watchdog Status"
hotline_status.category    = "Monitor"
hotline_help.description   = "Snippet für Hotline-Trigger"
hotline_help.category      = "Monitor"


# ══════════════════════════════════════════════════════════════════════════════
# SETUP
# ══════════════════════════════════════════════════════════════════════════════

def setup(app):
    if app.job_queue:
        app.job_queue.run_repeating(
            _poll, interval=POLL_INTERVAL, first=10, name="hotline_poll"
        )
    app.add_handler(CommandHandler("hotline",      hotline_status))
    app.add_handler(CommandHandler("hotline_help", hotline_help))


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if "--push" in sys.argv:
        idx  = sys.argv.index("--push")
        args = sys.argv[idx + 1:]
        msg  = args[0] if len(args) > 0 else "Event"
        icon = args[1] if len(args) > 1 else "🔔"
        src  = args[2] if len(args) > 2 else ""
        data = _read()
        data.append({"msg": msg, "icon": icon, "source": src,
                     "ts": datetime.now().strftime("%H:%M:%S"), "processed": False})
        _write(data)
        print(f"✅ Hotline-Event gepusht: {icon} {msg}")
    else:
        print("Nutze: python3 hotline.py --push 'Nachricht' '🔔' 'source'")