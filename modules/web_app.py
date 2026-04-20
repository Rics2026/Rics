import os
import sys
import json
import asyncio
import threading
import httpx
import logging
from datetime import datetime
from flask import Flask, request, jsonify, session, redirect, Response
from dotenv import load_dotenv, set_key

logger = logging.getLogger(__name__)

# ── Werkzeug Log-Filter: Scanner-Spam unterdrücken ────────────────────────
class _BotPathFilter(logging.Filter):
    _NOISE = (
        "php", "cgi-bin", "eval-stdin", "phpunit", "vendor/",
        "wp-", ".env", "setup.php", "install", "invokefunction",
        "containers/json", "pearcmd", "allow_url_include",
    )
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if any(p in msg for p in self._NOISE):
            return False
        return True

logging.getLogger("werkzeug").addFilter(_BotPathFilter())

BASE_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEMORY_DIR = os.path.join(BASE_DIR, "memory")
ENV_PATH   = os.path.join(BASE_DIR, ".env")

sys.path.append(BASE_DIR)
os.makedirs(MEMORY_DIR, exist_ok=True)
load_dotenv(ENV_PATH)

app = Flask(__name__)
app.secret_key = os.getenv("WEB_SECRET_KEY", "RICS_BRIDGE_SECRET_KEY_2026")

# ── Globale Instanzen (werden von bot.py via setup() gesetzt) ──────────────
jarvis_instance = None
brain_instance  = None
_telegram_app   = None   # Telegram Application — für Command-Router

# ── Web Push System ───────────────────────────────────────────────────────
import queue as _queue
_push_clients: list[_queue.Queue] = []   # eine Queue pro offener SSE-Verbindung
_push_lock = threading.Lock()

# ── FakeContext-Support (geteilt zwischen Command- und Callback-Route) ───
# user_data/chat_data persistieren modul-global, damit z.B. /models gefolgt
# von /model 1 dieselbe available_models-Liste sieht (wie in Telegram).
_web_user_data: dict = {}
_web_chat_data: dict = {}

# Threading-Flag: wenn aktiv (True), ist gerade ein Web-Handler am Laufen.
# web_push() unterdrückt dann den Push — sonst kommt die Antwort 2x
# (einmal inline via /chat-Stream, einmal via /push-stream).
_in_web_handler = threading.local()

def _push_log_append(text: str):
    """Schreibt Push-Nachricht in die tägliche chat.log."""
    try:
        log_dir = os.path.join(BASE_DIR, "logs")
        os.makedirs(log_dir, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        with open(os.path.join(log_dir, f"{today}.log"), "a", encoding="utf-8") as lf:
            lf.write(f"[PUSH] RICS: {text}\n")
    except Exception:
        pass

def web_push(text: str, buttons=None):
    """Schickt Nachricht an alle Webchat-Verbindungen und persistiert sie."""
    # Wenn gerade ein Web-Handler läuft (Command oder Callback aus /chat),
    # unterdrücken — die Antwort kommt bereits inline via /chat-Stream.
    # Sonst erscheint sie 2x (inline + push).
    if getattr(_in_web_handler, "active", False):
        return
    _push_log_append(text)
    if not _push_clients:
        return
    payload = {"push": text}
    if buttons:
        payload["buttons"] = buttons
    data = "data: " + json.dumps(payload) + "\n\n"
    with _push_lock:
        dead = []
        for q in _push_clients:
            try:
                q.put_nowait(data)
            except _queue.Full:
                dead.append(q)
        for q in dead:
            _push_clients.remove(q)


def _handle_builtin_command(cmd: str, args: list):
    """
    Web-eigene Befehle die keinen Telegram-Handler brauchen.
    Gibt Text zurück oder None wenn kein Match.
    """
    if not jarvis_instance:
        return None

    if cmd == "ichnbin":
        try:
            return jarvis_instance.personal.as_text()
        except Exception as e:
            return f"❌ Fehler: {e}"

    if cmd == "reset":
        try:
            jarvis_instance.memory.reset()
            jarvis_instance.chat_history = []
            return "⚠️ Chat-Verlauf gelöscht. Persönliche Daten bleiben erhalten."
        except Exception as e:
            return f"❌ Fehler: {e}"

    if cmd == "merke":
        text = " ".join(args)
        if "=" not in text:
            return "📝 Syntax: /merke <key> = <wert>\nBeispiel: /merke job = Landratsamt"
        key, _, value = text.partition("=")
        key = key.strip().lower(); value = value.strip()
        if not key or not value:
            return "Key und Wert dürfen nicht leer sein."
        try:
            jarvis_instance.personal.set_fact(key, value)
            jarvis_instance.memory.add_fact(f"Sir Rene {key}: {value}")
            return f"✅ Gespeichert: {key} = {value}"
        except Exception as e:
            return f"❌ Fehler: {e}"

    if cmd == "vergiss":
        key = " ".join(args).strip().lower()
        if not key:
            return "Syntax: /vergiss <key>"
        try:
            if jarvis_instance.personal.delete_fact(key):
                return f"🗑️ Fakt '{key}' gelöscht."
            else:
                return f"❓ Kein Fakt '{key}' gefunden."
        except Exception as e:
            return f"❌ Fehler: {e}"

    if cmd == "reflexion":
        try:
            log_dir = os.path.join(BASE_DIR, "logs")
            today = datetime.now().strftime("%Y-%m-%d")
            log_file = os.path.join(log_dir, f"{today}.log")
            if not os.path.exists(log_file):
                return "Keine Logs für heute vorhanden."
            with open(log_file, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
            added = 0
            for line in lines:
                if line.startswith("USER:") or line.startswith("[WEB] USER:"):
                    text = line.split("USER:", 1)[-1].strip()
                    if jarvis_instance.memory and len(text) > 20:
                        jarvis_instance.memory.add_user(text)
                        added += 1
            return f"✅ {added} Einträge aus dem Tages-Log ins Langzeitgedächtnis übertragen."
        except Exception as e:
            return f"❌ Fehler: {e}"

    return None  # kein Builtin → Telegram-Handler oder LLM

# ── ENV Config ────────────────────────────────────────────────────────────
ENV_GROUPS = {
    # ── Bot Grundkonfiguration ──────────────────────────────────────────
    "🤖 Telegram":      [("TELEGRAM_TOKEN","password"),("CHAT_ID","text"),("BOT_NAME","text"),("USER_NAME","text")],

    # ── KI / LLM ───────────────────────────────────────────────────────
    # LLM_PROVIDER steuert den Chat-Provider: deepseek oder groq
    # Ollama wird nur noch als Notfall-Fallback genutzt
    "🧠 KI Provider":   [("LLM_PROVIDER","text"),("DEEPSEEK_API_KEY","password"),("GROQ_API_KEY","password")],

    # ── Vision (lokal, Multimodal) ──────────────────────────────────────
    # Nur für Bildanalyse (Vision.py) — läuft lokal via Ollama
    "👁️ Vision (lokal)":[("OLLAMA_VISION_MODEL","text"),("OLLAMA_MODEL","text"),("OLLAMA_KEEP_ALIVE","text")],

    # ── System & Persönliches ───────────────────────────────────────────
    "⚙️ System":        [("TIMEZONE","text"),("WOHNORT","text"),("TTS_VOICE","text"),("IMAGEMAGICK_BINARY","text")],

    # ── externe APIs ───────────────────────────────────────────────────
    "🌐 APIs":          [("WETTER_TOKEN","password"),("YOUTUBE_API_KEY","password"),("MOLTBOOK_API_KEY","password"),
                         ("MOLTBOOK_USERNAME","text"),("ECOTRACKER_IP","text"),("TANKERKOENIG_API_KEY","password"),("FLUX_API_TOKEN","password")],

    # ── Energie / Growatt Noah 2000 ─────────────────────────────────────
    "🔋 Energie":       [("GROWATT_USER","text"),("GROWATT_PASS","password"),("GROWATT_NOAH_SN","text"),
                         ("NOAH_SOC_VOLL","text"),("NOAH_SOC_LEER","text")],

    # ── Mail ────────────────────────────────────────────────────────────
    "📧 Mail":          [("MAIL_IMAP_SERVER","text"),("MAIL_ADDRESS","text"),("MAIL_PASSWORD","password"),
                         ("MAIL_CHECK_INTERVAL","text")],

    # ── Kommunikation ───────────────────────────────────────────────────
    "💬 Discord":       [("DISCORD_BOT_TOKEN","password"),("DISCORD_GUILD_ID","text"),("DISCORD_ADMIN_ID","text"),
                         ("DISCORD_BOT_CHANNEL","text")],

    # ── Briefing ────────────────────────────────────────────────────────
    "📅 Briefing":      [("BRIEFING_ZEIT","text"),("BRIEFING_AKTIV","text")],

    # ── PayPal Monitor ──────────────────────────────────────────────────
    "💳 PayPal":        [("PAYPAL_NIGHT_MODE","text"),("PAYPAL_NIGHT_START","text"),("PAYPAL_NIGHT_END","text")],

    # ── Pfade (optional) ────────────────────────────────────────────────
    "📁 Pfade":         [("MODULE_PATH","text"),("MEMORY_PATH","text"),("LOG_PATH","text"),
                         ("AGENDA_FILE","text"),("SYSTEM_PROMPT_PATH","text"),("AGENT_PROMPT_PATH","text")],

    # ── Dashboard ───────────────────────────────────────────────────────
    "🔐 Dashboard":     [("WEB_PIN","password"),("WEB_PORT","text")],
}
ALL_KEYS = [k for grp in ENV_GROUPS.values() for k, _ in grp]


# ════════════════════════════════════════════════════════════════
# HTML BUILDER  (kein f-string → kein Problem mit {done} etc.)
# ════════════════════════════════════════════════════════════════

def build_settings_html():
    load_dotenv(ENV_PATH, override=True)
    config = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    config[k.strip()] = v.strip()

    h = ""
    for grp, fields in ENV_GROUPS.items():
        h += '<div class="card"><div class="group-title">' + grp + "</div>"
        h += '<div class="settings-grid">'
        for key, ftype in fields:
            val = config.get(key, "")
            safe_val = val.replace('"', "&quot;")
            h += '<div class="field"><label>' + key + "</label>"
            if ftype == "password":
                h += '<div class="pw-wrap">'
                h += '<input type="password" name="' + key + '" value="' + safe_val + '" class="inp" id="pw_' + key + '">'
                h += '<button type="button" class="eye-btn" onclick="togglePwd(\'' + key + '\')">👁</button>'
                h += "</div>"
            else:
                h += '<input type="text" name="' + key + '" value="' + safe_val + '" class="inp">'
            h += "</div>"
        h += "</div></div>"

    extra = set(config.keys()) - set(ALL_KEYS)
    if extra:
        h += '<div class="card"><div class="group-title">Weitere</div><div class="settings-grid">'
        for key in sorted(extra):
            val = config.get(key, "")
            safe_val = val.replace('"', "&quot;")
            h += '<div class="field"><label>' + key + "</label>"
            is_secret = any(x in key.lower() for x in ["password","token","key","secret","api"])
            if is_secret:
                h += '<div class="pw-wrap">'
                h += '<input type="password" name="' + key + '" value="' + safe_val + '" class="inp" id="pw_' + key + '">'
                h += '<button type="button" class="eye-btn" onclick="togglePwd(\'' + key + '\')">👁</button>'
                h += "</div>"
            else:
                h += '<input type="text" name="' + key + '" value="' + safe_val + '" class="inp">'
            h += "</div>"
        h += "</div></div>"
    return h


def build_index_html():
    settings_html = build_settings_html()

    # Status-Werte
    provider = os.getenv("LLM_PROVIDER", "ollama")
    model    = os.getenv("OLLAMA_MODEL", "?")
    bot_name = os.getenv("BOT_NAME", "RICS")
    status   = "ONLINE" if jarvis_instance else "INIT"
    memory_ok = "✅" if (jarvis_instance and hasattr(jarvis_instance, "memory") and jarvis_instance.memory) else "⚠️"
    brain_ok  = "✅" if brain_instance else "⚠️"

    # ACHTUNG: Kein f-string hier unten, sonst crasht Python bei {done}, {value} etc.
    # Wir nutzen .replace() um dynamische Werte einzusetzen.
    html = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,shrink-to-fit=no,viewport-fit=cover">
<title>__BOT_NAME__ Dashboard</title>
<style>
:root{--c:#00d4ff;--c2:#00ff88;--c3:#7c3aed;--bg:#020617;--bg2:rgba(15,23,42,.85);--bg3:rgba(2,6,23,.7);--border:rgba(0,212,255,.25);--text:#e2e8f0;--sub:#64748b}
*{box-sizing:border-box;margin:0;padding:0}
html,body{width:100%;height:100%;overflow:hidden;max-width:100%}
body{background:var(--bg);color:var(--text);font-family:'Courier New',monospace}
body::before{content:'';position:fixed;inset:0;background:radial-gradient(ellipse 120% 80% at 50% -10%,rgba(0,212,255,.12),transparent 60%),radial-gradient(ellipse 60% 60% at 80% 100%,rgba(124,58,237,.1),transparent 50%);pointer-events:none;z-index:0}

/* Layout */
.app{display:grid;grid-template-rows:auto auto 1fr;position:fixed;top:0;left:0;width:100%;height:100vh;height:-webkit-fill-available;height:100svh;overflow:hidden;z-index:1}

/* Header */
.header{display:flex;justify-content:space-between;align-items:center;padding:1rem 2rem;background:var(--bg2);border-bottom:1px solid var(--border);backdrop-filter:blur(20px);position:sticky;top:0;z-index:100}
.logo{display:flex;align-items:center;gap:.75rem}
.logo-icon{width:36px;height:36px;background:linear-gradient(135deg,var(--c),var(--c3));border-radius:8px;display:flex;align-items:center;justify-content:center;font-weight:900;color:#000;font-size:1rem}
.logo-name{font-size:1.4rem;font-weight:700;color:var(--c);letter-spacing:3px}
.logo-sub{font-size:.65rem;color:var(--sub);letter-spacing:1px}
.header-right{display:flex;align-items:center;gap:1rem}
.status-pill{padding:.3rem .8rem;border-radius:20px;font-size:.7rem;font-weight:700;letter-spacing:1px;border:1px solid}
.status-pill.online{background:rgba(0,255,136,.15);border-color:var(--c2);color:var(--c2)}
.status-pill.init{background:rgba(255,165,0,.15);border-color:orange;color:orange}
.btn-logout{padding:.5rem 1.2rem;background:rgba(255,68,68,.15);border:1px solid rgba(255,68,68,.4);color:#ff6b6b;border-radius:8px;cursor:pointer;font-size:.8rem;font-weight:700;transition:all .2s}
.btn-logout:hover{background:rgba(255,68,68,.3)}

/* Tabs */
.tabs{display:flex;background:var(--bg2);border-bottom:1px solid var(--border);padding:0 2rem}
.tab{padding:.9rem 1.5rem;background:transparent;border:none;color:var(--sub);cursor:pointer;font-weight:700;font-size:.8rem;letter-spacing:1px;text-transform:uppercase;border-bottom:3px solid transparent;transition:all .25s;font-family:inherit}
.tab:hover{color:var(--text)}
.tab.active{color:var(--c);border-bottom-color:var(--c)}

/* Content */
.main{overflow:hidden;padding:2rem;background:transparent;display:flex;flex-direction:column;min-height:0;min-width:0}
.content{display:none}
.content.show{display:block;flex:1;height:0;overflow-y:auto;overflow-x:hidden;-webkit-overflow-scrolling:touch;min-width:0}
#chat.show{overflow:hidden;display:flex;flex-direction:column;flex:1;min-height:0;min-width:0}

/* Cards */
.card{background:var(--bg2);border:1px solid var(--border);border-radius:16px;padding:1.5rem;margin-bottom:1.5rem;backdrop-filter:blur(10px)}
.card-title{font-size:.75rem;color:var(--c);font-weight:700;text-transform:uppercase;letter-spacing:1px;margin-bottom:1.2rem;padding-bottom:.7rem;border-bottom:1px solid var(--border)}

/* Dashboard Grid */
.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:1rem;margin-bottom:1.5rem}
.stat{background:var(--bg3);border:1px solid var(--border);border-radius:12px;padding:1.2rem;text-align:center;transition:border-color .2s}
.stat:hover{border-color:var(--c)}
.stat-label{font-size:.65rem;color:var(--sub);text-transform:uppercase;letter-spacing:1px;margin-bottom:.4rem}
.stat-val{font-size:1.4rem;font-weight:700;color:var(--c)}
.stat-val.ok{color:var(--c2)}
.stat-val.warn{color:orange}

.quick-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:.8rem}
.qbtn{padding:.9rem 1rem;background:rgba(0,212,255,.07);border:1px solid var(--border);border-radius:10px;color:var(--c);cursor:pointer;font-weight:700;font-size:.8rem;text-align:center;transition:all .2s;font-family:inherit}
.qbtn:hover{background:rgba(0,212,255,.18);border-color:var(--c);transform:translateY(-2px)}
.qbtn:active{transform:translateY(0)}

/* Chat */
.chat-wrap{display:flex;flex-direction:column;height:calc(100vh - 240px);height:calc(100dvh - 240px);min-height:400px;gap:1rem}
.chatbox{flex:1;overflow-y:auto;overflow-x:hidden;-webkit-overflow-scrolling:touch;padding:1.5rem;background:var(--bg3);border:1px solid var(--border);border-radius:14px;scroll-behavior:smooth;min-width:0}
.chatbox::-webkit-scrollbar{width:6px}
.chatbox::-webkit-scrollbar-thumb{background:rgba(0,212,255,.4);border-radius:3px}
.msg{margin-bottom:1rem;padding:1rem 1.2rem;border-radius:12px;font-size:.88rem;line-height:1.65;word-wrap:break-word;overflow-wrap:break-word;word-break:break-word;max-width:100%;overflow:hidden;animation:fadeIn .3s ease}
@keyframes fadeIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
.msg.user{background:rgba(0,255,136,.1);border:1px solid rgba(0,255,136,.25);border-left:4px solid var(--c2)}
.msg.bot{background:rgba(0,212,255,.07);border:1px solid rgba(0,212,255,.2);border-left:4px solid var(--c)}
.msg .who{font-size:.7rem;font-weight:700;letter-spacing:1px;margin-bottom:.4rem;text-transform:uppercase}
.msg.user .who{color:var(--c2)}
.msg.bot .who{color:var(--c)}
.msg .ts{font-size:.65rem;color:var(--sub);margin-left:.5rem;font-weight:400}
.msg-text{white-space:pre-wrap}
.typing{display:flex;gap:5px;align-items:center;padding:.3rem 0}
.typing span{width:7px;height:7px;background:var(--c);border-radius:50%;animation:blink 1.2s infinite}
.typing span:nth-child(2){animation-delay:.2s}
.typing span:nth-child(3){animation-delay:.4s}
@keyframes blink{0%,80%,100%{opacity:.2}40%{opacity:1}}

.input-row{display:flex;gap:.75rem;align-items:center}
.msg-input{flex:1;padding:.9rem 1.2rem;background:var(--bg3);border:1.5px solid var(--border);color:var(--c);border-radius:10px;font-family:inherit;font-size:16px;transition:border-color .2s;resize:none}
.msg-input:focus{outline:none;border-color:var(--c)}
.send-btn{padding:.9rem 1.8rem;background:linear-gradient(135deg,var(--c),var(--c3));color:#000;border:none;border-radius:10px;font-weight:700;cursor:pointer;font-size:.85rem;transition:opacity .2s;white-space:nowrap}
.send-btn:hover{opacity:.85}
.send-btn:disabled{opacity:.4;cursor:not-allowed}

/* Settings */
.group-title{font-size:.75rem;color:var(--c);font-weight:700;text-transform:uppercase;letter-spacing:1px;margin-bottom:1.2rem;padding-bottom:.6rem;border-bottom:1px solid var(--border)}
.settings-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:1.2rem}
.field{display:flex;flex-direction:column;gap:.4rem}
.field label{font-size:.7rem;color:var(--c);text-transform:uppercase;font-weight:700;letter-spacing:.5px}
.inp{padding:.75rem .9rem;background:var(--bg3);border:1px solid var(--border);color:var(--c);border-radius:8px;font-family:inherit;font-size:.85rem;transition:border-color .2s;width:100%}
.inp:focus{outline:none;border-color:var(--c)}
.pw-wrap{display:flex;gap:.4rem}
.pw-wrap .inp{flex:1}
.eye-btn{padding:.5rem .7rem;background:rgba(0,212,255,.1);border:1px solid var(--border);color:var(--c);border-radius:6px;cursor:pointer;font-size:.85rem}
.eye-btn:hover{background:rgba(0,212,255,.2)}
.save-btn{width:100%;padding:1rem;background:linear-gradient(135deg,var(--c),var(--c3));color:#000;border:none;border-radius:10px;font-weight:700;cursor:pointer;font-size:.95rem;margin-top:1.5rem;letter-spacing:1px}
.save-btn:hover{opacity:.88}
.toast{position:fixed;bottom:2rem;right:2rem;padding:.9rem 1.8rem;border-radius:10px;font-weight:700;font-size:.85rem;z-index:9999;opacity:0;transition:opacity .3s;pointer-events:none}
.toast.ok{background:rgba(0,255,136,.9);color:#000}
.toast.err{background:rgba(255,68,68,.9);color:#fff}
.toast.show{opacity:1}

/* KI-Config */
.ki-textarea{width:100%;min-height:340px;padding:1rem;background:var(--bg3);border:1px solid var(--border);color:var(--c);border-radius:10px;font-family:'Courier New',monospace;font-size:.82rem;line-height:1.6;resize:vertical;transition:border-color .2s}
.ki-textarea:focus{outline:none;border-color:var(--c)}
.action-card{background:var(--bg3);border:1px solid var(--border);border-radius:10px;padding:1rem;display:grid;grid-template-columns:1fr 1fr;gap:.7rem;position:relative}
.action-card .del-btn{position:absolute;top:.6rem;right:.6rem;background:rgba(255,68,68,.15);border:1px solid rgba(255,68,68,.4);color:#ff6b6b;border-radius:6px;cursor:pointer;font-size:.75rem;padding:.3rem .6rem}
.action-card .del-btn:hover{background:rgba(255,68,68,.3)}
.action-card label{font-size:.65rem;color:var(--c);text-transform:uppercase;letter-spacing:.5px;display:block;margin-bottom:.25rem}
.action-card .inp{width:100%}
.add-action-btn{padding:.8rem 1.4rem;background:rgba(0,255,136,.12);border:1px solid rgba(0,255,136,.4);color:var(--c2);border-radius:8px;cursor:pointer;font-weight:700;font-size:.8rem;letter-spacing:1px;transition:all .2s;font-family:inherit}
.add-action-btn:hover{background:rgba(0,255,136,.22)}
.actions-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:1rem;margin-bottom:1rem}

/* Mobile */
@media(max-width:768px){
  .header{padding:.75rem 1rem}
  .logo-name{font-size:1.1rem}
  .tabs{padding:0 .5rem;overflow-x:auto;white-space:nowrap}
  .tab{padding:.75rem .9rem;font-size:.72rem}
  .main{padding:.5rem}
  .chatbox{padding:.8rem}
  .msg{padding:.75rem .9rem;font-size:.85rem}
  .input-row{gap:.4rem}
  .msg-input{padding:.75rem .8rem;font-size:16px}
  .send-btn{padding:.75rem 1rem;font-size:.8rem}
  .stats-grid{grid-template-columns:repeat(2,1fr)}
  .settings-grid{grid-template-columns:1fr}
  .actions-grid{grid-template-columns:1fr}
  .action-card{grid-template-columns:1fr}
  .ki-textarea{min-height:240px}
  .qbtn{padding:.65rem .8rem;font-size:.72rem;white-space:nowrap}
  .quick-btns::-webkit-scrollbar{display:none}
}
</style>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
</head>
<body>
<div class="app">

<!-- HEADER -->
<div class="header">
  <div class="logo">
    <div class="logo-icon">R</div>
    <div>
      <div class="logo-name">__BOT_NAME__</div>
      <div class="logo-sub">AI ASSISTANT</div>
    </div>
  </div>
  <div class="header-right">
    <div class="status-pill __STATUS_CLASS__">__STATUS__</div>
    <button class="btn-logout" onclick="if(confirm('Logout?'))location.href='/logout'">LOGOUT</button>
  </div>
</div>

<!-- TABS -->
<div class="tabs">
  <button class="tab active" onclick="switchTab(event,'dash')">Dashboard</button>
  <button class="tab" onclick="switchTab(event,'chat')">Chat</button>
  <button class="tab" onclick="switchTab(event,'sett')">Settings</button>
  <button class="tab" onclick="switchTab(event,'kiconf');loadKiConfig()">KI-Config</button>
</div>

<!-- MAIN -->
<div class="main">

  <!-- DASHBOARD -->
  <div id="dash" class="content show">
    <div class="stats-grid">
      <div class="stat"><div class="stat-label">Status</div><div class="stat-val __STATUS_CLASS__">__STATUS__</div></div>
      <div class="stat"><div class="stat-label">Provider</div><div class="stat-val">__PROVIDER__</div></div>
      <div class="stat"><div class="stat-label">Model</div><div class="stat-val" style="font-size:.95rem">__MODEL__</div></div>
      <div class="stat"><div class="stat-label">Memory</div><div class="stat-val">__MEMORY_OK__</div></div>
      <div class="stat"><div class="stat-label">Brain</div><div class="stat-val">__BRAIN_OK__</div></div>
      <div class="stat"><div class="stat-label">Zeit</div><div class="stat-val" id="clock" style="font-size:1rem">--:--</div></div>
    </div>
    <div class="card" id="ds-card">
      <div class="card-title">🔑 DeepSeek API — Guthaben &amp; Verlauf</div>
      <div id="ds-loading" style="color:var(--c2);font-size:.9rem">Lade Daten…</div>
      <div id="ds-data" style="display:none">
        <!-- Stats -->
        <div class="stats-grid" style="margin-top:.75rem">
          <div class="stat"><div class="stat-label">Gesamt</div><div class="stat-val" id="ds-total">—</div></div>
          <div class="stat"><div class="stat-label">Verfügbar</div><div class="stat-val ok" id="ds-avail">—</div></div>
          <div class="stat"><div class="stat-label">Bonus</div><div class="stat-val" style="color:#a78bfa" id="ds-granted">—</div></div>
          <div class="stat"><div class="stat-label">Status</div><div class="stat-val" id="ds-status">—</div></div>
        </div>
        <!-- Balken: Verbrauch visualisieren -->
        <div style="margin:1.2rem 0 .4rem;font-size:.7rem;color:var(--sub);text-transform:uppercase;letter-spacing:1px">Guthaben-Aufteilung</div>
        <div style="background:rgba(255,255,255,.06);border-radius:8px;height:28px;position:relative;overflow:hidden;border:1px solid var(--border)">
          <div id="ds-bar-avail" style="position:absolute;left:0;top:0;height:100%;background:linear-gradient(90deg,#00ff88,#00d4ff);border-radius:8px;transition:width .8s ease"></div>
          <div id="ds-bar-granted" style="position:absolute;top:0;height:100%;background:linear-gradient(90deg,#7c3aed,#a78bfa);border-radius:8px;transition:width .8s ease,left .8s ease"></div>
          <div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:.7rem;font-weight:700;color:#fff;letter-spacing:1px" id="ds-bar-label">—</div>
        </div>
        <div style="display:flex;gap:1.5rem;margin-top:.5rem;font-size:.7rem;color:var(--sub)">
          <span><span style="color:#00ff88">■</span> Eingezahlt</span>
          <span><span style="color:#a78bfa">■</span> Bonus</span>
          <span><span style="color:rgba(255,255,255,.2)">■</span> Verbraucht</span>
        </div>
        <!-- Chart: Verlauf -->
        <div style="margin:1.4rem 0 .4rem;font-size:.7rem;color:var(--sub);text-transform:uppercase;letter-spacing:1px">Guthaben-Verlauf <span id="ds-chart-hint" style="color:rgba(255,255,255,.25)">(wird mit jedem Aufruf aufgezeichnet)</span></div>
        <div style="position:relative;height:160px">
          <canvas id="ds-chart" style="width:100%;height:160px"></canvas>
          <div id="ds-no-history" style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:rgba(255,255,255,.25);font-size:.8rem">Noch nicht genug Datenpunkte — komm später wieder</div>
        </div>
      </div>
      <div id="ds-error" style="display:none;color:orange;font-size:.85rem;margin-top:.5rem"></div>
      <div style="margin-top:.8rem;font-size:.7rem;color:rgba(255,255,255,.25)" id="ds-updated"></div>
    </div>
  </div>

  <!-- CHAT -->
  <div id="chat" class="content">
    <div class="card" style="flex:1;min-height:0;display:flex;flex-direction:column;padding:1rem;overflow:hidden">
      <div class="chatbox" id="chatbox">
        <div class="msg bot">
          <div class="who">__BOT_NAME__ <span class="ts">Bereit</span></div>
          <div class="msg-text">Hallo! Ich bin __BOT_NAME__, dein KI-Assistent. Alle Gedächtnisse und Module sind aktiv. Was kann ich für dich tun?</div>
        </div>
      </div>
      <div id="quick-btns-container" class="quick-btns" style="display:flex;gap:.5rem;margin-top:.6rem;overflow-x:auto;flex-shrink:0;padding-bottom:.2rem">
        <span style="color:var(--sub);font-size:.75rem;padding:.5rem">Lade Befehle…</span>
      </div>
      <div class="input-row" style="margin-top:.5rem">
        <input class="msg-input" id="msg-input" type="text" placeholder="Nachricht oder /befehl (Enter zum Senden)" autocomplete="off">
        <button class="send-btn" id="send-btn" onclick="sendMsg()">SENDEN</button>
      </div>
    </div>
  </div>

  <!-- SETTINGS -->
  <div id="sett" class="content">
    <form id="settings-form">
      __SETTINGS_HTML__
      <button type="button" class="save-btn" onclick="saveSettings()">💾 EINSTELLUNGEN SPEICHERN</button>
    </form>
  </div>

  <!-- KI-CONFIG -->
  <div id="kiconf" class="content">
    <!-- Reload Button -->
    <div style="display:flex;justify-content:flex-end;margin-bottom:1rem">
      <button type="button" onclick="reloadPrompts()" style="padding:.6rem 1.4rem;background:rgba(124,58,237,.2);border:1px solid rgba(124,58,237,.5);color:#a78bfa;border-radius:8px;cursor:pointer;font-weight:700;font-size:.8rem;font-family:inherit;transition:all .2s" onmouseover="this.style.background='rgba(124,58,237,.35)'" onmouseout="this.style.background='rgba(124,58,237,.2)'">🔄 Prompts im Bot neu laden</button>
    </div>
    <!-- System Prompt -->
    <div class="card">
      <div class="card-title">📝 System-Prompt</div>
      <div style="font-size:.75rem;color:var(--sub);margin-bottom:.8rem">system_prompt.txt — Persönlichkeit &amp; Verhalten von __BOT_NAME__</div>
      <textarea class="ki-textarea" id="ki-sysprompt" spellcheck="false"></textarea>
      <button type="button" class="save-btn" style="margin-top:1rem" onclick="saveSysPrompt()">💾 SYSTEM-PROMPT SPEICHERN</button>
    </div>
    <!-- Custom Actions -->
    <div class="card">
      <div class="card-title">⚙️ Custom Actions <span style="font-size:.7rem;color:var(--sub);font-weight:400">(custom_actions.json)</span></div>
      <div style="font-size:.75rem;color:var(--sub);margin-bottom:1rem">Definiert welche Actions der Bot erkennt und an welchen Befehl sie weitergeleitet werden.</div>
      <div class="actions-grid" id="actions-grid"></div>
      <div style="display:flex;gap:.8rem;flex-wrap:wrap">
        <button type="button" class="add-action-btn" onclick="addActionCard()">＋ Action hinzufügen</button>
        <button type="button" class="save-btn" style="margin-top:0;width:auto;padding:.8rem 2rem" onclick="saveActions()">💾 ACTIONS SPEICHERN</button>
      </div>
    </div>
    <!-- Agent Prompt -->
    <div class="card">
      <div class="card-title">🤖 Agent-Prompt</div>
      <div style="font-size:.75rem;color:var(--sub);margin-bottom:.8rem">agent_prompt.txt — Steuert den autonomen Agenten-Modus (Code-Ausführung, Werkzeuge, Format)</div>
      <textarea class="ki-textarea" id="ki-agentprompt" spellcheck="false"></textarea>
      <button type="button" class="save-btn" style="margin-top:1rem" onclick="saveAgentPrompt()">💾 AGENT-PROMPT SPEICHERN</button>
    </div>
  </div>

</div><!-- /main -->
</div><!-- /app -->

<div class="toast" id="toast"></div>

<script>
// ── Visual Viewport: fixes keyboard resize on iOS Safari ─────
(function(){
  var app = document.querySelector('.app');
  function syncViewport(){
    var vv = window.visualViewport;
    if(!vv || !app) return;
    app.style.height = Math.round(vv.height) + 'px';
    app.style.width  = Math.round(vv.width)  + 'px';
    app.style.top    = Math.round(vv.offsetTop)  + 'px';
    app.style.left   = Math.round(vv.offsetLeft) + 'px';
  }
  if(window.visualViewport){
    window.visualViewport.addEventListener('resize', syncViewport);
    window.visualViewport.addEventListener('scroll', syncViewport);
  }
  syncViewport();
})();

// ── DeepSeek Balance + Chart ──────────────────────────────────
var _dsChart = null;

function renderDsChart(history, currency) {
  var canvas = document.getElementById('ds-chart');
  var noHist  = document.getElementById('ds-no-history');
  if (!history || history.length < 2) {
    if (noHist) noHist.style.display = 'flex';
    return;
  }
  if (noHist) noHist.style.display = 'none';

  var labels  = history.map(function(h){ return h.ts; });
  var totals  = history.map(function(h){ return parseFloat(h.total) || 0; });
  var avails  = history.map(function(h){
    var a = parseFloat(h.available);
    return isNaN(a) ? (parseFloat(h.total) || 0) : a;
  });

  if (_dsChart) { _dsChart.destroy(); }

  var ctx = canvas.getContext('2d');
  _dsChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: labels,
      datasets: [
        {
          label: 'Gesamt (' + currency + ')',
          data: totals,
          borderColor: '#00d4ff',
          backgroundColor: 'rgba(0,212,255,0.08)',
          borderWidth: 2,
          pointRadius: 3,
          pointBackgroundColor: '#00d4ff',
          tension: 0.4,
          fill: true,
        },
        {
          label: 'Verfügbar (' + currency + ')',
          data: avails,
          borderColor: '#00ff88',
          backgroundColor: 'rgba(0,255,136,0.06)',
          borderWidth: 2,
          pointRadius: 3,
          pointBackgroundColor: '#00ff88',
          tension: 0.4,
          fill: true,
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 600 },
      plugins: {
        legend: {
          labels: { color: '#94a3b8', font: { size: 10, family: "'Courier New', monospace" }, boxWidth: 12 }
        },
        tooltip: {
          backgroundColor: 'rgba(2,6,23,0.95)',
          borderColor: 'rgba(0,212,255,0.4)',
          borderWidth: 1,
          titleColor: '#00d4ff',
          bodyColor: '#e2e8f0',
          callbacks: {
            label: function(ctx) { return ' ' + ctx.dataset.label + ': ' + ctx.parsed.y.toFixed(4); }
          }
        }
      },
      scales: {
        x: {
          ticks: { color: '#475569', font: { size: 9 }, maxTicksLimit: 8, maxRotation: 30 },
          grid:  { color: 'rgba(255,255,255,0.04)' }
        },
        y: {
          ticks: { color: '#475569', font: { size: 9 },
            callback: function(v) { return v.toFixed(2); }
          },
          grid: { color: 'rgba(255,255,255,0.06)' }
        }
      }
    }
  });
}

async function loadDsBalance() {
  try {
    var r = await fetch('/api/deepseek-balance');
    var d = await r.json();
    document.getElementById('ds-loading').style.display = 'none';

    if (d.error) {
      document.getElementById('ds-error').textContent = '⚠️ ' + d.error;
      document.getElementById('ds-error').style.display = 'block';
      return;
    }

    var cur      = d.currency || '';
    var totalF   = parseFloat(d.total_f)     || 0;
    var availF   = parseFloat(d.available_f) || 0;
    var grantedF = parseFloat(d.granted_f)   || 0;
    var usedF    = Math.max(0, totalF - availF - grantedF);

    document.getElementById('ds-total').textContent   = parseFloat(d.total_balance).toFixed(4)   + ' ' + cur;
    document.getElementById('ds-avail').textContent   = parseFloat(d.available).toFixed(4)       + ' ' + cur;
    document.getElementById('ds-granted').textContent = parseFloat(d.granted_balance).toFixed(4) + ' ' + cur;
    document.getElementById('ds-status').textContent  = d.is_available ? '✅ Aktiv' : '❌ Gesperrt';

    // Balken
    if (totalF > 0) {
      var pctAvail   = (availF   / totalF * 100).toFixed(1);
      var pctGranted = (grantedF / totalF * 100).toFixed(1);
      var pctUsed    = Math.max(0, 100 - parseFloat(pctAvail) - parseFloat(pctGranted)).toFixed(1);
      document.getElementById('ds-bar-avail').style.width   = pctAvail + '%';
      document.getElementById('ds-bar-granted').style.left  = pctAvail + '%';
      document.getElementById('ds-bar-granted').style.width = pctGranted + '%';
      document.getElementById('ds-bar-label').textContent   = pctUsed + '% verbraucht';
    }

    document.getElementById('ds-data').style.display = 'block';

    // Chart
    renderDsChart(d.history || [], cur);

    var now = new Date();
    document.getElementById('ds-updated').textContent = 'Zuletzt aktualisiert: ' + now.toLocaleTimeString('de-DE');

  } catch(e) {
    document.getElementById('ds-loading').textContent = '⚠️ Fehler: ' + e.message;
  }
}
loadDsBalance();
setInterval(loadDsBalance, 300000); // alle 5 Minuten

// ── sendQuick: /befehl direkt in Chat senden ─────────────────
function sendQuick(cmd) {
  document.querySelectorAll('.content').forEach(function(c){c.classList.remove('show');});
  document.querySelectorAll('.tab').forEach(function(t){t.classList.remove('active');});
  document.getElementById('chat').classList.add('show');
  document.querySelectorAll('.tab')[1].classList.add('active');
  var input = document.getElementById('msg-input');
  if (input) { input.value = cmd; }
  setTimeout(sendMsg, 100);
}

// ── Dynamische Command-Buttons laden ─────────────────────────
var CMD_ICONS = {
  wetter:'🌤', dashboard:'📊', briefing:'📋', agenda:'📅',
  memory_view:'🧠', hilfe:'❓', solar:'☀️', jobs:'📝',
  reset:'🔄', ichnbin:'👤', merke:'📝', vergiss:'🗑️',
  discord:'💬', youtube:'▶️', web:'🌐', benzin:'⛽',
  look:'🔍', timer_start:'⏱️', ls:'📁', backup:'💾',
  model:'🤖', voice:'🎤', status:'📡', updater:'🔧', update:'🔧',
};
function loadDynamicCmds() {
  fetch('/api/commands', {credentials:'same-origin'})
    .then(function(r){ return r.json(); })
    .then(function(d) {
      var container = document.getElementById('quick-btns-container');
      if (!container) return;
      container.innerHTML = '';
      (d.commands || []).forEach(function(cmd) {
        var icon = CMD_ICONS[cmd] || '⚡';
        var btn = document.createElement('button');
        btn.className = 'qbtn';
        btn.style.flexShrink = '0';
        btn.textContent = icon + ' /' + cmd;
        btn.onclick = function(){ sendQuick('/' + cmd); };
        container.appendChild(btn);
      });
      if (!d.commands || d.commands.length === 0) {
        container.innerHTML = '<span style="color:var(--sub);font-size:.75rem;padding:.5rem">Keine Befehle gefunden</span>';
      }
    })
    .catch(function() {
      var c = document.getElementById('quick-btns-container');
      if (c) c.innerHTML = '';
    });
}

// ── Action-Confirm UI ────────────────────────────────────────
var _pendingAction = null;

function showActionConfirm(action) {
  _pendingAction = action;
  var box = document.getElementById('chatbox');
  var div = document.createElement('div');
  div.id = 'action-confirm-card';
  div.style.cssText = 'background:rgba(0,212,255,.08);border:1px solid rgba(0,212,255,.35);border-radius:12px;padding:1rem 1.2rem;margin:.5rem 0;font-size:.85rem;';
  // Strip HTML tags from preview
  var preview = (action.preview || '').replace(/<[^>]+>/g, '');
  div.innerHTML =
    '<div style="margin-bottom:.7rem;white-space:pre-line">' + preview + '</div>' +
    '<div style="font-style:italic;color:var(--sub);margin-bottom:.8rem;font-size:.8rem">Soll ich das ausführen?</div>' +
    '<div style="display:flex;gap:.6rem">' +
      '<button onclick="confirmAction()" style="padding:.5rem 1.2rem;background:rgba(0,255,136,.15);border:1px solid rgba(0,255,136,.4);color:#00ff88;border-radius:8px;cursor:pointer;font-weight:700;font-family:inherit">✅ Ausführen</button>' +
      '<button onclick="cancelAction()" style="padding:.5rem 1.2rem;background:rgba(255,68,68,.12);border:1px solid rgba(255,68,68,.35);color:#ff6b6b;border-radius:8px;cursor:pointer;font-weight:700;font-family:inherit">❌ Abbrechen</button>' +
    '</div>';
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

function removeActionCard() {
  var el = document.getElementById('action-confirm-card');
  if (el) el.remove();
}

function confirmAction() {
  if (!_pendingAction) return;
  removeActionCard();
  var a = _pendingAction;
  _pendingAction = null;
  if (a.exec_cmd) {
    // CUSTOM action → als Chat-Befehl senden
    sendMsg(a.exec_cmd);
  } else if (a.type && a.answer) {
    // TERMIN_ADD / DISCORD_MESSAGE → /api/action-exec
    fetch('/api/action-exec', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      credentials: 'same-origin',
      body: JSON.stringify({type: a.type, answer: a.answer})
    }).then(function(r){ return r.json(); })
      .then(function(d){ appendMsg('bot', d.result || '✅ Ausgeführt'); })
      .catch(function(e){ appendMsg('bot', '❌ Fehler: ' + e.message); });
  }
}

function cancelAction() {
  removeActionCard();
  _pendingAction = null;
  appendMsg('bot', '❌ Abgebrochen.');
}

// ── Inline Keyboard Buttons (aus Telegram reply_markup) ──────
var _lastKeyboardId = null;

function renderInlineKeyboard(rows) {
  var box = document.getElementById('chatbox');
  // Altes Keyboard ersetzen (Telegram-Verhalten: edit_message_text)
  if (_lastKeyboardId) {
    var old = document.getElementById(_lastKeyboardId);
    if (old) old.remove();
  }
  var id = 'kb-' + Date.now();
  _lastKeyboardId = id;
  var wrap = document.createElement('div');
  wrap.id = id;
  wrap.style.cssText = 'display:flex;flex-direction:column;gap:.35rem;margin:.3rem 0 .6rem 0;touch-action:manipulation;pointer-events:auto';
  rows.forEach(function(row) {
    var rowDiv = document.createElement('div');
    rowDiv.style.cssText = 'display:flex;gap:.4rem;flex-wrap:wrap';
    row.forEach(function(btn) {
      var b = document.createElement('button');
      b.className = 'qbtn';
      b.type = 'button';
      // touch-action:manipulation → verhindert 300ms-Tap-Delay / Double-Tap-Zoom auf iOS
      // pointer-events:auto + z-index → stellt sicher dass Klicks immer ankommen
      b.style.cssText = 'flex-shrink:0;font-size:.78rem;padding:.5rem .9rem;touch-action:manipulation;pointer-events:auto;position:relative;z-index:2';
      b.textContent = btn.text;
      if (btn.url) {
        b.addEventListener('click', (function(u){ return function(ev){ ev.preventDefault(); window.open(u, '_blank'); }; })(btn.url));
      } else if (btn.data) {
        b.addEventListener('click', (function(d){ return function(ev){ ev.preventDefault(); sendMsg(d); }; })(btn.data));
      }
      rowDiv.appendChild(b);
    });
    wrap.appendChild(rowDiv);
  });
  box.appendChild(wrap);
  // Force reflow — iOS Safari rendert dynamisch eingefügte Buttons sonst
  // manchmal erst nach externem Layout-Trigger (z.B. Tab-Wechsel). Ohne das
  // sind die Buttons sichtbar aber der erste Klick wird verschluckt.
  /* eslint-disable no-unused-expressions */
  wrap.offsetHeight;
  requestAnimationFrame(function(){
    box.scrollTop = box.scrollHeight;
  });
}

// ── Clock ──────────────────────────────────────────────────────
function updateClock() {
  const el = document.getElementById('clock');
  if (el) {
    const now = new Date();
    el.textContent = now.toLocaleTimeString('de-DE', {hour:'2-digit', minute:'2-digit', second:'2-digit'});
  }
}
setInterval(updateClock, 1000);
updateClock();

// ── Tabs ──────────────────────────────────────────────────────
function switchTab(e, id) {
  document.querySelectorAll('.content').forEach(function(c) { c.classList.remove('show'); });
  document.querySelectorAll('.tab').forEach(function(t) { t.classList.remove('active'); });
  document.getElementById(id).classList.add('show');
  e.currentTarget.classList.add('active');
  if (id === 'chat') {
    const box = document.getElementById('chatbox');
    if (box) box.scrollTop = box.scrollHeight;
  }
}

// ── Toast ─────────────────────────────────────────────────────
function showToast(msg, type) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast ' + (type || 'ok') + ' show';
  setTimeout(function() { el.className = 'toast'; }, 3000);
}

// ── Password Toggle ───────────────────────────────────────────
function togglePwd(k) {
  const el = document.getElementById('pw_' + k);
  if (el) el.type = el.type === 'password' ? 'text' : 'password';
}

// ── Quick Action (Switch to Chat + send) ──────────────────────
function sendToChat(text) {
  // Tab wechseln
  document.querySelectorAll('.content').forEach(function(c) { c.classList.remove('show'); });
  document.querySelectorAll('.tab').forEach(function(t) { t.classList.remove('active'); });
  document.getElementById('chat').classList.add('show');
  document.querySelectorAll('.tab')[1].classList.add('active');
  setTimeout(function() { sendMsg(text); }, 100);
}

// ── Chat ──────────────────────────────────────────────────────
var isStreaming = false;

function escapeHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function getTime() {
  return new Date().toLocaleTimeString('de-DE', {hour:'2-digit', minute:'2-digit'});
}

function appendMsg(who, text, id) {
  var box = document.getElementById('chatbox');
  var div = document.createElement('div');
  div.className = 'msg ' + (who === 'user' ? 'user' : 'bot');
  if (id) div.id = id;
  var name = who === 'user' ? 'Du' : '__BOT_NAME__';
  div.innerHTML = '<div class="who">' + name + ' <span class="ts">' + getTime() + '</span></div>' +
                  '<div class="msg-text" id="' + (id ? id + '-text' : '') + '">' + escapeHtml(text) + '</div>';
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
  return div;
}

function appendTyping(id) {
  var box = document.getElementById('chatbox');
  var div = document.createElement('div');
  div.className = 'msg bot';
  div.id = id;
  div.innerHTML = '<div class="who">__BOT_NAME__</div>' +
                  '<div class="typing"><span></span><span></span><span></span></div>';
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

function sendMsg(cmd) {
  var input = document.getElementById('msg-input');
  var txt = cmd ? cmd.trim() : input.value.trim();
  if (!txt || isStreaming) return;

  isStreaming = true;
  var sendBtn = document.getElementById('send-btn');
  sendBtn.disabled = true;
  sendBtn.textContent = '...';
  input.value = '';

  appendMsg('user', txt, null);

  var botId = 'bot-' + Date.now();
  appendTyping(botId);

  fetch('/chat', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({msg: txt}),
    credentials: 'same-origin'
  })
  .then(function(r) {
    if (!r.ok) {
      replaceMsgContent(botId, 'HTTP Fehler ' + r.status);
      resetSend();
      return null;
    }
    return r.body.getReader();
  })
  .then(function(reader) {
    if (!reader) return;
    var decoder = new TextDecoder();
    var accumulated = '';
    var msgEl = null;

    function readLoop() {
      reader.read().then(function(result) {
        var done  = result.done;
        var value = result.value;

        if (done) {
          if (!accumulated) replaceMsgContent(botId, '(Keine Antwort)');
          resetSend();
          return;
        }

        var raw = decoder.decode(value, {stream: true});
        raw.split('\\n').forEach(function(line) {
          if (line.indexOf('data: ') === 0) {
            var payload = line.substring(6);
            try {
              var j = JSON.parse(payload);
              if (j.t) {
                accumulated += j.t;
                // Ersten Chunk: Typing-Div ersetzen
                if (!msgEl) {
                  var box = document.getElementById('chatbox');
                  var old = document.getElementById(botId);
                  if (old) old.remove();
                  msgEl = appendMsg('bot', '', botId);
                }
                document.getElementById(botId + '-text').textContent = accumulated;
                var box = document.getElementById('chatbox');
                box.scrollTop = box.scrollHeight;
              }
              // edit_text-Ersatz: letzte Bot-Nachricht überschreiben
              if (j.replace_last !== undefined) {
                accumulated = j.replace_last;
                if (!msgEl) {
                  var box2 = document.getElementById('chatbox');
                  var old2 = document.getElementById(botId);
                  if (old2) old2.remove();
                  msgEl = appendMsg('bot', '', botId);
                }
                document.getElementById(botId + '-text').textContent = accumulated;
                var box2 = document.getElementById('chatbox');
                box2.scrollTop = box2.scrollHeight;
              }
              if (j.action_pending) {
                showActionConfirm(j.action_pending);
              }
              if (j.buttons) {
                renderInlineKeyboard(j.buttons);
              }
              if (j.done) {
                resetSend();
                return;
              }
            } catch(e) {}
          }
        });
        readLoop();
      }).catch(function(e) {
        replaceMsgContent(botId, 'Stream-Fehler: ' + e.message);
        resetSend();
      });
    }
    readLoop();
  })
  .catch(function(e) {
    replaceMsgContent(botId, 'Netzwerk-Fehler: ' + e.message);
    resetSend();
  });
}

function replaceMsgContent(id, text) {
  var old = document.getElementById(id);
  if (old) old.remove();
  appendMsg('bot', text, id);
}

function resetSend() {
  isStreaming = false;
  var btn = document.getElementById('send-btn');
  btn.disabled = false;
  btn.textContent = 'SENDEN';
}

// Enter key
document.addEventListener('DOMContentLoaded', function() {
  loadDynamicCmds();
  var inp = document.getElementById('msg-input');
  if (inp) {
    inp.addEventListener('keydown', function(e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMsg();
      }
    });
  }

  // ── Push-Stream (automatische Nachrichten von Modulen) ──────────
  function connectPushStream() {
    var es = new EventSource('/push-stream');
    es.onmessage = function(e) {
      try {
        var d = JSON.parse(e.data);
        // Replay: letzte Zeilen aus chat.log beim Connect
        if (d.replay) {
          appendMsg(d.who, d.msg);
          return;
        }
        // Live-Push: Modul-Nachricht
        if (d.push) {
          appendMsg('bot', d.push);
          var box = document.getElementById('chatbox');
          if (box) box.scrollTop = box.scrollHeight;
          // Browser-Notification wenn Tab nicht fokussiert
          if (document.hidden && Notification.permission === 'granted') {
            new Notification('RICS', { body: d.push });
          }
          if (d.buttons) {
            renderInlineKeyboard(d.buttons);
          }
        }
      } catch(err) {}
    };
    es.onerror = function() {
      es.close();
      // Nach 5s neu verbinden
      setTimeout(connectPushStream, 5000);
    };
  }
  connectPushStream();

  // Browser-Notifications anfragen
  if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission();
  }
});

// ── Settings ──────────────────────────────────────────────────
async function saveSettings() {
  var form = document.getElementById('settings-form');
  var fd = new FormData(form);
  try {
    var r = await fetch('/save', {method:'POST', body: fd});
    if (r.ok) showToast('✅ Gespeichert!', 'ok');
    else showToast('❌ Fehler beim Speichern', 'err');
  } catch(e) {
    showToast('❌ ' + e.message, 'err');
  }
}

async function reloadPrompts() {
  try {
    var r = await fetch('/api/reload-prompts', {method: 'POST'});
    var d = await r.json();
    if (d.ok) {
      showToast('✅ Prompts neu geladen: ' + (d.reloaded||[]).join(', '), 'ok');
      await loadKiConfig();
    } else {
      showToast('❌ ' + (d.error || 'Fehler'), 'err');
    }
  } catch(e) { showToast('❌ ' + e.message, 'err'); }
}

// ── KI-Config ─────────────────────────────────────────────────
var _actionsData = [];

async function loadKiConfig() {
  try {
    var r = await fetch('/api/ki-config');
    var d = await r.json();
    // System prompt
    var ta = document.getElementById('ki-sysprompt');
    if (ta) ta.value = d.system_prompt || '';
    // Agent prompt
    var ta2 = document.getElementById('ki-agentprompt');
    if (ta2) ta2.value = d.agent_prompt || '';
    // Actions
    _actionsData = (d.actions || []).map(function(a) {
      return a;
    });
    renderActions();
  } catch(e) {
    showToast('❌ Laden fehlgeschlagen: ' + e.message, 'err');
  }
}

function renderActions() {
  var grid = document.getElementById('actions-grid');
  if (!grid) return;
  grid.innerHTML = '';
  _actionsData.forEach(function(a, i) {
    grid.innerHTML += '<div class="action-card" id="ac-' + i + '">' +
      '<button class="del-btn" onclick="removeAction(' + i + ')">✕</button>' +
      '<div><label>Action-Name</label><input class="inp" id="ac-action-' + i + '" value="' + esc(a.action||'') + '" placeholder="z.B. WETTER"></div>' +
      '<div><label>Command (Handler)</label><input class="inp" id="ac-command-' + i + '" value="' + esc(a.command||'') + '" placeholder="z.B. wetter"></div>' +
      '<div><label>Label (im Prompt)</label><input class="inp" id="ac-label-' + i + '" value="' + esc(a.label||'') + '" placeholder="z.B. Wetter-Suche"></div>' +
      '<div><label>Param-Key</label><input class="inp" id="ac-param-' + i + '" value="' + esc(a.param||'QUERY') + '" placeholder="z.B. QUERY"></div>' +
      '<div><label>Beschreibung (nur für dich)</label><input class="inp" id="ac-desc-' + i + '" value="' + esc(a.description||'') + '" placeholder="Kurzbeschreibung"></div>' +
      '<div style="grid-column:1/-1"><label>Prompt-Beispiel</label><textarea class="ki-textarea" id="ac-prompt-' + i + '" style="min-height:80px;font-size:.8rem">' + esc(a.prompt_example||'') + '</textarea></div>' +
      '</div>';
  });
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function addActionCard() {
  _actionsData.push({action:'',command:'',param:'QUERY',description:'',prompt_example:''});
  renderActions();
  // scroll to new card
  var grid = document.getElementById('actions-grid');
  if (grid) grid.lastElementChild && grid.lastElementChild.scrollIntoView({behavior:'smooth'});
}

function removeAction(i) {
  _actionsData.splice(i, 1);
  renderActions();
}

function collectActions() {
  var arr = [];
  _actionsData.forEach(function(_, i) {
    arr.push({
      action:         document.getElementById('ac-action-' + i)  ? document.getElementById('ac-action-' + i).value.trim()  : '',
      command:        document.getElementById('ac-command-' + i) ? document.getElementById('ac-command-' + i).value.trim() : '',
      label:          document.getElementById('ac-label-' + i)   ? document.getElementById('ac-label-' + i).value.trim()   : '',
      param:          document.getElementById('ac-param-' + i)   ? document.getElementById('ac-param-' + i).value.trim()   : 'QUERY',
      description:    document.getElementById('ac-desc-' + i)    ? document.getElementById('ac-desc-' + i).value.trim()    : '',
      prompt_example: document.getElementById('ac-prompt-' + i)  ? document.getElementById('ac-prompt-' + i).value.trim()  : ''
    });
  });
  return arr;
}

async function saveSysPrompt() {
  var ta = document.getElementById('ki-sysprompt');
  if (!ta) return;
  try {
    var r = await fetch('/api/save-system-prompt', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({content: ta.value})
    });
    if (r.ok) showToast('✅ System-Prompt gespeichert!', 'ok');
    else showToast('❌ Fehler beim Speichern', 'err');
  } catch(e) { showToast('❌ ' + e.message, 'err'); }
}

async function saveAgentPrompt() {
  var ta = document.getElementById('ki-agentprompt');
  if (!ta) return;
  try {
    var r = await fetch('/api/save-agent-prompt', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({content: ta.value})
    });
    if (r.ok) showToast('✅ Agent-Prompt gespeichert!', 'ok');
    else showToast('❌ Fehler beim Speichern', 'err');
  } catch(e) { showToast('❌ ' + e.message, 'err'); }
}

async function saveActions() {
  var actions = collectActions();
  try {
    var r = await fetch('/api/save-actions', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({actions: actions})
    });
    var d = await r.json();
    if (r.ok && d.ok) {
      _actionsData = actions;
      var msg = '✅ Actions gespeichert!';
      if (d.prompt_updated) msg += ' System-Prompt aktualisiert.';
      else msg += ' ⚠️ System-Prompt nicht aktualisiert.';
      showToast(msg, 'ok');
    } else {
      showToast('❌ Fehler: ' + (d.error || 'Unbekannt'), 'err');
    }
  } catch(e) { showToast('❌ ' + e.message, 'err'); }
}
</script>
</body>
</html>"""

    html = html.replace("__BOT_NAME__", bot_name)
    html = html.replace("__STATUS__", status)
    html = html.replace("__STATUS_CLASS__", "ok" if status == "ONLINE" else "init")
    html = html.replace("__PROVIDER__", provider.upper())
    html = html.replace("__MODEL__", model)
    html = html.replace("__MEMORY_OK__", memory_ok)
    html = html.replace("__BRAIN_OK__", brain_ok)
    html = html.replace("__SETTINGS_HTML__", settings_html)
    return html


# ════════════════════════════════════════════════════════════════
# ROUTES
# ════════════════════════════════════════════════════════════════

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        load_dotenv(ENV_PATH, override=True)
        if request.form.get("pin") == os.getenv("WEB_PIN","7312"):
            session["auth"] = True
            return redirect("/")
        error = '<p style="color:#ff6b6b;margin-top:.5rem;font-size:.85rem">Falscher PIN</p>'
    else:
        error = ""

    _bot = os.getenv("BOT_NAME", "RICS")
    return ("""<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">
<title>__BOT_NAME__ Login</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:linear-gradient(135deg,#020617,#0a1628,#0f1f3d);display:flex;justify-content:center;align-items:center;min-height:100vh;font-family:'Courier New',monospace}
.box{background:rgba(15,23,42,.95);border:1.5px solid rgba(0,212,255,.4);padding:3rem 2.5rem;border-radius:20px;text-align:center;min-width:320px;box-shadow:0 0 80px rgba(0,212,255,.2)}
.logo{font-size:3rem;font-weight:900;color:#00d4ff;letter-spacing:4px;margin-bottom:.3rem}
.sub{font-size:.7rem;color:#64748b;letter-spacing:2px;margin-bottom:2rem}
input{width:100%;padding:1rem;margin-bottom:.8rem;background:rgba(2,6,23,.8);border:1.5px solid rgba(0,212,255,.3);color:#00d4ff;border-radius:10px;text-align:center;font-size:1.8rem;letter-spacing:4px;font-family:inherit}
input:focus{outline:none;border-color:#00d4ff}
button{width:100%;padding:1rem;background:linear-gradient(135deg,#00d4ff,#7c3aed);color:#000;border:none;border-radius:10px;font-weight:900;cursor:pointer;font-size:1rem;letter-spacing:2px;margin-top:.5rem}
button:hover{opacity:.88}
</style></head><body>
<div class="box">
  <div class="logo">__BOT_NAME__</div>
  <div class="sub">AI ASSISTANT DASHBOARD</div>
  <form method="POST">
    <input type="password" name="pin" placeholder="PIN" autofocus autocomplete="off">
    <button type="submit">EINLOGGEN</button>
  </form>""" + error + """
</div></body></html>""").replace("__BOT_NAME__", _bot)


@app.route("/")
def index():
    if not session.get("auth"):
        return redirect("/login")
    return build_index_html()


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


def _write_env_key(env_path, key, value):
    """Schreibt KEY='value' in die .env — immer mit einfachen Quotes."""
    raw = value.strip()
    # Vorhandene einfache Quotes entfernen falls dotenv sie nicht stripped hat
    if raw.startswith("'") and raw.endswith("'"):
        raw = raw[1:-1]
    new_line = f"{key}='{raw}'\n"
    lines = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    found = False
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith(f"{key}=") or s.startswith(f"{key} ="):
            lines[i] = new_line
            found = True
            break
    if not found:
        lines.append(new_line)
    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


@app.route("/save", methods=["POST"])
def save():
    if not session.get("auth"):
        return "Unauthorized", 401
    if not os.path.exists(ENV_PATH):
        open(ENV_PATH, "a").close()
    for k, v in request.form.items():
        try:
            _write_env_key(ENV_PATH, k, v)
        except Exception as e:
            logger.error(f"Error saving {k}: {e}")
    load_dotenv(ENV_PATH, override=True)
    return jsonify({"ok": True})


# ── KI-Config Paths ───────────────────────────────────────────────────────
_CORE_DIR         = os.path.join(BASE_DIR, "core")
_SYSPROMPT_PATH   = os.path.join(_CORE_DIR, "system_prompt.txt")
_ACTIONS_PATH     = os.path.join(_CORE_DIR, "custom_actions.json")
_AGENTPROMPT_PATH = os.path.join(_CORE_DIR, "agent_prompt.txt")

@app.route("/api/ki-config")
def api_ki_config():
    if not session.get("auth"):
        return "Unauthorized", 401
    sys_prompt = ""
    if os.path.exists(_SYSPROMPT_PATH):
        with open(_SYSPROMPT_PATH, "r", encoding="utf-8") as f:
            sys_prompt = f.read()
    agent_prompt = ""
    if os.path.exists(_AGENTPROMPT_PATH):
        with open(_AGENTPROMPT_PATH, "r", encoding="utf-8") as f:
            agent_prompt = f.read()
    actions = []
    if os.path.exists(_ACTIONS_PATH):
        try:
            with open(_ACTIONS_PATH, "r", encoding="utf-8") as f:
                actions = json.load(f)
        except Exception:
            actions = []
    return jsonify({"system_prompt": sys_prompt, "agent_prompt": agent_prompt, "actions": actions})

@app.route("/api/save-system-prompt", methods=["POST"])
def api_save_system_prompt():
    if not session.get("auth"):
        return "Unauthorized", 401
    data = request.get_json(silent=True) or {}
    content = data.get("content", "")
    try:
        with open(_SYSPROMPT_PATH, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info("system_prompt.txt updated via Web UI")
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"save system_prompt error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/save-agent-prompt", methods=["POST"])
def api_save_agent_prompt():
    if not session.get("auth"):
        return "Unauthorized", 401
    data = request.get_json(silent=True) or {}
    content = data.get("content", "")
    try:
        with open(_AGENTPROMPT_PATH, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info("agent_prompt.txt updated via Web UI")
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"save agent_prompt error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/reload-prompts", methods=["POST"])
def api_reload_prompts():
    if not session.get("auth"):
        return "Unauthorized", 401
    if not jarvis_instance:
        return jsonify({"ok": False, "error": "Bot noch nicht initialisiert"})
    try:
        reloaded = []
        if os.path.exists(_SYSPROMPT_PATH):
            with open(_SYSPROMPT_PATH, "r", encoding="utf-8") as f:
                jarvis_instance.system_prompt = f.read()
            reloaded.append("system_prompt")
        if os.path.exists(_AGENTPROMPT_PATH):
            with open(_AGENTPROMPT_PATH, "r", encoding="utf-8") as f:
                jarvis_instance.agent_prompt = f.read()
            reloaded.append("agent_prompt")
        logger.info(f"Prompts neu geladen: {reloaded}")
        return jsonify({"ok": True, "reloaded": reloaded})
    except Exception as e:
        logger.error(f"reload prompts error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

def _rebuild_action_format_block(actions: list) -> str:
    """Generiert den ### 📦 ACTION FORMAT Block aus der Actions-Liste."""
    lines = []
    lines.append("----------------------------------------")
    lines.append("### 📦 ACTION FORMAT (NUR BEI BEFEHL!)")
    lines.append("----------------------------------------")
    for a in actions:
        action_name = a.get("action", "").strip().upper()
        param_key   = a.get("param", "QUERY").strip().upper()
        label       = a.get("label", "").strip()        # kurzes Label-Feld
        description = a.get("description", "").strip()  # nur Fallback
        example     = a.get("prompt_example", "").strip()

        # Literal \n (zwei Zeichen, z.B. aus altem Input-Feld) → echter Zeilenumbruch
        example = example.replace("\\n", "\n")

        # Label: label-Feld bevorzugt, dann description, dann Action-Name
        display_label = label or description or action_name

        # Fallback wenn kein echter Zeilenumbruch im Beispiel
        if not example or "\n" not in example:
            example = f"ACTION: {action_name}\n{param_key}: Suchbegriff"

        lines.append(f"{display_label.upper()}:")
        for ex_line in example.splitlines():
            lines.append(ex_line)
        lines.append("")  # Leerzeile zwischen Actions

    lines.append("KEIN zusätzlicher Text bei ACTION!")
    lines.append("")
    return "\n".join(lines)


def _append_new_actions_to_sysprompt(old_actions: list, new_actions: list):
    """Hängt nur wirklich neue Actions an den ACTION FORMAT Block an."""
    if not os.path.exists(_SYSPROMPT_PATH):
        return

    # Welche Action-Namen sind neu?
    old_names = {a.get("action", "").strip().upper() for a in old_actions}
    added = [a for a in new_actions if a.get("action", "").strip().upper() not in old_names]
    if not added:
        return  # nichts Neues → nichts tun

    with open(_SYSPROMPT_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    END_MARKER = "KEIN zusätzlicher Text bei ACTION!"
    end_idx = content.find(END_MARKER)
    if end_idx == -1:
        logger.warning("ACTION FORMAT Block nicht gefunden — überspringe Append")
        return

    # Neue Einträge bauen
    new_lines = []
    for a in added:
        action_name = a.get("action", "").strip().upper()
        param_key   = a.get("param", "QUERY").strip().upper()
        label       = a.get("label", "").strip() or a.get("description", "").strip() or action_name
        example     = a.get("prompt_example", "").strip().replace("\\n", "\n")
        if not example or "\n" not in example:
            example = f"ACTION: {action_name}\n{param_key}: Suchbegriff"
        new_lines.append(f"{label.upper()}:")
        new_lines.extend(example.splitlines())
        new_lines.append("")  # Leerzeile

    insert_text = "\n".join(new_lines) + "\n"
    content = content[:end_idx] + insert_text + content[end_idx:]

    with open(_SYSPROMPT_PATH, "w", encoding="utf-8") as f:
        f.write(content)
    logger.info(f"system_prompt.txt: {len(added)} neue Action(s) angehängt ({[a.get('action') for a in added]})")


@app.route("/api/save-actions", methods=["POST"])
def api_save_actions():
    if not session.get("auth"):
        return "Unauthorized", 401
    data = request.get_json(silent=True) or {}
    actions = data.get("actions", [])
    try:
        # 1. Alte Actions lesen (für Vergleich)
        old_actions = []
        if os.path.exists(_ACTIONS_PATH):
            try:
                with open(_ACTIONS_PATH, "r", encoding="utf-8") as f:
                    old_actions = json.load(f)
            except Exception:
                old_actions = []

        # 2. custom_actions.json speichern
        with open(_ACTIONS_PATH, "w", encoding="utf-8") as f:
            json.dump(actions, f, ensure_ascii=False, indent=2)
        logger.info(f"custom_actions.json updated via Web UI ({len(actions)} actions)")

        # 3. Nur neue Actions an system_prompt.txt anhängen
        try:
            _append_new_actions_to_sysprompt(old_actions, actions)
            prompt_updated = True
        except Exception as pe:
            logger.error(f"system_prompt append error: {pe}")
            prompt_updated = False

        return jsonify({"ok": True, "prompt_updated": prompt_updated})
    except Exception as e:
        logger.error(f"save actions error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/push-stream")
def push_stream():
    """SSE-Endpoint — Browser hält diese Verbindung offen und empfängt Push-Nachrichten."""
    if not session.get("auth"):
        return "Unauthorized", 401

    q = _queue.Queue(maxsize=50)
    with _push_lock:
        _push_clients.append(q)

    def generate():
        # Letzte 5 Zeilen aus täglichem chat.log nachliefern (USER + BOT + PUSH gemischt)
        try:
            log_dir = os.path.join(BASE_DIR, "logs")
            today = datetime.now().strftime("%Y-%m-%d")
            log_path = os.path.join(log_dir, f"{today}.log")
            if os.path.exists(log_path):
                with open(log_path, "r", encoding="utf-8") as f:
                    lines = [l.rstrip("\n") for l in f.readlines() if l.strip()]
                for line in lines[-5:]:
                    if line.startswith("[WEB] USER:"):
                        msg = line[len("[WEB] USER:"):].strip()
                        yield "data: " + json.dumps({"replay": True, "who": "user", "msg": msg}) + "\n\n"
                    elif line.startswith("[WEB] BOT:"):
                        msg = line[len("[WEB] BOT:"):].strip()
                        yield "data: " + json.dumps({"replay": True, "who": "bot", "msg": msg}) + "\n\n"
                    elif line.startswith("[PUSH] RICS:"):
                        msg = line[len("[PUSH] RICS:"):].strip()
                        yield "data: " + json.dumps({"replay": True, "who": "bot", "msg": "📡 " + msg}) + "\n\n"
                    else:
                        # andere Zeilen (z.B. USER: / BOT: ohne [WEB])
                        if "USER:" in line:
                            msg = line.split("USER:", 1)[-1].strip()
                            yield "data: " + json.dumps({"replay": True, "who": "user", "msg": msg}) + "\n\n"
                        elif "BOT:" in line:
                            msg = line.split("BOT:", 1)[-1].strip()
                            yield "data: " + json.dumps({"replay": True, "who": "bot", "msg": msg}) + "\n\n"
        except Exception:
            pass
        # Heartbeat damit die Verbindung nicht abbricht
        yield "data: " + json.dumps({"ping": True}) + "\n\n"
        try:
            while True:
                try:
                    msg = q.get(timeout=25)
                    yield msg
                except _queue.Empty:
                    # Heartbeat alle 25s
                    yield "data: " + json.dumps({"ping": True}) + "\n\n"
        except GeneratorExit:
            pass
        finally:
            with _push_lock:
                if q in _push_clients:
                    _push_clients.remove(q)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})




_DS_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs", "ds_balance_history.json")

def _ds_save_snapshot(total: float, available: float, currency: str):
    """Speichert Guthaben-Snapshot für Chart-History (max 30 Einträge)."""
    try:
        os.makedirs(os.path.dirname(_DS_HISTORY_FILE), exist_ok=True)
        history = []
        if os.path.exists(_DS_HISTORY_FILE):
            with open(_DS_HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
        ts = datetime.now().strftime("%d.%m %H:%M")
        history.append({"ts": ts, "total": total, "available": available, "currency": currency})
        history = history[-30:]  # max 30 Datenpunkte
        with open(_DS_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False)
    except Exception:
        pass

def _ds_load_history() -> list:
    try:
        if os.path.exists(_DS_HISTORY_FILE):
            with open(_DS_HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []


@app.route("/api/deepseek-balance")
def deepseek_balance():
    """Holt Guthaben & Verfügbarkeit vom DeepSeek API."""
    if not session.get("auth"):
        return jsonify({"error": "Unauthorized"}), 401
    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    if not api_key:
        return jsonify({"error": "DEEPSEEK_API_KEY nicht in .env gesetzt"})
    try:
        r = httpx.get(
            "https://api.deepseek.com/user/balance",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=8,
        )
        if r.status_code != 200:
            return jsonify({"error": f"API HTTP {r.status_code}: {r.text[:120]}"})
        data    = r.json()
        bal_list = data.get("balance_infos", [])
        bal      = bal_list[0] if bal_list else {}

        total_str   = bal.get("total_balance",   "0")
        avail_str   = bal.get("topped_up_balance", bal.get("total_balance", "0"))
        granted_str = bal.get("granted_balance", "0")
        currency    = bal.get("currency", "USD")

        try:
            total_f   = float(total_str)
            avail_f   = float(avail_str)
            granted_f = float(granted_str)
        except (ValueError, TypeError):
            total_f = avail_f = granted_f = 0.0

        # Snapshot für Chart speichern
        _ds_save_snapshot(total_f, avail_f, currency)
        history = _ds_load_history()

        return jsonify({
            "is_available":    data.get("is_available", False),
            "currency":        currency,
            "total_balance":   total_str,
            "available":       avail_str,
            "granted_balance": granted_str,
            "total_f":         total_f,
            "available_f":     avail_f,
            "granted_f":       granted_f,
            "history":         history,
        })
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/commands")
def api_commands():
    """Gibt alle registrierten Telegram-Command-Namen zurück (für Quick-Buttons)."""
    if not session.get("auth"):
        return jsonify({"error": "unauthorized"}), 401
    cmds = []
    if _telegram_app:
        for group in _telegram_app.handlers.values():
            for h in group:
                if hasattr(h, "commands"):
                    for c in h.commands:
                        cmds.append(c)
    return jsonify({"commands": sorted(set(cmds))})


def _detect_action_web(answer: str) -> dict | None:
    """
    Wie bot.py _detect_action — aber ohne Telegram-Context.
    Gibt ein Action-Dict zurück oder None.
    """
    import re as _re

    # ── TERMIN_ADD ────────────────────────────────────────────
    if (_re.search(r'(?m)^\s*ACTION:\s*TERMIN_ADD\s*$', answer) and
            _re.search(r'(?m)^\s*DATE:\s*.+', answer) and
            _re.search(r'(?m)^\s*TEXT:\s*.+', answer)):
        d   = _re.search(r"DATE:\s*(.*)", answer, _re.I).group(1).split('\n')[0].strip()
        tm  = _re.search(r"TIME:\s*(.*)", answer, _re.I)
        t   = tm.group(1).split('\n')[0].strip() if tm else "00:00"
        txt = _re.search(r"TEXT:\s*(.*)", answer, _re.I).group(1).split('\n')[0].strip()
        return {
            "type":    "TERMIN_ADD",
            "preview": f"📅 Termin eintragen\n📌 {txt}\n🕐 {d} {t}",
            "answer":  answer,
        }

    # ── DISCORD_MESSAGE ───────────────────────────────────────
    if (_re.search(r'(?m)^\s*ACTION:\s*DISCORD_MESSAGE\s*$', answer) and
            _re.search(r'(?m)^\s*CHANNEL:\s*.+', answer) and
            _re.search(r'(?m)^\s*MESSAGE:\s*.+', answer)):
        ch_m  = _re.search(r"CHANNEL:\s*(#?\S+)", answer, _re.I)
        msg_m = _re.search(r"MESSAGE:\s*(.+)",    answer, _re.I)
        tgt_m = _re.search(r"TARGET:\s*(.+)",     answer, _re.I)
        ch    = ch_m.group(1).strip().lstrip("#") if ch_m else "?"
        txt   = msg_m.group(1).strip() if msg_m else "?"
        tgt   = tgt_m.group(1).strip() if tgt_m else None
        display = f"@{tgt} {txt}" if tgt else txt
        return {
            "type":    "DISCORD_MESSAGE",
            "preview": f"💬 Discord-Nachricht senden\n📢 #{ch}\n✉️ {display}",
            "answer":  answer,
        }

    # ── CUSTOM ACTIONS ────────────────────────────────────────
    import re as _re2
    try:
        ca_path = os.path.join(BASE_DIR, "core", "custom_actions.json")
        if not os.path.exists(ca_path):
            ca_path = os.path.join(BASE_DIR, "custom_actions.json")
        if os.path.exists(ca_path):
            with open(ca_path, "r", encoding="utf-8") as _f:
                custom_actions = json.load(_f)
            for ca in custom_actions:
                action_name = ca.get("action", "").strip().upper()
                command     = ca.get("command", "").strip().lstrip("/").lower()
                param_key   = ca.get("param", "QUERY").strip().upper()
                if not action_name or not command:
                    continue
                if not _re.search(rf'(?m)^\s*ACTION:\s*{_re.escape(action_name)}\s*$', answer, _re.I):
                    continue
                pm = _re.search(rf"{param_key}:\s*(.+)", answer, _re.I)
                pv = pm.group(1).strip() if pm else ""
                desc = ca.get("description", action_name)
                exec_cmd = f"/{command} {pv}".strip()
                return {
                    "type":     "CUSTOM",
                    "preview":  f"⚙️ {desc}\n🔧 {exec_cmd}",
                    "exec_cmd": exec_cmd,
                }
    except Exception:
        pass

    return None


@app.route("/api/action-exec", methods=["POST"])
def api_action_exec():
    """Führt TERMIN_ADD oder DISCORD_MESSAGE aus (ohne Telegram-Context)."""
    if not session.get("auth"):
        return jsonify({"error": "unauthorized"}), 401
    data        = request.get_json(force=True, silent=True) or {}
    action_type = data.get("type", "")
    answer      = data.get("answer", "")
    import re as _re

    if action_type == "TERMIN_ADD":
        try:
            sys.path.insert(0, BASE_DIR)
            import modules.agenda as agenda_mod
            d       = _re.search(r"DATE:\s*(.*)", answer, _re.I).group(1).split('\n')[0].strip()
            tm      = _re.search(r"TIME:\s*(.*)", answer, _re.I)
            t       = tm.group(1).split('\n')[0].strip() if tm else "00:00"
            txt     = _re.search(r"TEXT:\s*(.*)", answer, _re.I).group(1).split('\n')[0].strip()
            task_date = agenda_mod.parse_date(d, t, brain_instance)
            ag = agenda_mod.load_agenda()
            ag.append({"date": task_date.isoformat(), "task": txt, "reminded": False})
            agenda_mod.save_agenda(ag)
            return jsonify({"result": f"✅ Termin eingetragen: {txt} ({task_date.strftime('%d.%m. %H:%M')})"})
        except Exception as e:
            return jsonify({"result": f"❌ Termin-Fehler: {e}"})

    elif action_type == "DISCORD_MESSAGE":
        try:
            ch_m  = _re.search(r"CHANNEL:\s*(#?\S+)", answer, _re.I)
            msg_m = _re.search(r"MESSAGE:\s*(.+)",    answer, _re.I)
            tgt_m = _re.search(r"TARGET:\s*(.+)",     answer, _re.I)
            if not ch_m or not msg_m:
                return jsonify({"result": "❌ Kanal oder Nachricht fehlt"})
            ch_name = ch_m.group(1).strip().lstrip("#")
            msg_txt = msg_m.group(1).strip()
            if tgt_m:
                msg_txt = f"@{tgt_m.group(1).strip()} {msg_txt}"
            from modules.discord_manager import get_discord_bot
            dbot = get_discord_bot()
            if not dbot or not dbot.ready:
                return jsonify({"result": "❌ Discord Bot nicht bereit"})
            guild = dbot.get_guild()
            channel = dbot.find_channel(guild, ch_name) if guild else None
            if not channel:
                return jsonify({"result": f"❌ Kanal #{ch_name} nicht gefunden"})
            async def _send():
                await channel.send(msg_txt)
            dbot.run_coro(_send())
            return jsonify({"result": f"✅ Discord Nachricht gesendet → #{ch_name}"})
        except Exception as e:
            return jsonify({"result": f"❌ Discord Fehler: {e}"})

    return jsonify({"result": "❌ Unbekannter Action-Typ"})


@app.route("/chat", methods=["POST"])
def chat():
    """
    Chat-Endpoint — exakt gleiche Logik wie bot.py:
    personal, brain, memory, chat_history, learn_from_message
    """
    if not session.get("auth"):
        return "Unauthorized", 401

    if not jarvis_instance:
        def _wait():
            yield "data: " + json.dumps({"t": "Bot wird initialisiert, bitte kurz warten..."}) + "\n\n"
        return Response(_wait(), mimetype="text/event-stream")

    data = request.get_json(force=True, silent=True) or {}
    msg = data.get("msg", "").strip()
    if not msg:
        def _empty():
            yield "data: " + json.dumps({"t": "Leere Nachricht"}) + "\n\n"
        return Response(_empty(), mimetype="text/event-stream")

    # ── Command Router (/wetter, /ichnbin, /merke etc.) ───────────
    # Erkennt Befehle und ruft den echten Telegram-Handler auf —
    # via FakeUpdate, identisch zu job.py SimpleUpdate.
    if msg.startswith("/"):
        parts = msg.split()
        cmd   = parts[0][1:].lower()
        args  = parts[1:]

        tg_app = _telegram_app  # global, gesetzt in setup()

        # Handler suchen
        handler_cb = None
        if tg_app:
            for group in tg_app.handlers.values():
                for h in group:
                    if hasattr(h, "commands") and cmd in h.commands:
                        handler_cb = h.callback
                        break
                if handler_cb:
                    break

        # Eingebaute Web-Commands (kein Telegram-Handler nötig)
        builtin = _handle_builtin_command(cmd, args)
        if builtin is not None:
            def _builtin_stream(_b=builtin):
                yield "data: " + json.dumps({"t": _b}) + "\n\n"
                yield "data: " + json.dumps({"done": True}) + "\n\n"
            return Response(_builtin_stream(), mimetype="text/event-stream",
                            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

        if handler_cb:
            def _cmd_stream(_cb=handler_cb, _app=tg_app, _args=args, _cmd=cmd):
                import queue as _cmd_q
                result_q   = _cmd_q.Queue()
                keyboard_rows = []

                def _extract_keyboard(kw):
                    markup = kw.get("reply_markup")
                    if markup and hasattr(markup, "inline_keyboard"):
                        for row in markup.inline_keyboard:
                            keyboard_rows.append([{
                                "text": btn.text,
                                "data": getattr(btn, "callback_data", None),
                                "url":  getattr(btn, "url", None),
                            } for btn in row])

                class FakeMessageSent:
                    """Returned by reply_text — edit_text ersetzt letzte Nachricht live."""
                    async def edit_text(self, text, **kw):
                        _extract_keyboard(kw)
                        result_q.put(("replace", str(text)))
                    async def delete(self): pass

                class FakeMessage:
                    text = msg
                    message_id = 0
                    async def reply_text(self, text, **kw):
                        _extract_keyboard(kw)
                        result_q.put(("append", str(text)))
                        return FakeMessageSent()
                    async def reply_chat_action(self, action, **kw):
                        pass
                    async def reply_photo(self, photo, caption=None, **kw):
                        if caption: result_q.put(("append", str(caption)))
                    async def reply_voice(self, voice, **kw):
                        pass

                class FakeChat:
                    id = 0

                class FakeUpdate:
                    message        = FakeMessage()
                    effective_chat = FakeChat()

                class FakeContext:
                    args        = _args
                    application = _app
                    bot         = _app.bot
                    bot_data    = _app.bot_data
                    user_data   = _web_user_data
                    chat_data   = _web_chat_data

                def _run():
                    _loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(_loop)
                    _in_web_handler.active = True
                    try:
                        _loop.run_until_complete(_cb(FakeUpdate(), FakeContext()))
                    except Exception as e:
                        result_q.put(("append", f"❌ Fehler bei /{_cmd}: {e}"))
                    finally:
                        _in_web_handler.active = False
                        _loop.close()
                        result_q.put(("done", None))

                threading.Thread(target=_run, daemon=True).start()

                has_output = False
                while True:
                    try:
                        kind, text = result_q.get(timeout=0.4)
                    except _cmd_q.Empty:
                        # Keepalive — hält SSE-Verbindung offen während langer Ops
                        yield "data: " + json.dumps({"ping": True}) + "\n\n"
                        continue

                    if kind == "done":
                        if keyboard_rows:
                            yield "data: " + json.dumps({"buttons": keyboard_rows}) + "\n\n"
                        if not has_output:
                            yield "data: " + json.dumps({"t": f"✅ /{_cmd} ausgeführt."}) + "\n\n"
                        yield "data: " + json.dumps({"done": True}) + "\n\n"
                        break
                    elif kind == "append":
                        has_output = True
                        yield "data: " + json.dumps({"t": text}) + "\n\n"
                    elif kind == "replace":
                        has_output = True
                        yield "data: " + json.dumps({"replace_last": text}) + "\n\n"

            return Response(_cmd_stream(), mimetype="text/event-stream",
                            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        # kein Handler gefunden → LLM übernimmt (fällt durch zu stream_response)

    # ── CallbackQuery Router (Inline-Keyboard Buttons) ─────────────
    # Callback-Daten aus Inline-Buttons (z.B. "help_cat:…") werden an
    # den passenden CallbackQueryHandler weitergeleitet — kein LLM nötig.
    if not msg.startswith("/"):
        import re as _re_cq
        from telegram.ext import CallbackQueryHandler as _CQH
        cq_handler_cb = None
        tg_app_cq = _telegram_app
        if tg_app_cq:
            for group in tg_app_cq.handlers.values():
                for h in group:
                    if not isinstance(h, _CQH):
                        continue
                    pat = getattr(h, "pattern", None)
                    matched = False
                    if pat is None:
                        matched = True
                    elif callable(pat) and not hasattr(pat, "match"):
                        try: matched = bool(pat(msg))
                        except Exception: pass
                    else:
                        try: matched = bool(_re_cq.search(pat if isinstance(pat, str) else pat.pattern, msg))
                        except Exception: pass
                    if matched:
                        cq_handler_cb = h.callback
                        break
                if cq_handler_cb:
                    break

        if cq_handler_cb:
            def _cq_stream(_cb=cq_handler_cb, _app=tg_app_cq, _data=msg):
                collected    = []
                keyboard_rows = []

                class FakeMessage:
                    message_id = 0
                    text       = ""
                    chat_id    = 0
                    async def reply_text(self, text, **kw):
                        collected.append(str(text))
                        markup = kw.get("reply_markup")
                        if markup and hasattr(markup, "inline_keyboard"):
                            keyboard_rows.clear()
                            for row in markup.inline_keyboard:
                                keyboard_rows.append([{"text": btn.text, "data": getattr(btn, "callback_data", None), "url": getattr(btn, "url", None)} for btn in row])

                class FakeCallbackQuery:
                    data    = _data
                    message = FakeMessage()
                    async def answer(self, *a, **kw):
                        pass
                    async def edit_message_text(self, text, **kw):
                        collected.clear()
                        collected.append(str(text))
                        markup = kw.get("reply_markup")
                        if markup and hasattr(markup, "inline_keyboard"):
                            keyboard_rows.clear()
                            for row in markup.inline_keyboard:
                                keyboard_rows.append([{"text": btn.text, "data": getattr(btn, "callback_data", None), "url": getattr(btn, "url", None)} for btn in row])
                    async def edit_message_reply_markup(self, reply_markup=None, **kw):
                        if reply_markup and hasattr(reply_markup, "inline_keyboard"):
                            keyboard_rows.clear()
                            for row in reply_markup.inline_keyboard:
                                keyboard_rows.append([{"text": btn.text, "data": getattr(btn, "callback_data", None), "url": getattr(btn, "url", None)} for btn in row])

                class FakeChat:
                    id = 0

                class FakeUpdate:
                    callback_query = FakeCallbackQuery()
                    effective_chat = FakeChat()
                    message        = None

                class FakeContext:
                    args        = []
                    application = _app
                    bot         = _app.bot
                    bot_data    = _app.bot_data     # real: enthält jarvis, brain, etc.
                    user_data   = _web_user_data    # modul-global (persistiert)
                    chat_data   = _web_chat_data    # modul-global (persistiert)

                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                _in_web_handler.active = True
                try:
                    loop.run_until_complete(_cb(FakeUpdate(), FakeContext()))
                except Exception as e:
                    collected.append(f"❌ Fehler: {e}")
                finally:
                    _in_web_handler.active = False
                    loop.close()

                result = "\n".join(collected) if collected else ""
                if result:
                    yield "data: " + json.dumps({"t": result}) + "\n\n"
                if keyboard_rows:
                    yield "data: " + json.dumps({"buttons": keyboard_rows}) + "\n\n"
                if not result and not keyboard_rows:
                    yield "data: " + json.dumps({"t": "✅ Ausgeführt."}) + "\n\n"
                yield "data: " + json.dumps({"done": True}) + "\n\n"

            return Response(_cq_stream(), mimetype="text/event-stream",
                            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        # kein CQ-Handler → LLM

    def stream_response():
        try:
            # ── 1. Persönliche Daten (exakt wie bot.py) ────────────
            try:
                personal_text = jarvis_instance.personal.as_text()
            except Exception:
                personal_text = getattr(jarvis_instance, "personal_data", "")

            # ── 2. Brain ───────────────────────────────────────────
            brain_data = ""
            if brain_instance:
                try:
                    brain_data = brain_instance.get_historical(msg)
                except Exception as e:
                    brain_data = f"Brain-Fehler: {e}"

            # ── 3. Vector Memory ───────────────────────────────────
            past_context = ""
            if hasattr(jarvis_instance, "memory") and jarvis_instance.memory:
                try:
                    past_context = jarvis_instance.memory.search_user(msg)
                except Exception:
                    past_context = ""

            # ── 4. Brain Folder (wie bot.py) ───────────────────────
            brain_file_section = ""
            if hasattr(jarvis_instance, "detect_brain_request"):
                try:
                    brain_request = jarvis_instance.detect_brain_request(msg)
                    if brain_request == "__LIST__":
                        brain_file_section = "\n### VERFÜGBARE BRAIN-DATEIEN:\n" + jarvis_instance.list_brain_files()
                    elif brain_request:
                        fc = jarvis_instance.load_brain_file(brain_request)
                        brain_file_section = f"\n{fc}" if fc else f"\n### HINWEIS: '{brain_request}' nicht gefunden."
                except Exception:
                    pass

            # ── 5. System-Message zusammenbauen (exakt wie bot.py) ─
            brain_section  = f"\n### BRAIN:\n{brain_data}"        if brain_data and brain_data != "KEINE DATEN" else ""
            memory_section = f"\n### GEDÄCHTNIS:\n{past_context}" if past_context else ""

            _WDAYS = ["Montag","Dienstag","Mittwoch","Donnerstag","Freitag","Samstag","Sonntag"]
            now_str = ""
            if brain_instance:
                try:
                    _t = brain_instance.get_now()
                    now_str = f"{_WDAYS[_t.weekday()]}, {_t.strftime('%d.%m.%Y %H:%M')}"
                except Exception:
                    pass
            if not now_str:
                _t = datetime.now()
                now_str = f"{_WDAYS[_t.weekday()]}, {_t.strftime('%d.%m.%Y %H:%M')}"

            system_prompt = getattr(jarvis_instance, "system_prompt", "")
            system_msg = f"""{system_prompt}

ZEIT: {now_str}

{personal_text}{brain_section}{memory_section}{brain_file_section}"""

            # ── 6. Messages mit chat_history (wie bot.py) ──────────
            chat_history = getattr(jarvis_instance, "chat_history", [])
            messages = (
                [{"role": "system", "content": system_msg}]
                + list(chat_history[-6:])
                + [{"role": "user", "content": msg}]
            )

            # ── 7. LLM Provider ────────────────────────────────────
            load_dotenv(ENV_PATH, override=True)
            provider = os.getenv("LLM_PROVIDER", "deepseek").lower()

            answer_chunks = []

            if provider == "deepseek":
                api_key = os.getenv("DEEPSEEK_API_KEY", "")
                if not api_key:
                    yield "data: " + json.dumps({"t": "❌ DEEPSEEK_API_KEY nicht gesetzt"}) + "\n\n"
                    return
                try:
                    with httpx.Client(timeout=120) as client:
                        with client.stream("POST",
                            "https://api.deepseek.com/v1/chat/completions",
                            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                            json={"model":"deepseek-chat","messages":messages,"stream":True,"max_tokens":2048}
                        ) as r:
                            if r.status_code != 200:
                                yield "data: " + json.dumps({"t": f"❌ Deepseek {r.status_code}: {r.text[:200]}"}) + "\n\n"
                                return
                            for line in r.iter_lines():
                                if line and line.startswith("data: "):
                                    raw = line[6:].strip()
                                    if raw and raw != "[DONE]":
                                        try:
                                            d = json.loads(raw)
                                            content = d.get("choices",[{}])[0].get("delta",{}).get("content","")
                                            if content:
                                                answer_chunks.append(content)
                                                yield "data: " + json.dumps({"t": content}) + "\n\n"
                                        except json.JSONDecodeError:
                                            pass
                except Exception as e:
                    yield "data: " + json.dumps({"t": f"❌ Deepseek Error: {e}"}) + "\n\n"
                    return

            elif provider == "groq":
                api_key = os.getenv("GROQ_API_KEY", "")
                if not api_key:
                    yield "data: " + json.dumps({"t": "❌ GROQ_API_KEY nicht gesetzt"}) + "\n\n"
                    return
                try:
                    with httpx.Client(timeout=120) as client:
                        with client.stream("POST",
                            "https://api.groq.com/openai/v1/chat/completions",
                            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                            json={"model":"llama-3.3-70b-versatile","messages":messages,"stream":True,"max_tokens":2048}
                        ) as r:
                            if r.status_code != 200:
                                yield "data: " + json.dumps({"t": f"❌ Groq {r.status_code}: {r.text[:200]}"}) + "\n\n"
                                return
                            for line in r.iter_lines():
                                if line and line.startswith("data: "):
                                    raw = line[6:].strip()
                                    if raw and raw != "[DONE]":
                                        try:
                                            d = json.loads(raw)
                                            content = d.get("choices",[{}])[0].get("delta",{}).get("content","")
                                            if content:
                                                answer_chunks.append(content)
                                                yield "data: " + json.dumps({"t": content}) + "\n\n"
                                        except json.JSONDecodeError:
                                            pass
                except Exception as e:
                    yield "data: " + json.dumps({"t": f"❌ Groq Error: {e}"}) + "\n\n"
                    return

            else:
                # Ollama Fallback
                try:
                    import ollama as _ollama
                    model_name = os.getenv("OLLAMA_MODEL", "qwen3:8b")
                    for chunk in _ollama.chat(model=model_name, messages=messages, stream=True):
                        txt = chunk.get("message", {}).get("content", "")
                        if txt:
                            answer_chunks.append(txt)
                            yield "data: " + json.dumps({"t": txt}) + "\n\n"
                except Exception as e:
                    yield "data: " + json.dumps({"t": f"❌ Ollama Error: {e}"}) + "\n\n"
                    return

            # ── 8. Gedächtnis + Logs aktualisieren (exakt wie bot.py) ─
            answer = "".join(answer_chunks)
            if answer:
                # 8a. chat_history (In-Memory, für Kontext der nächsten Frage)
                try:
                    if not hasattr(jarvis_instance, "chat_history"):
                        jarvis_instance.chat_history = []
                    jarvis_instance.chat_history.append({"role": "user",      "content": msg})
                    jarvis_instance.chat_history.append({"role": "assistant", "content": answer})
                except Exception as e:
                    logger.warning(f"chat_history error: {e}")

                # 8b. VectorDB — memory.add_user + add_assistant (ChromaDB)
                try:
                    if hasattr(jarvis_instance, "memory") and jarvis_instance.memory:
                        jarvis_instance.memory.add_user(msg)
                        jarvis_instance.memory.add_assistant(answer)
                except Exception as e:
                    logger.warning(f"VectorDB update error: {e}")

                # 8c. Tages-Log → logs/YYYY-MM-DD.log  (wie jarvis.log_chat)
                try:
                    if hasattr(jarvis_instance, "log_chat"):
                        jarvis_instance.log_chat(msg, answer)
                    else:
                        # Fallback: direkt schreiben
                        log_dir = os.path.join(BASE_DIR, "logs")
                        os.makedirs(log_dir, exist_ok=True)
                        today = datetime.now().strftime("%Y-%m-%d")
                        with open(os.path.join(log_dir, f"{today}.log"), "a", encoding="utf-8") as lf:
                            lf.write(f"[WEB] USER: {msg}\n")
                            lf.write(f"[WEB] BOT:  {answer}\n")
                except Exception as e:
                    logger.warning(f"log_chat error: {e}")

                # 8d. Interessen sofort aktualisieren (identisch zu bot.py)
                try:
                    from modules.proactive_brain import update_interests_from_chat
                    update_interests_from_chat([
                        {"role": "user",      "message": msg},
                        {"role": "assistant", "message": answer},
                    ])
                except Exception as e:
                    logger.debug(f"update_interests error: {e}")

                # ── 9. Persönliches lernen im Hintergrund (wie bot.py) ──
                # bot.py nutzt asyncio.create_task() — wir sind im Flask-Thread
                # also: eigenen Event-Loop in Background-Thread starten
                try:
                    if hasattr(jarvis_instance, "learn_from_message"):
                        _msg_copy = msg
                        def _learn(_m=_msg_copy):
                            loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(loop)
                            try:
                                loop.run_until_complete(jarvis_instance.learn_from_message(_m))
                            except Exception as ex:
                                logger.debug(f"learn_from_message: {ex}")
                            finally:
                                loop.close()
                        threading.Thread(target=_learn, daemon=True).start()
                except Exception as e:
                    logger.debug(f"learn thread error: {e}")

            # ── 8d. Action Detection (Web) ───────────────────────────
            if answer:
                try:
                    pending_web = _detect_action_web(answer)
                    if pending_web:
                        yield "data: " + json.dumps({"action_pending": pending_web}) + "\n\n"
                except Exception as ex_act:
                    logger.debug(f"action_detect_web error: {ex_act}")

            yield "data: " + json.dumps({"done": True}) + "\n\n"

        except Exception as e:
            logger.exception("Chat stream error")
            yield "data: " + json.dumps({"t": f"❌ Fehler: {e}"}) + "\n\n"

    return Response(stream_response(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ════════════════════════════════════════════════════════════════
# SETUP  (wird von bot.py aufgerufen)
# ════════════════════════════════════════════════════════════════

def setup(app_instance, event_bus_instance=None):
    global jarvis_instance, brain_instance, _telegram_app
    logger.info("🌐 Web App: Initializing...")

    if hasattr(app_instance, "bot_data"):
        jarvis_instance = app_instance.bot_data.get("jarvis")
        brain_instance  = app_instance.bot_data.get("brain")
        logger.info(f"✅ Jarvis: {jarvis_instance is not None}")
        logger.info(f"✅ Brain:  {brain_instance  is not None}")

    # Telegram Application für Command-Router speichern
    _telegram_app = app_instance
    logger.info(f"✅ Telegram App für Command-Router: {_telegram_app is not None}")

    port = int(os.getenv("WEB_PORT", 5001))

    # ── Flask mit Retry-Loop starten ───────────────────────────
    # Werkzeug setzt intern SO_REUSEADDR, das hilft bei TIME_WAIT.
    # Falls Port trotzdem noch kurz belegt: bis zu 15s retry.
    def _run_flask():
        import time as _t
        for attempt in range(30):
            try:
                app.run(
                    host="0.0.0.0",
                    port=port,
                    threaded=True,
                    use_reloader=False,
                )
                return
            except OSError as e:
                logger.warning(f"🔌 Flask Port {port} noch belegt ({e}) — retry {attempt+1}/30")
                _t.sleep(0.5)
        logger.error(f"❌ Flask konnte Port {port} nach 15s nicht binden")

    threading.Thread(target=_run_flask, daemon=True).start()
    logger.info(f"🌐 {os.getenv('BOT_NAME', 'RICS')} Web Interface: http://localhost:{port}")