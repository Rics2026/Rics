import os
import time
import json
import psutil
from datetime import datetime
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

BOT_NAME = os.getenv("BOT_NAME", "RICS")


def get_uptime():
    boot_time = psutil.boot_time()
    uptime = time.time() - boot_time
    days, rem = divmod(uptime, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    return f"{int(days)}d {int(hours)}h {int(minutes)}m"


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cpu  = psutil.cpu_percent(interval=0.5)
    ram  = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    net  = psutil.net_io_counters()
    uptime_str = get_uptime()

    msg = (
        f"🖥 *{BOT_NAME} SYSTEM DIAGNOSTICS*\n"
        "￣￣￣￣￣￣￣￣￣￣￣￣￣\n"
        f"⏱ *Uptime:* `{uptime_str}`\n"
        f"🌡 *CPU Load:* `{cpu}%` (Avg)\n"
        f"🧠 *RAM:* `{ram.percent}%` ({ram.used // 1024**2}MB / {ram.total // 1024**2}MB)\n"
        f"💽 *Disk:* `{disk.percent}%` ({disk.free // 1024**3}GB frei)\n"
        f"🌐 *Net:* ↑{net.bytes_sent // 1024**2}MB | ↓{net.bytes_recv // 1024**2}MB\n"
        "￣￣￣￣￣￣￣￣￣￣￣￣￣\n"
        f"🤖 *Modell:* `{os.getenv('OLLAMA_MODEL', 'Standard')}`\n"
        f"📅 *Stand:* `{datetime.now().strftime('%H:%M:%S')}`"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

    # --- In brain_log schreiben damit der Bot später darauf antworten kann ---
    try:
        brain = context.bot_data.get("brain")
        if brain:
            stats = {
                "ram_percent":  ram.percent,
                "ram_used_mb":  ram.used // 1024**2,
                "ram_total_mb": ram.total // 1024**2,
                "cpu_percent":  cpu,
                "disk_percent": disk.percent,
                "disk_free_gb": disk.free // 1024**3,
                "net_sent_mb":  net.bytes_sent // 1024**2,
                "net_recv_mb":  net.bytes_recv // 1024**2,
                "uptime":       uptime_str,
            }
            brain.log_data_sync(stats, extra={"source": "status_command"})
    except Exception as e:
        print(f"[status] brain_log Fehler: {e}")


status_command.description = "Detailliertes Systemfenster aufrufen"
status_command.category = "Monitor"


def setup(app):
    app.add_handler(CommandHandler("status", status_command))