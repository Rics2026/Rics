#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import re
from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler
from core.brain import Brain
from dateparser import parse

load_dotenv()

# ---------------- PFAD & MEMORY ----------------
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEMORY_DIR = os.path.join(PROJECT_DIR, "memory")
if not os.path.exists(MEMORY_DIR):
    os.makedirs(MEMORY_DIR)

AGENDA_FILE = os.path.join(MEMORY_DIR, "agenda.json")
AGENDA_SETTINGS_FILE = os.path.join(MEMORY_DIR, "agenda_settings.json")
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Berlin"))

# ---------------- SETTINGS ----------------
def load_settings() -> dict:
    if not os.path.exists(AGENDA_SETTINGS_FILE):
        return {"daily_time": "07:00"}
    try:
        with open(AGENDA_SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"daily_time": "07:00"}

def save_settings(data: dict):
    with open(AGENDA_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

# ---------------- HILFSFUNKTIONEN ----------------
def escape_md(text: str) -> str:
    escape_chars = r"_*[]()~`>#+\-=|{}.!"
    return re.sub(f"([{re.escape(escape_chars)}])", r"\\\1", str(text))

def load_agenda():
    if not os.path.exists(AGENDA_FILE):
        return []
    try:
        with open(AGENDA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # alle Termine zu aware datetime konvertieren
            for item in data:
                dt_obj = datetime.fromisoformat(item['date'])
                if dt_obj.tzinfo is None:
                    item['date'] = dt_obj.replace(tzinfo=TIMEZONE).isoformat()
            return data
    except:
        return []

def save_agenda(data):
    with open(AGENDA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def parse_date(d: str, t: str, brain: Brain) -> datetime:
    """Datum parsen, Uhrzeit über Brain-Zeit"""
    now = brain.get_current_datetime()
    d_clean = d.lower().replace("nächste woche ", "nächster ")
    date_str = f"{d_clean} {t}"
    
    dt = parse(date_str, languages=['de'], settings={
        'PREFER_DATES_FROM': 'future',
        'RELATIVE_BASE': now,
        'RETURN_AS_TIMEZONE_AWARE': True
    })

    if not dt:
        # fallback auf datetime
        try:
            current_year = now.year
            fmt_d = d.strip() if d.strip().endswith('.') else d.strip() + "."
            dt = datetime.strptime(f"{fmt_d}{current_year} {t.strip()}", "%d.%m.%Y %H:%M").replace(tzinfo=TIMEZONE)
        except:
            dt = now

    # Uhrzeit-Korrektur
    try:
        h, m = map(int, t.strip().split(':'))
        dt = dt.replace(hour=h, minute=m, second=0, microsecond=0)
    except:
        pass

    return dt

# ---------------- COMMANDS ----------------
async def agenda_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    brain: Brain = context.application.bot_data.get("brain")
    agenda = load_agenda()
    if not agenda:
        return await update.message.reply_text(escape_md("📅 Deine Agenda ist aktuell leer."))

    agenda.sort(key=lambda x: x['date'])
    lines = ["📅 *DEINE TERMINE*", escape_md("￣￣￣￣￣￣￣￣￣￣￣￣￣")]
    for i, item in enumerate(agenda, 1):
        try:
            dt_obj = datetime.fromisoformat(item['date'])
            dt_str = dt_obj.strftime('%d.%m. %H:%M')
            lines.append(rf"{i}\. `{escape_md(dt_str)}` — *{escape_md(item['task'])}*")
        except:
            continue
    await update.message.reply_text("\n".join(lines))

async def agenda_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    brain: Brain = context.application.bot_data.get("brain")
    if len(context.args) < 3:
        return await update.message.reply_text("Syntax: /termin_add TT.MM. HH:MM Text")

    try:
        d, t = context.args[0], context.args[1]
        task_text = " ".join(context.args[2:])
        task_date = parse_date(d, t, brain)

        agenda = load_agenda()
        agenda.append({"date": task_date.isoformat(), "task": task_text, "reminded": False})
        save_agenda(agenda)

        await update.message.reply_text(rf"✅ Termin gespeichert: *{task_text}*")
    except Exception as e:
        await update.message.reply_text(f"❌ Fehler: {str(e)}")

async def agenda_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return
    try:
        idx = int(context.args[0]) - 1
        agenda = load_agenda()
        agenda.sort(key=lambda x: x['date'])
        if 0 <= idx < len(agenda):
            removed = agenda.pop(idx)
            save_agenda(agenda)
            await update.message.reply_text(rf"🗑 Gelöscht: *{removed['task']}*")
    except:
        await update.message.reply_text("❌ Ungültiger Index.")

# ---------------- REMINDER ----------------
async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    brain: Brain = context.application.bot_data.get("brain")
    now = brain.get_current_datetime()

    agenda = load_agenda()
    changed = False
    chat_id = os.getenv("CHAT_ID")

    if chat_id:
        for item in agenda:
            task_date = datetime.fromisoformat(item['date'])
            if task_date.tzinfo is None:
                task_date = task_date.replace(tzinfo=TIMEZONE)

            if now < task_date <= (now + timedelta(hours=4)) and not item.get('reminded'):
                msg = rf"🔔 *TERMIN\-ERINNERUNG*\n\n📌 *{item['task']}*\n⏰ `{task_date.strftime('%H:%M')} Uhr`"
                await context.bot.send_message(chat_id=chat_id, text=msg)
                item['reminded'] = True
                changed = True

    # alte Termine löschen (>24h)
    new_agenda = []
    for item in agenda:
        dt_obj = datetime.fromisoformat(item['date'])
        if dt_obj.tzinfo is None:
            dt_obj = dt_obj.replace(tzinfo=TIMEZONE)
        if dt_obj > (now - timedelta(days=1)):
            new_agenda.append(item)
    if changed or len(new_agenda) != len(agenda):
        save_agenda(new_agenda)

# ---------------- TÄGLICHE ÜBERSICHT ----------------
async def daily_agenda_summary(context: ContextTypes.DEFAULT_TYPE):
    """Schickt jeden Morgen eine Zusammenfassung aller heutigen Termine."""
    brain: Brain = context.application.bot_data.get("brain")
    now = brain.get_current_datetime()
    today = now.date()
    chat_id = os.getenv("CHAT_ID")
    if not chat_id:
        return

    agenda = load_agenda()
    todays = []
    for item in agenda:
        try:
            dt_obj = datetime.fromisoformat(item['date'])
            if dt_obj.tzinfo is None:
                dt_obj = dt_obj.replace(tzinfo=TIMEZONE)
            if dt_obj.date() == today:
                todays.append((dt_obj, item['task']))
        except:
            continue

    todays.sort(key=lambda x: x[0])

    if not todays:
        msg = escape_md("📅 Heute stehen keine Termine an. Freier Tag! 🙌")
        await context.bot.send_message(chat_id=chat_id, text=msg)
        return

    lines = [
        "☀️ *GUTEN MORGEN \– DEINE TERMINE HEUTE*",
        escape_md("￣￣￣￣￣￣￣￣￣￣￣￣￣￣￣￣￣￣￣"),
    ]
    for i, (dt_obj, task) in enumerate(todays, 1):
        time_str = dt_obj.strftime('%H:%M')
        lines.append(rf"{i}\. ⏰ `{escape_md(time_str)}` — *{escape_md(task)}*")
    lines.append(escape_md(f"\n📌 {len(todays)} Termin(e) heute – viel Erfolg!"))

    await context.bot.send_message(
        chat_id=chat_id,
        text="\n".join(lines),
        parse_mode="MarkdownV2"
    )

async def set_daily_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Setzt die tägliche Erinnerungszeit. Syntax: /termin_uhrzeit HH:MM"""
    if not context.args:
        settings = load_settings()
        current = settings.get("daily_time", "07:00")
        return await update.message.reply_text(
            escape_md(f"⏰ Aktuelle Erinnerungszeit: {current} Uhr\nÄndern mit: /termin_uhrzeit HH:MM")
        )
    try:
        raw = context.args[0].strip()
        h, m = map(int, raw.split(":"))
        assert 0 <= h <= 23 and 0 <= m <= 59
    except:
        return await update.message.reply_text("❌ Ungültig. Bitte Format HH:MM nutzen, z.B. /termin_uhrzeit 08:30")

    settings = load_settings()
    settings["daily_time"] = f"{h:02d}:{m:02d}"
    save_settings(settings)

    # Alten Job entfernen und neu planen
    jq = context.application.job_queue
    for job in jq.get_jobs_by_name("agenda_daily_summary"):
        job.schedule_removal()

    jq.run_daily(
        daily_agenda_summary,
        time=dt_time(hour=h, minute=m, tzinfo=TIMEZONE),
        name="agenda_daily_summary"
    )

    await update.message.reply_text(escape_md(f"✅ Tägliche Erinnerung ab jetzt um {h:02d}:{m:02d} Uhr gespeichert."))

# ---------------- WIEDERKEHR-ERINNERUNGEN ----------------
def _schedule_reminder(jq, rid: str, h: int, m: int, text: str):
    """Plant einen einzelnen täglichen Reminder-Job."""
    job_name = f"reminder_{rid}"
    for job in jq.get_jobs_by_name(job_name):
        job.schedule_removal()

    async def _fire(ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = os.getenv("CHAT_ID")
        if chat_id:
            await ctx.bot.send_message(
                chat_id=chat_id,
                text=escape_md(f"⏰ Erinnerung: {ctx.job.data}"),
                parse_mode="MarkdownV2"
            )

    jq.run_daily(
        _fire,
        time=dt_time(hour=h, minute=m, tzinfo=TIMEZONE),
        name=job_name,
        data=text
    )

async def reminder_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Täglich wiederkehrende Erinnerung. Syntax: /erinnerung HH:MM Text"""
    if len(context.args) < 2:
        return await update.message.reply_text("Syntax: /erinnerung HH:MM Dein Text")
    try:
        raw = context.args[0].strip()
        h, m = map(int, raw.split(":"))
        assert 0 <= h <= 23 and 0 <= m <= 59
    except:
        return await update.message.reply_text("❌ Ungültige Uhrzeit. Format: HH:MM")

    text = " ".join(context.args[1:])
    settings = load_settings()
    reminders = settings.get("reminders", [])

    import uuid
    rid = uuid.uuid4().hex[:8]
    reminders.append({"id": rid, "time": f"{h:02d}:{m:02d}", "text": text})
    settings["reminders"] = reminders
    save_settings(settings)

    _schedule_reminder(context.application.job_queue, rid, h, m, text)
    await update.message.reply_text(escape_md(f'✅ Täglich um {h:02d}:{m:02d} Uhr: "{text}"'))

async def reminder_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Listet alle täglichen Erinnerungen auf."""
    settings = load_settings()
    reminders = settings.get("reminders", [])
    if not reminders:
        return await update.message.reply_text(escape_md("📋 Keine Erinnerungen gespeichert."))
    lines = ["🔁 *TÄGLICHE ERINNERUNGEN*", escape_md("￣￣￣￣￣￣￣￣￣￣￣￣￣")]
    for i, r in enumerate(reminders, 1):
        lines.append(rf"{i}\. ⏰ `{escape_md(r['time'])}` — {escape_md(r['text'])}")
    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")

async def reminder_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Löscht eine tägliche Erinnerung per Index. Syntax: /erinnerung_del 1"""
    if not context.args:
        return await update.message.reply_text("Syntax: /erinnerung_del <Nummer>")
    try:
        idx = int(context.args[0]) - 1
        settings = load_settings()
        reminders = settings.get("reminders", [])
        if not (0 <= idx < len(reminders)):
            return await update.message.reply_text("❌ Ungültiger Index.")
        removed = reminders.pop(idx)
        settings["reminders"] = reminders
        save_settings(settings)
        # Job entfernen
        for job in context.application.job_queue.get_jobs_by_name(f"reminder_{removed['id']}"):
            job.schedule_removal()
        await update.message.reply_text(escape_md(f"🗑 Erinnerung gelöscht: {removed['text']}"))
    except Exception as e:
        await update.message.reply_text(f"❌ Fehler: {e}")

# ---------------- SETUP ----------------
def setup(app):
    app.add_handler(CommandHandler("agenda", agenda_list))
    app.add_handler(CommandHandler("termin_add", agenda_add))
    app.add_handler(CommandHandler("termin_del", agenda_del))
    app.add_handler(CommandHandler("termin_uhrzeit", set_daily_time))
    app.add_handler(CommandHandler("erinnerung", reminder_add))
    app.add_handler(CommandHandler("erinnerungen", reminder_list))
    app.add_handler(CommandHandler("erinnerung_del", reminder_del))
    if app.job_queue:
        app.job_queue.run_repeating(check_reminders, interval=300, first=10)

        # Tägliche Morgen-Zusammenfassung – Uhrzeit aus memory/agenda_settings.json
        settings = load_settings()
        raw_time = settings.get("daily_time", "07:00")
        try:
            h, m = map(int, raw_time.strip().split(":"))
        except:
            h, m = 7, 0
        app.job_queue.run_daily(
            daily_agenda_summary,
            time=dt_time(hour=h, minute=m, tzinfo=TIMEZONE),
            name="agenda_daily_summary"
        )

        # Gespeicherte Wiederkehr-Erinnerungen laden
        for r in settings.get("reminders", []):
            try:
                rh, rm = map(int, r["time"].split(":"))
                _schedule_reminder(app.job_queue, r["id"], rh, rm, r["text"])
            except Exception:
                pass

# ---------------- METADATEN ----------------
agenda_list.description = "Zeigt alle Termine aus der Agenda"
agenda_list.category = "Agenda"

agenda_add.description = "Fügt einen Termin hinzu"
agenda_add.category = "Agenda"

agenda_del.description = "Löscht einen Termin"
agenda_del.category = "Agenda"

set_daily_time.description = "Setzt die tägliche Erinnerungszeit (z.B. /termin_uhrzeit 08:00)"
set_daily_time.category = "Agenda"

reminder_add.description = "Tägl. Erinnerung hinzufügen (z.B. /erinnerung 16:00 Singen Üben)"
reminder_add.category = "Agenda"

reminder_list.description = "Alle täglichen Erinnerungen anzeigen"
reminder_list.category = "Agenda"

reminder_del.description = "Tägliche Erinnerung löschen (z.B. /erinnerung_del 1)"
reminder_del.category = "Agenda"