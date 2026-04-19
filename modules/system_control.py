import os
from dotenv import load_dotenv
load_dotenv()

BOT_NAME = os.getenv("BOT_NAME", "RICS")
import subprocess
import threading
import time
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

# -----------------------------
# KERNEL RESTART LOGIK
# -----------------------------
def _perform_rics_restart():
    """Signalisiert dem Watchdog einen Neustart via Exit-Code 42."""
    time.sleep(2)  # kurze Pause für Telegram-Feedback
    os._exit(42)

async def restart_rics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Startet den Bot neu, lädt alle Module frisch."""
    await update.message.reply_text(
        f"♻️ **{BOT_NAME} KERNEL-REBOOT**\nInitialisiere Kernschmelze und Neuaufbau, Sir..."
    )
    threading.Thread(target=_perform_rics_restart).start()

# -----------------------------
# OLLAMA RESTART LOGIK (macOS)
# -----------------------------
async def restart_ollama(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Beendet Ollama via AppleScript und startet es neu."""
    await update.message.reply_text(
        "🧠 **OLLAMA REBOOT**\nStarte LLM-Backend neu..."
    )
    try:
        # Ollama beenden
        subprocess.run(["osascript", "-e", 'quit app "Ollama"'], check=False)
        time.sleep(3)
        # Ollama neu starten
        subprocess.run(["open", "-a", "Ollama"], check=True)
        await update.message.reply_text("✅ Ollama-Dienst ist wieder online, Sir.")
    except Exception as e:
        await update.message.reply_text(f"❌ Fehler bei Ollama-Restart: {e}")

# -----------------------------
# METADATEN
# -----------------------------
restart_rics.description = "Startet den RICS-Bot (Kernel) neu (lädt alle Module frisch)"
restart_rics.category = "System"

restart_ollama.description = "Startet die Ollama-App auf macOS neu"
restart_ollama.category = "LLM"

# -----------------------------
# SETUP
# -----------------------------
def setup(app):
    app.add_handler(CommandHandler("restart", restart_rics))
    app.add_handler(CommandHandler("ollamarestart", restart_ollama))