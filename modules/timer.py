# timer.py – RICS Timer-Modul
# Befehle: /timer_start, /timer_list, /timer_stop

import os
from datetime import datetime
from zoneinfo import ZoneInfo
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler

TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Berlin"))

# ─────────────────────────────────────────────
# INTERNER ZUSTAND  (in bot_data gespeichert)
# Key: "timers" → dict{ job_name → label }
# ─────────────────────────────────────────────

def _get_timers(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.application.bot_data.setdefault("timers", {})


# ─────────────────────────────────────────────
# JOB-CALLBACK  –  wird vom JobQueue aufgerufen
# ─────────────────────────────────────────────

async def _timer_fire(context: ContextTypes.DEFAULT_TYPE):
    job       = context.job
    chat_id   = job.data["chat_id"]
    label     = job.data["label"]
    job_name  = job.data["job_name"]

    # Timer aus der internen Liste entfernen
    timers = context.application.bot_data.get("timers", {})
    timers.pop(job_name, None)

    # Telegram senden — schlägt fehl wenn chat_id vom Web-Interface kommt
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ *{label}* ist fertig!",
            parse_mode="Markdown"
        )
    except Exception:
        pass  # Web-Chat hat keine Telegram-chat_id — web_push übernimmt

    # Web-Push — immer versuchen (Fallback + gleichzeitige Benachrichtigung)
    try:
        from web_app import web_push
        web_push(f"⏰ {label} ist fertig!")
    except Exception:
        try:
            from modules.web_app import web_push
            web_push(f"⏰ {label} ist fertig!")
        except Exception:
            pass


# ─────────────────────────────────────────────
# /timer_start  <minuten> <beschreibung...>
# ─────────────────────────────────────────────

async def timer_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        return await update.message.reply_text(
            "❌ Nutzung: /timer_start <Minuten> <Beschreibung>\n"
            "Beispiel: /timer_start 10 Pizza fertig"
        )

    # Erste Argument = Minuten
    try:
        minutes = int(context.args[0])
        if minutes <= 0:
            raise ValueError
    except ValueError:
        return await update.message.reply_text("❌ Bitte eine positive Zahl für die Minuten angeben.")

    label    = " ".join(context.args[1:])
    chat_id  = update.effective_chat.id
    now      = datetime.now(tz=TIMEZONE)
    job_name = f"timer_{chat_id}_{now.strftime('%H%M%S')}_{label[:10].replace(' ','_')}"

    # Job in die Queue eintragen
    context.application.job_queue.run_once(
        _timer_fire,
        when=minutes * 60,
        data={"chat_id": chat_id, "label": label, "job_name": job_name},
        name=job_name
    )

    # Intern speichern
    _get_timers(context)[job_name] = {
        "label":    label,
        "minutes":  minutes,
        "chat_id":  chat_id,
        "started":  now.strftime("%H:%M:%S")
    }

    await update.message.reply_text(
        f"✅ Timer gestartet!\n"
        f"🏷 *{label}*\n"
        f"⏱ In *{minutes} Minute{'n' if minutes != 1 else ''}* erinnere ich dich.",
        parse_mode="Markdown"
    )


# ─────────────────────────────────────────────
# /timer_list  –  alle laufenden Timer
# ─────────────────────────────────────────────

async def timer_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    timers  = _get_timers(context)
    chat_id = update.effective_chat.id

    # Nur Timer dieses Chats anzeigen
    eigene = {k: v for k, v in timers.items() if v.get("chat_id") == chat_id}

    if not eigene:
        return await update.message.reply_text("📭 Keine aktiven Timer.")

    # Restzeit aus der JobQueue berechnen
    laufende_jobs = {j.name: j for j in context.application.job_queue.jobs()}

    lines = []
    for i, (job_name, info) in enumerate(eigene.items(), 1):
        job = laufende_jobs.get(job_name)
        if job and job.next_t:
            remaining_sec = max(0, (job.next_t - datetime.now(tz=job.next_t.tzinfo)).total_seconds())
            mins_left = int(remaining_sec // 60)
            secs_left = int(remaining_sec % 60)
            rest = f"{mins_left}:{secs_left:02d} min"
        else:
            rest = "abgelaufen"
        lines.append(f"{i}. ⏱ *{info['label']}* – noch {rest}")

    # Abbrechen-Buttons
    buttons = [
        [InlineKeyboardButton(
            f"❌ #{i} {info['label'][:20]}",
            callback_data=f"timer_stop:{job_name}"
        )]
        for i, (job_name, info) in enumerate(eigene.items(), 1)
    ]

    text = "⏰ *Aktive Timer:*\n\n" + "\n".join(lines)
    markup = InlineKeyboardMarkup(buttons) if buttons else None

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)


# ─────────────────────────────────────────────
# /timer_stop  –  alle Timer dieses Chats stoppen
# ─────────────────────────────────────────────

async def timer_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    timers  = _get_timers(context)
    chat_id = update.effective_chat.id
    eigene  = [k for k, v in timers.items() if v.get("chat_id") == chat_id]

    if not eigene:
        return await update.message.reply_text("📭 Keine aktiven Timer zum Stoppen.")

    count = 0
    for job_name in eigene:
        for job in context.application.job_queue.jobs():
            if job.name == job_name:
                job.schedule_removal()
                count += 1
        timers.pop(job_name, None)

    await update.message.reply_text(f"🛑 {count} Timer gestoppt.")


# ─────────────────────────────────────────────
# CALLBACK  –  einzelnen Timer per Button stoppen
# ─────────────────────────────────────────────

async def timer_stop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    await query.answer()

    job_name = query.data.split(":", 1)[1]
    timers   = _get_timers(context)
    info     = timers.pop(job_name, None)

    for job in context.application.job_queue.jobs():
        if job.name == job_name:
            job.schedule_removal()
            break

    label = info["label"] if info else job_name
    await query.edit_message_text(f"🛑 Timer *{label}* abgebrochen.", parse_mode="Markdown")


# ─────────────────────────────────────────────
# HELP META
# ─────────────────────────────────────────────

timer_start.description = "⏱ Timer starten (/timer_start 10 Pizza fertig)"
timer_start.category    = "Agenda"

timer_list.description  = "📋 Alle laufenden Timer anzeigen"
timer_list.category     = "Agenda"

timer_stop.description  = "🛑 Alle Timer stoppen"
timer_stop.category     = "Agenda"


# ─────────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────────

def setup(app):
    app.add_handler(CommandHandler("timer_start", timer_start))
    app.add_handler(CommandHandler("timer_list",  timer_list))
    app.add_handler(CommandHandler("timer_stop",  timer_stop))
    app.add_handler(CallbackQueryHandler(timer_stop_callback, pattern=r"^timer_stop:"))