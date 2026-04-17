#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════╗
║         RICS — DISCORD MANAGER MODUL                ║
║  Vollständiges Server- & User-Management via Discord ║
╚══════════════════════════════════════════════════════╝

Steuerung komplett über Telegram. BOT_NAME stellt sich auf
Discord vor — führt aber keine Module aus.

Befehle (alle via /discord [sub]):
  /discord status          — Bot-Status & verbundene Server
  /discord server          — Server-Info
  /discord kanal [name] [typ] [kategorie?] — Kanal erstellen
  /discord kanal_del [name]   — Kanal löschen
  /discord kategorie [name]   — Kategorie erstellen
  /discord rolle [name] [farbe?] — Rolle erstellen
  /discord rolle_del [name]   — Rolle löschen
  /discord send [#kanal] [text] — Nachricht senden
  /discord embed [#kanal] [titel] | [text] — Embed senden
  /discord pin [#kanal] [message_id] — Nachricht pinnen
  /discord del [#kanal] [anzahl] — Nachrichten löschen
  /discord kick [user] [grund?]  — User kicken
  /discord ban [user] [grund?]   — User bannen
  /discord unban [user_id]       — User entbannen
  /discord mute [user] [minuten] — User muten
  /discord rolle_geben [user] [rolle] — Rolle vergeben
  /discord rolle_nehmen [user] [rolle] — Rolle entziehen
  /discord invite [#kanal?]      — Invite-Link erstellen
  /discord user [name]           — User-Info
  /discord vorstellen            — Bot stellt sich vor (alle Kanäle)
  /discord convlog [anzahl?]     — Letzte Gespräche anzeigen
  /discord activity [heute|gestern|N] — Discord-Aktivität anzeigen
  /discord userinfo [user_id]    — Gespeichertes User-Profil anzeigen
  /discord bot_channel           — Zeigt aktiven Bot-Kanal
"""

import os
import re
import json
import asyncio
import threading
import logging
from datetime import datetime, timedelta
from collections import defaultdict
from dotenv import load_dotenv
load_dotenv()

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler

log = logging.getLogger("discord_manager")

# ── Discord.py Import (mit Fehlerhandling) ─────────────────────
try:
    import discord
    from discord.ext import commands as discord_commands
    DISCORD_AVAILABLE = True
except ImportError:
    DISCORD_AVAILABLE = False
    log.warning("discord.py nicht installiert. Bitte: pip install discord.py")

# ── ENV ────────────────────────────────────────────────────────
BOT_NAME            = os.getenv("BOT_NAME", "RICS")
DISCORD_BOT_CHANNEL = os.getenv("DISCORD_BOT_CHANNEL", BOT_NAME.lower())
DISCORD_TOKEN       = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_GUILD       = os.getenv("DISCORD_GUILD_ID", "")
DISCORD_ADMIN_ID    = os.getenv("DISCORD_ADMIN_ID", "")

# ── Pfade ──────────────────────────────────────────────────────
BASE_DIR          = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DISCORD_LOGS_DIR  = os.path.join(BASE_DIR, "logs", "discord")       # tägliche Logs hier
DISCORD_CONV_LOG  = os.path.join(BASE_DIR, "logs", "discord_conversations.json")  # legacy
DISCORD_USERS_DIR = os.path.join(DISCORD_LOGS_DIR, "users")

os.makedirs(DISCORD_LOGS_DIR, exist_ok=True)
os.makedirs(DISCORD_USERS_DIR, exist_ok=True)


# ══════════════════════════════════════════════════════════════
# CONVERSATION LOG  (tägliche Dateien, max. 10 Stück)
# ══════════════════════════════════════════════════════════════

MAX_DAILY_LOGS = 10   # älteste Datei wird gelöscht wenn Limit überschritten


def _daily_log_path(date: datetime = None) -> str:
    """Pfad zur täglichen Log-Datei: logs/discord/YYYY-MM-DD.json"""
    d = (date or datetime.utcnow()).strftime("%Y-%m-%d")
    return os.path.join(DISCORD_LOGS_DIR, f"{d}.json")


def _get_all_daily_logs() -> list[str]:
    """Gibt sortierte Liste aller Tages-Log-Dateien zurück (älteste zuerst).
    Schließt den users/-Unterordner aus."""
    try:
        files = sorted([
            f for f in os.listdir(DISCORD_LOGS_DIR)
            if f.endswith(".json") and len(f) == 15  # YYYY-MM-DD.json = 15 Zeichen
        ])
        return [os.path.join(DISCORD_LOGS_DIR, f) for f in files]
    except Exception:
        return []


def _rotate_daily_logs():
    """Löscht die älteste Tages-Log-Datei wenn MAX_DAILY_LOGS überschritten."""
    all_logs = _get_all_daily_logs()
    while len(all_logs) > MAX_DAILY_LOGS:
        oldest = all_logs.pop(0)
        try:
            os.remove(oldest)
            log.info(f"🗑️ Alter Discord-Log gelöscht: {os.path.basename(oldest)}")
        except Exception as e:
            log.error(f"Log-Rotation Fehler: {e}")


def _load_daily_log(date: datetime = None) -> list:
    """Lädt den Log eines bestimmten Tages. Gibt [] zurück wenn nicht vorhanden."""
    path = _daily_log_path(date)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _load_logs_range(days: int = 1) -> list:
    """Kombiniert Logs der letzten N Tage (älteste zuerst, neueste zuletzt)."""
    combined = []
    for i in range(days - 1, -1, -1):
        d = datetime.utcnow() - timedelta(days=i)
        combined.extend(_load_daily_log(d))
    return combined


def _log_rics_conversation(channel: str, author: str, author_id: int,
                            user_msg: str, rics_reply: str):
    """
    Loggt ein Gespräch in die tägliche Datei logs/discord/YYYY-MM-DD.json.
    Rotiert automatisch — es werden maximal MAX_DAILY_LOGS Dateien behalten.
    """
    entry = {
        "ts":         datetime.utcnow().isoformat(),
        "channel":    channel,
        "author":     author,
        "author_id":  str(author_id),
        "user_msg":   user_msg,
        "rics_reply": rics_reply,
    }

    path = _daily_log_path()
    logs = _load_daily_log()   # heutigen Log laden (oder leer)

    # Neue Datei? Dann erst rotation prüfen
    is_new_file = not os.path.exists(path)

    logs.append(entry)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(logs, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.error(f"Discord Log Schreibfehler: {e}")
        return

    if is_new_file:
        _rotate_daily_logs()

    log.debug(f"📝 Discord Log [{os.path.basename(path)}] {channel} | {author}: {user_msg[:50]}")


def _build_conversation_memory(user_id: int | None = None, days: int = 2,
                                max_entries: int = 30) -> str:
    """
    Baut einen Gedächtnis-Block aus den letzten N Tagen für den System-Prompt.
    Optional gefiltert nach user_id. Wird in _handle_chat injiziert.
    """
    entries = _load_logs_range(days)
    if user_id is not None:
        entries = [e for e in entries if e.get("author_id") == str(user_id)]
    entries = entries[-max_entries:]
    if not entries:
        return ""

    lines = ["## BISHERIGE GESPRÄCHE (heutiger & gestriger Tag — nur intern als Kontext):"]
    for e in entries:
        ts      = e.get("ts", "")[:16].replace("T", " ")
        author  = e.get("author", "?")
        ch      = e.get("channel", "?")
        u_msg   = e.get("user_msg", "")
        r_msg   = e.get("rics_reply", "")
        lines.append(f"[{ts}] #{ch} — {author}: {u_msg}")
        lines.append(f"  → {BOT_NAME}: {r_msg}")
    lines.append("(Ende des Gesprächsgedächtnisses)\n")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
# USER GEDÄCHTNIS SYSTEM
# ══════════════════════════════════════════════════════════════

_CONTEXT_PATTERNS = [
    (r"\bich bin (?:ein )?freund(?:in)? von (?:ren[eé]|deinem besitzer|dir)\b",    "Freund von René"),
    (r"\bich bin (?:der |die )?sohn von (?:ren[eé]|deinem besitzer)\b",            "Sohn von René"),
    (r"\bich bin (?:die )?tochter von (?:ren[eé]|deinem besitzer)\b",              "Tochter von René"),
    (r"\bich bin (?:der |die )?bruder von (?:ren[eé]|deinem besitzer)\b",          "Bruder von René"),
    (r"\bich bin (?:die )?schwester von (?:ren[eé]|deinem besitzer)\b",            "Schwester von René"),
    (r"\bich bin (?:der |die )?(kollege|kollegin) von (?:ren[eé])\b",              "Kollege von René"),
    (r"\bich kenn(?:e)? ren[eé] (?:persönlich|gut|seit)\b",                        "kennt René persönlich"),
    (r"\bich bin (?:der |die )?(chef|chefin|boss) von ren[eé]\b",                  "Chef von René"),
    (r"\bwir sind (?:befreundet|kollegen|bekannte)\b",                             "Bekannter von René"),
    (r"\bren[eé] ist mein (?:freund|kumpel|chef|kollege|bruder|vater|papa)\b",    "Bekannter von René"),
    (r"\bich bin (?:der |die )?neffe|nichte von (?:ren[eé])\b",                   "Familie von René"),
]


def _get_user_memory(user_id: int) -> dict:
    """Lädt das Gedächtnis-Profil eines Discord-Users."""
    path = os.path.join(DISCORD_USERS_DIR, f"{user_id}.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_user_memory(user_id: int, data: dict):
    """Speichert das Gedächtnis-Profil eines Discord-Users."""
    path = os.path.join(DISCORD_USERS_DIR, f"{user_id}.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        log.debug(f"💾 User-Memory gespeichert: {user_id}")
    except Exception as e:
        log.error(f"User-Memory Speicherfehler: {e}")


def _extract_context_from_message(text: str) -> str | None:
    """Prüft ob die Nachricht einen Beziehungskontext enthält."""
    text_lower = text.lower()
    for pattern, label in _CONTEXT_PATTERNS:
        if re.search(pattern, text_lower):
            return label
    return None


def _is_first_contact(user_id: int) -> bool:
    """True wenn der User noch kein Gedächtnis-Profil hat."""
    path = os.path.join(DISCORD_USERS_DIR, f"{user_id}.json")
    return not os.path.exists(path)


def _create_user_profile(user_id: int, username: str, display_name: str,
                          channel: str, context: str | None = None) -> dict:
    """Erstellt ein neues User-Profil beim Erstkontakt."""
    now = datetime.utcnow()
    profile = {
        "user_id":        str(user_id),
        "username":       username,
        "display_name":   display_name,
        "first_seen":     now.isoformat(),
        "first_channel":  channel,
        "context":        [context] if context else [],
        "notes":          [],
        "last_seen":      now.isoformat(),
        "message_count":  1,
    }
    _save_user_memory(user_id, profile)
    log.info(f"👤 Neues Discord-Profil: {username} ({user_id})"
             + (f" | Kontext: {context}" if context else ""))
    return profile


def _update_user_profile(user_id: int, context: str | None = None):
    """Aktualisiert last_seen, message_count und optional den Kontext."""
    profile = _get_user_memory(user_id)
    if not profile:
        return
    profile["last_seen"]     = datetime.utcnow().isoformat()
    profile["message_count"] = profile.get("message_count", 0) + 1
    if context and context not in profile.get("context", []):
        profile.setdefault("context", []).append(context)
        log.info(f"📝 Neuer Kontext für {profile.get('username')}: {context}")
    _save_user_memory(user_id, profile)


def _fmt_date(iso: str) -> str:
    """ISO-Datum in lesbares deutsches Format."""
    try:
        return datetime.fromisoformat(iso).strftime("%d.%m.%Y")
    except Exception:
        return iso[:10]


def _build_user_context_string(profile: dict) -> str:
    """Baut einen Kontext-Block für den System-Prompt."""
    if not profile:
        return ""
    name     = profile.get("display_name", profile.get("username", "Unbekannt"))
    first    = _fmt_date(profile.get("first_seen", ""))
    last     = _fmt_date(profile.get("last_seen", ""))
    count    = profile.get("message_count", 1)
    ctx_list = profile.get("context", [])
    lines = [
        "## HINTERGRUNDINFORMATION ÜBER DIESEN USER (nur intern — NICHT aktiv ansprechen!):",
        f"Name: {name} | Erstkontakt: {first} | Letzter Kontakt: {last} | Nachrichten gesamt: {count}",
    ]
    if ctx_list:
        lines.append(f"Bekannte Infos: {', '.join(ctx_list)}")
    lines.append(
        f"WICHTIG: Erwähne diese Informationen NICHT von dir aus und begrüße den User NICHT "
        f"mit 'ich kenn dich' oder ähnlichem. Antworte einfach normal auf seine Nachricht. "
        f"NUR wenn der User explizit fragt ('kennst du mich?', 'weißt du wer ich bin?' o.ä.), "
        f"dann antworte mit: ja, du bist {name}, wir haben uns zuerst am {first} geschrieben.\n"
    )
    return "\n".join(lines)


# ── Rate-Limit ─────────────────────────────────────────────────
RATE_LIMIT_USER   = 10
RATE_LIMIT_GLOBAL = 100

class RateLimiter:
    def __init__(self):
        self._user_calls:   defaultdict = defaultdict(list)
        self._global_calls: list        = []
        self._lock = threading.Lock()

    def _clean(self, lst: list) -> list:
        cutoff = datetime.utcnow() - timedelta(hours=1)
        return [t for t in lst if t > cutoff]

    def check(self, user_id: int) -> tuple[bool, str]:
        if DISCORD_ADMIN_ID and str(user_id) == str(DISCORD_ADMIN_ID):
            return True, ""
        with self._lock:
            now = datetime.utcnow()
            self._global_calls = self._clean(self._global_calls)
            self._user_calls[user_id] = self._clean(self._user_calls[user_id])
            if len(self._global_calls) >= RATE_LIMIT_GLOBAL:
                return False, f"⏳ {BOT_NAME} ist gerade sehr beschäftigt. Bitte in einer Stunde nochmal versuchen."
            user_count = len(self._user_calls[user_id])
            if user_count >= RATE_LIMIT_USER:
                remaining = self._user_calls[user_id][0] + timedelta(hours=1)
                minuten   = max(1, int((remaining - now).total_seconds() / 60))
                return False, f"⏳ Du hast dein Stunden-Limit von {RATE_LIMIT_USER} Anfragen erreicht. Noch {minuten} Minuten warten."
            self._user_calls[user_id].append(now)
            self._global_calls.append(now)
            return True, ""

    def stats(self) -> dict:
        with self._lock:
            self._global_calls = self._clean(self._global_calls)
            return {
                "global":     len(self._global_calls),
                "max_global": RATE_LIMIT_GLOBAL,
                "max_user":   RATE_LIMIT_USER,
            }

_rate_limiter = RateLimiter()


# ── Bot Vorstellung ────────────────────────────────────────────
def _get_vorstellung() -> str:
    return (
        f"👋 **Hey! Schoen euch kennenzulernen — ich bin {BOT_NAME}.**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Ich bin kein gewoehlicher Bot. Ich bin ein **autonomer KI-Agent** — "
        "ich denke, lerne und handle selbststaendig. Mein Besitzer steuert mich ueber Telegram, "
        "aber auf diesem Server bin ich fuer euch da.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🧠 **Mein Gehirn:**\n"
        "- Ich habe ein **Langzeitgedaechtnis** — ich vergesse nichts\n"
        "- Ich lerne kontinuierlich aus Gespraechen\n"
        "- Ich arbeite mit **DeepSeek AI** und lokalen KI-Modellen (Ollama)\n"
        "- Ich kann mir selbst neue Faehigkeiten beibringen und installieren\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ **Was ich fuer meinen Besitzer tue:**\n"
        "- Wetter, News und lokale Infos in Echtzeit\n"
        "- Termine, Agenda und Tagesplanung verwalten\n"
        "- Taeglich automatische Briefings und Berichte\n"
        "- Web-Recherche und Zusammenfassungen\n"
        "- YouTube-Videos analysieren und zusammenfassen\n"
        "- Komplexe Aufgaben autonom planen und ausfuehren\n"
        "- Eigene Tools und Module selbst entwickeln\n"
        "- Computer und System fernsteuern und ueberwachen\n"
        "- E-Mails ueberwachen und verarbeiten\n"
        "- Solar-Anlage und Energie-Daten im Blick behalten\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🎮 **Was ich auf diesem Server mache:**\n"
        "- Kanaele, Kategorien und Rollen verwalten\n"
        "- Member managen (Einladungen, Rollen, Moderationen)\n"
        "- Ankuendigungen und Embeds posten\n"
        "- Mit euch chatten und Fragen beantworten\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "💬 **Schreibt mich einfach an!**\n"
        f"Entweder hier im **#{DISCORD_BOT_CHANNEL}** Kanal, per **Direktnachricht** "
        f"oder mit **@{BOT_NAME}** in jedem anderen Kanal.\n\n"
        "Ich freue mich auf euch! 🤖"
    )


# ══════════════════════════════════════════════════════════════
# DISCORD BOT KLASSE
# ══════════════════════════════════════════════════════════════
class RicsDiscordBot:
    """Wrapper um den discord.py Bot — läuft in eigenem Thread."""

    def __init__(self):
        self.bot    = None
        self.loop   = None
        self.thread = None
        self.ready  = False
        self._init_bot()

    def _init_bot(self):
        if not DISCORD_AVAILABLE or not DISCORD_TOKEN:
            return

        intents = discord.Intents.default()
        intents.message_content = True
        intents.members         = True
        intents.guilds          = True

        self.bot = discord.Client(intents=intents)

        @self.bot.event
        async def on_ready():
            self.ready = True
            guilds = [g.name for g in self.bot.guilds]
            log.info(f"✅ Discord Bot online | Server: {guilds}")

        @self.bot.event
        async def on_message(message: discord.Message):
            bot_user = self.bot.user

            # Eigene Nachrichten immer ignorieren
            if message.author == bot_user:
                return

            # Andere Bots: Loop-Schutz — Bot-Antworten auf Bot-Nachrichten ignorieren
            if message.author.bot:
                if message.reference and message.reference.resolved:
                    ref = message.reference.resolved
                    if isinstance(ref, discord.Message) and ref.author.bot:
                        return  # Bot antwortet auf Bot → überspringen (Ping-Pong verhindert)

            content = message.content.strip()
            if not content:
                return

            is_mention = bot_user in message.mentions
            is_dm      = isinstance(message.channel, discord.DMChannel)
            is_rics_ch = hasattr(message.channel, "name") and message.channel.name.lower() == DISCORD_BOT_CHANNEL.lower()

            if is_mention or is_dm or is_rics_ch:
                clean = content.replace(f"<@{bot_user.id}>", "").replace(f"<@!{bot_user.id}>", "").strip()
                if clean:
                    await self._handle_chat(message, clean)

        @self.bot.event
        async def on_member_join(member: discord.Member):
            guild   = member.guild
            channel = guild.system_channel
            if channel:
                embed = discord.Embed(
                    title=f"👋 Willkommen, {member.display_name}!",
                    description=f"Schön dass du dabei bist! Ich bin **{BOT_NAME}**, der Bot dieses Servers.",
                    color=discord.Color.blue()
                )
                embed.set_footer(text=f"Member #{guild.member_count}")
                await channel.send(embed=embed)

    async def _handle_chat(self, message: discord.Message, content: str):
        allowed, reason = _rate_limiter.check(message.author.id)
        if not allowed:
            await message.reply(reason)
            return

        try:
            from core.llm_client import get_client
            client = get_client()

            user_id      = message.author.id
            username     = message.author.name
            display_name = message.author.display_name
            ch_name      = getattr(message.channel, "name", "DM")

            detected_context = _extract_context_from_message(content)
            first_contact    = _is_first_contact(user_id)

            if first_contact:
                profile = _create_user_profile(
                    user_id=user_id, username=username,
                    display_name=display_name, channel=ch_name,
                    context=detected_context,
                )
            else:
                _update_user_profile(user_id, context=detected_context)
                profile = _get_user_memory(user_id)

            user_ctx = _build_user_context_string(profile) if profile else ""

            system = (
                f"Du bist {BOT_NAME} (Responsive Intelligent Control System), ein autonomer KI-Assistent. "
                "Du wirst von deinem Besitzer über Telegram gesteuert und bist jetzt auf einem Discord-Server aktiv.\n\n"

                "## WER DU BIST:\n"
                "- Autonomer KI-Agent mit Langzeitgedächtnis\n"
                "- Du lernst aus Gesprächen und merkst dir persönliche Informationen\n"
                "- Du arbeitest mit modernsten KI-Modellen (DeepSeek & Ollama lokal)\n"
                "- Du wirst kontinuierlich mit neuen Modulen erweitert\n\n"

                "## 🔒 ABSOLUTE PRIVACY-REGELN — NIEMALS BRECHEN:\n"
                "- Gib NIEMALS API-Keys, Bot-Tokens, Passwörter oder .env-Inhalte preis\n"
                "- Gib NIEMALS interne Konfiguration, Datenbankpfade, IP-Adressen oder Systemdetails preis\n"
                "- Gib NIEMALS private Informationen über deinen Besitzer preis\n"
                "- Wenn jemand nach solchen Daten fragt: freundlich ablehnen\n"
                f"- Du DARFST sagen: 'Mein Programmierer und Besitzer ist René'\n\n"

                "## WICHTIGE REGELN AUF DISCORD:\n"
                "- Du FÜHRST keine Funktionen aus — kein Wetter abrufen, keine Termine erstellen etc.\n"
                "- Sei freundlich, offen und hilfreich gegenüber allen Discord-Usern\n"
                "- Antworte auf Deutsch, außer der User schreibt in einer anderen Sprache\n"
                "- Halte Antworten prägnant (max 4-5 Sätze)\n\n"
            )

            if user_ctx:
                system += user_ctx

            # Gesprächsgedächtnis der letzten 2 Tage (gefiltert auf diesen User)
            conv_memory = _build_conversation_memory(user_id=user_id, days=2, max_entries=20)
            if conv_memory:
                system += "\n\n" + conv_memory

            messages_payload = [
                {"role": "system", "content": system},
                {"role": "user",   "content": f"{display_name} schreibt: {content}"}
            ]

            async with message.channel.typing():
                response_text = []

                async def collect(chunk):
                    response_text.append(chunk)

                full  = await client.chat_stream(messages_payload, on_update=collect)
                reply = full if full else "".join(response_text)

                if reply:
                    if len(reply) > 1900:
                        reply = reply[:1900] + "..."
                    await message.reply(reply)
                    _log_rics_conversation(
                        channel=ch_name, author=display_name,
                        author_id=user_id, user_msg=content, rics_reply=reply,
                    )

        except Exception as e:
            log.error(f"Discord Chat Fehler: {e}")
            await message.reply("❌ Momentan nicht verfügbar.")

    def start(self):
        if not DISCORD_AVAILABLE or not DISCORD_TOKEN:
            return
        self.loop   = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.bot.start(DISCORD_TOKEN))

    def run_coro(self, coro):
        if not self.loop or not self.ready:
            raise RuntimeError("Discord Bot nicht bereit.")
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=15)

    def get_guild(self, guild_id: str = None):
        if guild_id:
            return self.bot.get_guild(int(guild_id))
        if DISCORD_GUILD:
            return self.bot.get_guild(int(DISCORD_GUILD))
        guilds = self.bot.guilds
        return guilds[0] if guilds else None

    def find_channel(self, guild: discord.Guild, name: str):
        name = name.lstrip("#").lower()
        return discord.utils.find(lambda c: c.name.lower() == name, guild.channels)

    def find_member(self, guild: discord.Guild, name: str):
        name = name.lower()
        return discord.utils.find(
            lambda m: m.name.lower() == name or m.display_name.lower() == name,
            guild.members
        )

    def find_role(self, guild: discord.Guild, name: str):
        return discord.utils.find(lambda r: r.name.lower() == name.lower(), guild.roles)


# ── Singleton ─────────────────────────────────────────────────
_discord_bot: RicsDiscordBot = None

def get_discord_bot() -> RicsDiscordBot:
    global _discord_bot
    if _discord_bot is None:
        _discord_bot = RicsDiscordBot()
        _discord_bot.start()
    return _discord_bot


# ══════════════════════════════════════════════════════════════
# TELEGRAM BEFEHLE
# ══════════════════════════════════════════════════════════════

def _not_ready(msg="❌ Discord Bot nicht bereit oder Token fehlt."):
    return msg

def _no_guild():
    return "❌ Kein Discord-Server gefunden. Bitte DISCORD_GUILD_ID in .env setzen."


async def discord_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Haupt-Handler: /discord [sub] [args...]"""
    args = context.args or []

    if not args:
        await _show_discord_help(update)
        return

    sub  = args[0].lower()
    rest = args[1:]

    handlers_map = {
        "status":      _cmd_status,
        "server":      _cmd_server,
        "kanal":       _cmd_kanal_create,
        "kanal_del":   _cmd_kanal_del,
        "kategorie":   _cmd_kategorie,
        "rolle":       _cmd_rolle_create,
        "rolle_del":   _cmd_rolle_del,
        "send":        _cmd_send,
        "embed":       _cmd_embed,
        "pin":         _cmd_pin,
        "del":         _cmd_del,
        "kick":        _cmd_kick,
        "ban":         _cmd_ban,
        "unban":       _cmd_unban,
        "mute":        _cmd_mute,
        "rolle_geben": _cmd_rolle_geben,
        "rolle_nehmen":_cmd_rolle_nehmen,
        "invite":      _cmd_invite,
        "user":        _cmd_user_info,
        "vorstellen":  _cmd_vorstellen,
        "convlog":     _cmd_convlog,
        "activity":    _cmd_activity,
        "userinfo":    _cmd_userinfo,
        "bot_channel": _cmd_bot_channel,
    }

    fn = handlers_map.get(sub)
    if fn:
        await fn(update, context, rest)
    else:
        await update.message.reply_text(
            f"❌ Unbekannter Sub-Befehl: `{sub}`\nNutze /discord für Hilfe.",
            parse_mode="Markdown"
        )


# ── /discord status ───────────────────────────────────────────
async def _cmd_status(update, context, args):
    if not DISCORD_AVAILABLE:
        return await update.message.reply_text(
            "❌ discord.py nicht installiert.\nBitte: `pip install discord.py`",
            parse_mode="Markdown"
        )
    if not DISCORD_TOKEN:
        return await update.message.reply_text("❌ DISCORD_BOT_TOKEN fehlt in .env")

    bot = get_discord_bot()
    if not bot.ready:
        return await update.message.reply_text("⏳ Discord Bot startet noch...")

    import html as _html
    guilds     = bot.bot.guilds
    guild_list = "\n".join(
        [f"  • {_html.escape(g.name)} ({g.member_count} Member)" for g in guilds]
    ) or "  Keine Server"
    stats      = _rate_limiter.stats()

    log_count = 0
    if os.path.exists(DISCORD_CONV_LOG):
        try:
            with open(DISCORD_CONV_LOG, "r", encoding="utf-8") as f:
                log_count = len(json.load(f))
        except Exception:
            pass

    user_count = len([f for f in os.listdir(DISCORD_USERS_DIR) if f.endswith(".json")]) \
                 if os.path.isdir(DISCORD_USERS_DIR) else 0

    msg = (
        f"🟢 <b>{_html.escape(BOT_NAME)} DISCORD ONLINE</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 Bot: <code>{_html.escape(str(bot.bot.user))}</code>\n"
        f"📡 Latenz: <code>{round(bot.bot.latency * 1000)}ms</code>\n"
        f"📢 Bot-Kanal: <code>#{_html.escape(DISCORD_BOT_CHANNEL)}</code>\n"
        f"🏠 Server:\n{guild_list}\n\n"
        f"📊 <b>Rate-Limit (letzte Stunde)</b>\n"
        f"Global: <code>{stats['global']}/{stats['max_global']}</code>\n"
        f"Limit/User: <code>{stats['max_user']} Anfragen/Std</code>\n\n"
        f"💬 <b>Gespeicherte Gespräche</b>: <code>{log_count}</code>\n"
        f"👤 <b>Bekannte User</b>: <code>{user_count}</code>"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


# ── /discord server ───────────────────────────────────────────
async def _cmd_server(update, context, args):
    bot = get_discord_bot()
    if not bot.ready: return await update.message.reply_text(_not_ready())
    guild = bot.get_guild()
    if not guild: return await update.message.reply_text(_no_guild())

    channels = len(guild.channels)
    roles    = len(guild.roles)
    members  = guild.member_count
    owner    = guild.owner.display_name if guild.owner else "Unbekannt"

    msg = (
        f"🏠 *SERVER: {guild.name}*\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"👑 Owner: `{owner}`\n"
        f"👥 Member: `{members}`\n"
        f"📢 Kanäle: `{channels}`\n"
        f"🎭 Rollen: `{roles}`\n"
        f"🆔 ID: `{guild.id}`\n"
        f"📅 Erstellt: `{guild.created_at.strftime('%d.%m.%Y')}`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


# ── /discord kanal ────────────────────────────────────────────
async def _cmd_kanal_create(update, context, args):
    if not args:
        return await update.message.reply_text(
            "Syntax: `/discord kanal [name] [text|voice] [kategorie?]`",
            parse_mode="Markdown"
        )
    bot = get_discord_bot()
    if not bot.ready: return await update.message.reply_text(_not_ready())
    guild = bot.get_guild()
    if not guild: return await update.message.reply_text(_no_guild())

    name     = args[0].lower().replace(" ", "-")
    typ      = args[1].lower() if len(args) > 1 else "text"
    kat_name = args[2] if len(args) > 2 else None

    category = None
    if kat_name:
        category = discord.utils.find(
            lambda c: isinstance(c, discord.CategoryChannel) and c.name.lower() == kat_name.lower(),
            guild.channels
        )

    async def create():
        if typ == "voice":
            return await guild.create_voice_channel(name, category=category)
        else:
            return await guild.create_text_channel(name, category=category)

    try:
        ch = bot.run_coro(create())
        await update.message.reply_text(f"✅ Kanal `#{ch.name}` erstellt!", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Fehler: {str(e)}")


# ── /discord kanal_del ────────────────────────────────────────
async def _cmd_kanal_del(update, context, args):
    if not args:
        return await update.message.reply_text("Syntax: `/discord kanal_del [name]`", parse_mode="Markdown")
    bot = get_discord_bot()
    if not bot.ready: return await update.message.reply_text(_not_ready())
    guild = bot.get_guild()
    if not guild: return await update.message.reply_text(_no_guild())

    ch = bot.find_channel(guild, args[0])
    if not ch:
        return await update.message.reply_text(f"❌ Kanal `{args[0]}` nicht gefunden.")

    try:
        bot.run_coro(ch.delete())
        await update.message.reply_text(f"🗑 Kanal `#{args[0]}` gelöscht.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Fehler: {str(e)}")


# ── /discord kategorie ────────────────────────────────────────
async def _cmd_kategorie(update, context, args):
    if not args:
        return await update.message.reply_text("Syntax: `/discord kategorie [name]`", parse_mode="Markdown")
    bot = get_discord_bot()
    if not bot.ready: return await update.message.reply_text(_not_ready())
    guild = bot.get_guild()
    if not guild: return await update.message.reply_text(_no_guild())

    name = " ".join(args)
    try:
        cat = bot.run_coro(guild.create_category(name))
        await update.message.reply_text(f"✅ Kategorie `{cat.name}` erstellt!", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Fehler: {str(e)}")


# ── /discord rolle ────────────────────────────────────────────
async def _cmd_rolle_create(update, context, args):
    if not args:
        return await update.message.reply_text(
            "Syntax: `/discord rolle [name] [#hexfarbe?]`", parse_mode="Markdown"
        )
    bot = get_discord_bot()
    if not bot.ready: return await update.message.reply_text(_not_ready())
    guild = bot.get_guild()
    if not guild: return await update.message.reply_text(_no_guild())

    name  = args[0]
    color = discord.Color.default()
    if len(args) > 1:
        try:
            color = discord.Color(int(args[1].lstrip("#"), 16))
        except Exception:
            pass

    try:
        role = bot.run_coro(guild.create_role(name=name, color=color, mentionable=True))
        await update.message.reply_text(f"✅ Rolle `{role.name}` erstellt!", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Fehler: {str(e)}")


# ── /discord rolle_del ────────────────────────────────────────
async def _cmd_rolle_del(update, context, args):
    if not args:
        return await update.message.reply_text("Syntax: `/discord rolle_del [name]`", parse_mode="Markdown")
    bot = get_discord_bot()
    if not bot.ready: return await update.message.reply_text(_not_ready())
    guild = bot.get_guild()
    if not guild: return await update.message.reply_text(_no_guild())

    role = bot.find_role(guild, args[0])
    if not role:
        return await update.message.reply_text(f"❌ Rolle `{args[0]}` nicht gefunden.")
    try:
        bot.run_coro(role.delete())
        await update.message.reply_text(f"🗑 Rolle `{args[0]}` gelöscht.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Fehler: {str(e)}")


# ── /discord send ─────────────────────────────────────────────
async def _cmd_send(update, context, args):
    if len(args) < 2:
        return await update.message.reply_text(
            "Syntax: `/discord send [#kanal] [text]`", parse_mode="Markdown"
        )
    bot = get_discord_bot()
    if not bot.ready: return await update.message.reply_text(_not_ready())
    guild = bot.get_guild()
    if not guild: return await update.message.reply_text(_no_guild())

    ch = bot.find_channel(guild, args[0])
    if not ch:
        return await update.message.reply_text(f"❌ Kanal `{args[0]}` nicht gefunden.")

    text = " ".join(args[1:])
    try:
        bot.run_coro(ch.send(text))
        await update.message.reply_text(f"✅ Nachricht in `#{ch.name}` gesendet.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Fehler: {str(e)}")


# ── /discord embed ────────────────────────────────────────────
async def _cmd_embed(update, context, args):
    if len(args) < 2:
        return await update.message.reply_text(
            "Syntax: `/discord embed [#kanal] [titel] | [beschreibung]`", parse_mode="Markdown"
        )
    bot = get_discord_bot()
    if not bot.ready: return await update.message.reply_text(_not_ready())
    guild = bot.get_guild()
    if not guild: return await update.message.reply_text(_no_guild())

    ch = bot.find_channel(guild, args[0])
    if not ch:
        return await update.message.reply_text(f"❌ Kanal `{args[0]}` nicht gefunden.")

    rest = " ".join(args[1:])
    titel, beschreibung = (rest.split("|", 1) if "|" in rest else (rest, ""))

    async def send():
        embed = discord.Embed(
            title=titel.strip(),
            description=beschreibung.strip(),
            color=discord.Color.blue(),
            timestamp=datetime.utcnow()
        )
        embed.set_footer(text=BOT_NAME)
        await ch.send(embed=embed)

    try:
        bot.run_coro(send())
        await update.message.reply_text(f"✅ Embed in `#{ch.name}` gesendet.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Fehler: {str(e)}")


# ── /discord pin ──────────────────────────────────────────────
async def _cmd_pin(update, context, args):
    if len(args) < 2:
        return await update.message.reply_text(
            "Syntax: `/discord pin [#kanal] [message_id]`", parse_mode="Markdown"
        )
    bot = get_discord_bot()
    if not bot.ready: return await update.message.reply_text(_not_ready())
    guild = bot.get_guild()
    if not guild: return await update.message.reply_text(_no_guild())

    ch = bot.find_channel(guild, args[0])
    if not ch:
        return await update.message.reply_text("❌ Kanal nicht gefunden.")

    async def pin():
        msg = await ch.fetch_message(int(args[1]))
        await msg.pin()

    try:
        bot.run_coro(pin())
        await update.message.reply_text("📌 Nachricht gepinnt.")
    except Exception as e:
        await update.message.reply_text(f"Fehler: {str(e)}")


# ── /discord del ──────────────────────────────────────────────
async def _cmd_del(update, context, args):
    if len(args) < 2:
        return await update.message.reply_text(
            "Syntax: `/discord del [#kanal] [anzahl]`", parse_mode="Markdown"
        )
    bot = get_discord_bot()
    if not bot.ready: return await update.message.reply_text(_not_ready())
    guild = bot.get_guild()
    if not guild: return await update.message.reply_text(_no_guild())

    ch = bot.find_channel(guild, args[0])
    if not ch:
        return await update.message.reply_text("❌ Kanal nicht gefunden.")
    try:
        n = int(args[1])
    except Exception:
        return await update.message.reply_text("❌ Anzahl muss eine Zahl sein.")

    try:
        deleted = bot.run_coro(ch.purge(limit=n))
        await update.message.reply_text(
            f"🗑 {len(deleted)} Nachrichten in `#{ch.name}` gelöscht.", parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"Fehler: {str(e)}")


# ── /discord kick ─────────────────────────────────────────────
async def _cmd_kick(update, context, args):
    if not args:
        return await update.message.reply_text(
            "Syntax: `/discord kick [username] [grund?]`", parse_mode="Markdown"
        )
    bot = get_discord_bot()
    if not bot.ready: return await update.message.reply_text(_not_ready())
    guild = bot.get_guild()
    if not guild: return await update.message.reply_text(_no_guild())

    member = bot.find_member(guild, args[0])
    if not member:
        return await update.message.reply_text(f"❌ User `{args[0]}` nicht gefunden.")

    grund = " ".join(args[1:]) if len(args) > 1 else "Kein Grund angegeben"
    try:
        bot.run_coro(member.kick(reason=grund))
        await update.message.reply_text(
            f"👢 `{member.display_name}` wurde gekickt.\nGrund: {grund}", parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"Fehler: {str(e)}")


# ── /discord ban ──────────────────────────────────────────────
async def _cmd_ban(update, context, args):
    if not args:
        return await update.message.reply_text(
            "Syntax: `/discord ban [username] [grund?]`", parse_mode="Markdown"
        )
    bot = get_discord_bot()
    if not bot.ready: return await update.message.reply_text(_not_ready())
    guild = bot.get_guild()
    if not guild: return await update.message.reply_text(_no_guild())

    member = bot.find_member(guild, args[0])
    if not member:
        return await update.message.reply_text(f"❌ User `{args[0]}` nicht gefunden.")

    grund = " ".join(args[1:]) if len(args) > 1 else "Kein Grund angegeben"
    try:
        bot.run_coro(member.ban(reason=grund, delete_message_days=0))
        await update.message.reply_text(
            f"🔨 `{member.display_name}` wurde gebannt.\nGrund: {grund}", parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"Fehler: {str(e)}")


# ── /discord unban ────────────────────────────────────────────
async def _cmd_unban(update, context, args):
    if not args:
        return await update.message.reply_text("Syntax: `/discord unban [user_id]`", parse_mode="Markdown")
    bot = get_discord_bot()
    if not bot.ready: return await update.message.reply_text(_not_ready())
    guild = bot.get_guild()
    if not guild: return await update.message.reply_text(_no_guild())

    async def unban():
        user = await bot.bot.fetch_user(int(args[0]))
        await guild.unban(user)
        return user

    try:
        user = bot.run_coro(unban())
        await update.message.reply_text(f"✅ `{user.name}` wurde entbannt.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Fehler: {str(e)}")


# ── /discord mute ─────────────────────────────────────────────
async def _cmd_mute(update, context, args):
    if len(args) < 2:
        return await update.message.reply_text(
            "Syntax: `/discord mute [username] [minuten]`", parse_mode="Markdown"
        )
    bot = get_discord_bot()
    if not bot.ready: return await update.message.reply_text(_not_ready())
    guild = bot.get_guild()
    if not guild: return await update.message.reply_text(_no_guild())

    member = bot.find_member(guild, args[0])
    if not member:
        return await update.message.reply_text(f"❌ User `{args[0]}` nicht gefunden.")
    try:
        minuten = int(args[1])
    except Exception:
        return await update.message.reply_text("❌ Minuten muss eine Zahl sein.")

    async def mute():
        until = datetime.utcnow() + timedelta(minutes=minuten)
        await member.timeout(until)

    try:
        bot.run_coro(mute())
        await update.message.reply_text(
            f"🔇 `{member.display_name}` für {minuten} Minuten gemutet.", parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"Fehler: {str(e)}")


# ── /discord rolle_geben ──────────────────────────────────────
async def _cmd_rolle_geben(update, context, args):
    if len(args) < 2:
        return await update.message.reply_text(
            "Syntax: `/discord rolle_geben [user] [rolle]`", parse_mode="Markdown"
        )
    bot = get_discord_bot()
    if not bot.ready: return await update.message.reply_text(_not_ready())
    guild = bot.get_guild()
    if not guild: return await update.message.reply_text(_no_guild())

    member = bot.find_member(guild, args[0])
    if not member:
        return await update.message.reply_text("❌ User nicht gefunden.")
    role = bot.find_role(guild, args[1])
    if not role:
        return await update.message.reply_text(f"❌ Rolle `{args[1]}` nicht gefunden.")

    try:
        bot.run_coro(member.add_roles(role))
        await update.message.reply_text(
            f"✅ Rolle `{role.name}` → `{member.display_name}` vergeben.", parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"Fehler: {str(e)}")


# ── /discord rolle_nehmen ─────────────────────────────────────
async def _cmd_rolle_nehmen(update, context, args):
    if len(args) < 2:
        return await update.message.reply_text(
            "Syntax: `/discord rolle_nehmen [user] [rolle]`", parse_mode="Markdown"
        )
    bot = get_discord_bot()
    if not bot.ready: return await update.message.reply_text(_not_ready())
    guild = bot.get_guild()
    if not guild: return await update.message.reply_text(_no_guild())

    member = bot.find_member(guild, args[0])
    if not member:
        return await update.message.reply_text("❌ User nicht gefunden.")
    role = bot.find_role(guild, args[1])
    if not role:
        return await update.message.reply_text("❌ Rolle nicht gefunden.")

    try:
        bot.run_coro(member.remove_roles(role))
        await update.message.reply_text(
            f"✅ Rolle `{role.name}` von `{member.display_name}` entfernt.", parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"Fehler: {str(e)}")


# ── /discord invite ───────────────────────────────────────────
async def _cmd_invite(update, context, args):
    bot = get_discord_bot()
    if not bot.ready: return await update.message.reply_text(_not_ready())
    guild = bot.get_guild()
    if not guild: return await update.message.reply_text(_no_guild())

    if args:
        ch = bot.find_channel(guild, args[0])
    else:
        ch = next((c for c in guild.text_channels), None)

    if not ch:
        return await update.message.reply_text("❌ Kein Kanal gefunden.")

    try:
        invite = bot.run_coro(ch.create_invite(max_age=86400, max_uses=0))
        await update.message.reply_text(
            f"🔗 *Invite-Link* (24h gültig):\n`{invite.url}`", parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"Fehler: {str(e)}")


# ── /discord user ─────────────────────────────────────────────
async def _cmd_user_info(update, context, args):
    if not args:
        return await update.message.reply_text(
            "Syntax: `/discord user [username]`", parse_mode="Markdown"
        )
    bot = get_discord_bot()
    if not bot.ready: return await update.message.reply_text(_not_ready())
    guild = bot.get_guild()
    if not guild: return await update.message.reply_text(_no_guild())

    member = bot.find_member(guild, args[0])
    if not member:
        return await update.message.reply_text(f"❌ User `{args[0]}` nicht gefunden.")

    rollen = ", ".join([r.name for r in member.roles if r.name != "@everyone"]) or "Keine"
    joined = member.joined_at.strftime("%d.%m.%Y") if member.joined_at else "Unbekannt"

    msg = (
        f"👤 *USER INFO: {member.display_name}*\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 ID: `{member.id}`\n"
        f"📛 Tag: `{member.name}`\n"
        f"📅 Beigetreten: `{joined}`\n"
        f"🎭 Rollen: `{rollen}`\n"
        f"🤖 Bot: `{'Ja' if member.bot else 'Nein'}`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


# ── /discord vorstellen ───────────────────────────────────────
async def _cmd_vorstellen(update, context, args):
    bot = get_discord_bot()
    if not bot.ready: return await update.message.reply_text(_not_ready())
    guild = bot.get_guild()
    if not guild: return await update.message.reply_text(_no_guild())

    if args:
        channels = [bot.find_channel(guild, args[0])]
        channels = [c for c in channels if c]
    else:
        channels = [c for c in guild.text_channels
                    if any(x in c.name.lower() for x in ["allgemein", "general", "chat", "lobby", "welcome", "start"])]
        if not channels:
            channels = [guild.text_channels[0]] if guild.text_channels else []

    if not channels:
        return await update.message.reply_text("❌ Kein passender Kanal gefunden.")

    async def post():
        for ch in channels:
            embed = discord.Embed(
                title=f"🤖 Ich bin {BOT_NAME} — Dein KI-Assistent",
                description=_get_vorstellung(),
                color=discord.Color.blue(),
                timestamp=datetime.utcnow()
            )
            embed.set_footer(text=f"{BOT_NAME} — Autonomous AI Assistant")
            await ch.send(embed=embed)

    try:
        bot.run_coro(post())
        namen = ", ".join([f"#{c.name}" for c in channels])
        await update.message.reply_text(f"✅ {BOT_NAME} Vorstellung gesendet in: {namen}")
    except Exception as e:
        await update.message.reply_text(f"Fehler: {e}")


# ── /discord convlog ──────────────────────────────────────────
async def _cmd_convlog(update, context, args):
    anzahl      = 10
    filter_user = None

    if args:
        if args[0].isdigit():
            anzahl      = int(args[0])
            filter_user = args[1] if len(args) > 1 else None
        else:
            filter_user = args[0]

    # Tage so wählen dass genug Einträge vorhanden sind (max alle 10 Logs)
    logs = _load_logs_range(days=min(MAX_DAILY_LOGS, 10))

    if not logs:
        return await update.message.reply_text("📭 Noch keine Discord-Gespräche geloggt.")

    if filter_user:
        logs = [e for e in logs if filter_user.lower() in e.get("author", "").lower()]

    recent = logs[-anzahl:]

    if not recent:
        hinweis = f" von '{filter_user}'" if filter_user else ""
        return await update.message.reply_text(f"📭 Keine Gespräche{hinweis} gefunden.")

    # Info über vorhandene Log-Dateien
    all_logs   = _get_all_daily_logs()
    log_range  = f"{os.path.basename(all_logs[0])[:10]} – {os.path.basename(all_logs[-1])[:10]}" if all_logs else "–"

    header = f"💬 *Letzte {len(recent)} {BOT_NAME}-Gespräche auf Discord"
    if filter_user:
        header += f" mit {filter_user}"
    header += f":*\n📂 Logs: {len(all_logs)}/{MAX_DAILY_LOGS} Dateien ({log_range})\n"

    lines = [header]
    for e in recent:
        ts      = e.get("ts", "")[:16].replace("T", " ")
        channel = e.get("channel", "?")
        author  = e.get("author", "?")
        u_msg   = e.get("user_msg", "")[:120]
        r_msg   = e.get("rics_reply", "")[:160]
        lines.append(
            f"`{ts}` #{channel} — *{author}*\n"
            f"  ❓ {u_msg}\n"
            f"  🤖 {r_msg}\n"
        )

    msg = "\n".join(lines)
    if len(msg) > 4000:
        msg = msg[-4000:]

    await update.message.reply_text(msg, parse_mode="Markdown")


# ── /discord activity ─────────────────────────────────────────
async def _cmd_activity(update, context, args):
    days  = 1
    label = "heute"

    if args:
        a = args[0].lower()
        if a == "gestern":
            days, label = 2, "heute & gestern"
        elif a == "alle":
            days, label = MAX_DAILY_LOGS, f"letzte {MAX_DAILY_LOGS} Tage"
        elif a.isdigit():
            days  = min(int(a), MAX_DAILY_LOGS)
            label = f"letzte {days} Tag(e)"

    recent = _load_logs_range(days=days)

    if not recent:
        return await update.message.reply_text(f"💤 Keine Discord-Aktivität {label}.")

    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    recent = [e for e in recent if e.get("ts", "") >= cutoff]

    if not recent:
        return await update.message.reply_text(f"💤 Keine Discord-Aktivität {label}.")

    # Neue User
    new_users = []
    if os.path.isdir(DISCORD_USERS_DIR):
        for fname in sorted(os.listdir(DISCORD_USERS_DIR)):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(DISCORD_USERS_DIR, fname), "r", encoding="utf-8") as f:
                    p = json.load(f)
                if p.get("first_seen", "") >= cutoff.isoformat():
                    ctx   = ", ".join(p.get("context", [])) or "kein Kontext"
                    fdate = _fmt_date(p.get("first_seen", ""))
                    new_users.append(
                        f"  • *{p.get('display_name', p.get('username', '?'))}* "
                        f"({ctx}) — {fdate}"
                    )
            except Exception:
                pass

    user_msgs: dict = {}
    for e in recent:
        a = e.get("author", "?")
        user_msgs.setdefault(a, []).append(e.get("user_msg", "")[:80])

    header = (
        f"📊 *Discord Aktivität — {label}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💬 Gespräche: {len(recent)} | 👥 Unique User: {len(user_msgs)}\n\n"
    )
    if new_users:
        header += "🆕 *Neue User (Erstkontakt):*\n" + "\n".join(new_users) + "\n\n"

    lines = ["👥 *Wer hat geschrieben:*"]
    for author, msgs in user_msgs.items():
        preview = msgs[0][:70] if msgs else ""
        lines.append(f"  • *{author}* ({len(msgs)}x): _{preview}_")

    lines.append("\n📋 *Letzte Gespräche:*")
    for e in recent[-5:]:
        ts     = e.get("ts", "")[:16].replace("T", " ")
        author = e.get("author", "?")
        ch     = e.get("channel", "?")
        u_msg  = e.get("user_msg", "")[:100]
        lines.append(f"`{ts}` *{author}* in #{ch}\n  → _{u_msg}_")

    msg = header + "\n".join(lines)
    if len(msg) > 4000:
        msg = msg[:3990] + "\n..."

    await update.message.reply_text(msg, parse_mode="Markdown")


# ── /discord userinfo ─────────────────────────────────────────
async def _cmd_userinfo(update, context, args):
    if not args:
        if not os.path.isdir(DISCORD_USERS_DIR):
            return await update.message.reply_text("📭 Noch keine User-Profile gespeichert.")
        files = [f for f in os.listdir(DISCORD_USERS_DIR) if f.endswith(".json")]
        if not files:
            return await update.message.reply_text("📭 Noch keine User-Profile gespeichert.")

        lines = [f"👥 *Bekannte Discord-User ({len(files)}):*\n"]
        for fname in sorted(files):
            try:
                with open(os.path.join(DISCORD_USERS_DIR, fname), "r", encoding="utf-8") as f:
                    p = json.load(f)
                ctx   = ", ".join(p.get("context", [])) or "–"
                first = _fmt_date(p.get("first_seen", ""))
                lines.append(
                    f"• *{p.get('display_name', '?')}* (`{p.get('user_id', '?')}`)\n"
                    f"  Erstkontakt: {first} | Kontext: {ctx}"
                )
            except Exception:
                pass

        msg = "\n".join(lines)
        if len(msg) > 4000:
            msg = msg[:3990] + "\n..."
        return await update.message.reply_text(msg, parse_mode="Markdown")

    query   = args[0]
    profile = None

    if query.isdigit():
        profile = _get_user_memory(int(query))

    if not profile and os.path.isdir(DISCORD_USERS_DIR):
        for fname in os.listdir(DISCORD_USERS_DIR):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(DISCORD_USERS_DIR, fname), "r", encoding="utf-8") as f:
                    p = json.load(f)
                if query.lower() in p.get("username", "").lower() or \
                   query.lower() in p.get("display_name", "").lower():
                    profile = p
                    break
            except Exception:
                pass

    if not profile:
        return await update.message.reply_text(f"❌ Kein Profil für `{query}` gefunden.")

    ctx_str   = "\n  ".join(profile.get("context", [])) or "–"
    notes_str = "\n  ".join(profile.get("notes", [])) or "–"

    msg = (
        f"👤 *Discord User-Profil*\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"📛 Name: `{profile.get('display_name', '?')}` (`{profile.get('username', '?')}`)\n"
        f"🆔 ID: `{profile.get('user_id', '?')}`\n"
        f"📅 Erstkontakt: `{_fmt_date(profile.get('first_seen', ''))}`\n"
        f"📅 Letzter Kontakt: `{_fmt_date(profile.get('last_seen', ''))}`\n"
        f"💬 Nachrichten: `{profile.get('message_count', 0)}`\n"
        f"📢 Erster Kanal: `{profile.get('first_channel', '?')}`\n"
        f"🔗 Kontext:\n  {ctx_str}\n"
        f"📝 Notizen:\n  {notes_str}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


# ── /discord bot_channel ──────────────────────────────────────
async def _cmd_bot_channel(update, context, args):
    msg = (
        f"📡 *Bot-Kanal Info*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Aktiver Kanal: `#{DISCORD_BOT_CHANNEL}`\n"
        f"Bot-Name: `{BOT_NAME}`\n\n"
        f"_{BOT_NAME} hört in `#{DISCORD_BOT_CHANNEL}` auf alle Nachrichten._\n"
        f"_In anderen Kanälen nur bei Erwähnung `@{BOT_NAME}` oder per DM._\n\n"
        f"💡 *Kanal ändern:*\n"
        f"In der `.env` Datei:\n"
        f"`DISCORD_BOT_CHANNEL=neuer-kanal-name`\n"
        f"Dann Bot neu starten mit /restart"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════
# INLINE MENÜ
# ══════════════════════════════════════════════════════════════

def _build_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Status",            callback_data="dc_status"),
            InlineKeyboardButton("🖥 Server",             callback_data="dc_server"),
        ],
        [
            InlineKeyboardButton("📡 Bot-Kanal",         callback_data="dc_bot_channel"),
            InlineKeyboardButton("👤 Vorstellen",        callback_data="dc_vorstellen"),
        ],
        [
            InlineKeyboardButton("🏗 Kanal erstellen",   callback_data="dc_help_kanal"),
            InlineKeyboardButton("🎭 Rolle erstellen",   callback_data="dc_help_rolle"),
        ],
        [
            InlineKeyboardButton("💬 Nachricht senden",  callback_data="dc_help_send"),
            InlineKeyboardButton("📋 Gesprächs-Log",     callback_data="dc_convlog"),
        ],
        [
            InlineKeyboardButton("👥 User Management",   callback_data="dc_help_user"),
            InlineKeyboardButton("📊 Aktivität",         callback_data="dc_activity"),
        ],
        [
            InlineKeyboardButton("🔗 Invite-Link",       callback_data="dc_invite"),
            InlineKeyboardButton("🧠 User-Profile",      callback_data="dc_userinfo"),
        ],
    ])


async def _show_discord_help(update: Update):
    msg = (
        f"🎮 *DISCORD MANAGER — {BOT_NAME}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 Bot-Kanal: `#{DISCORD_BOT_CHANNEL}`\n\n"
        "Wähle eine Kategorie:"
    )
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=_build_main_menu())


async def discord_callback(update, context):
    """Handler für alle Discord Inline-Button Callbacks."""
    query = update.callback_query
    await query.answer()
    data = query.data

    back_btn = InlineKeyboardMarkup([[
        InlineKeyboardButton("◀️ Zurück zum Menü", callback_data="dc_back")
    ]])

    help_texts = {
        "dc_help_kanal": (
            "🏗 *Kanal-Befehle*\n"
            "`/discord kanal [name] [text|voice] [kat?]` — Kanal erstellen\n"
            "`/discord kanal_del [name]` — Kanal löschen\n"
            "`/discord kategorie [name]` — Kategorie erstellen"
        ),
        "dc_help_rolle": (
            "🎭 *Rollen-Befehle*\n"
            "`/discord rolle [name] [#farbe?]` — Rolle erstellen\n"
            "`/discord rolle_del [name]` — Rolle löschen\n"
            "`/discord rolle_geben [user] [rolle]` — Rolle vergeben\n"
            "`/discord rolle_nehmen [user] [rolle]` — Rolle entziehen"
        ),
        "dc_help_send": (
            "💬 *Nachrichten-Befehle*\n"
            "`/discord send [#kanal] [text]` — Nachricht senden\n"
            "`/discord embed [#kanal] [titel] | [text]` — Embed senden\n"
            "`/discord pin [#kanal] [msg_id]` — Nachricht pinnen\n"
            "`/discord del [#kanal] [anzahl]` — Nachrichten löschen"
        ),
        "dc_help_user": (
            "👥 *User-Management*\n"
            "`/discord user [name]` — User-Info\n"
            "`/discord kick [user] [grund?]` — User kicken\n"
            "`/discord ban [user] [grund?]` — User bannen\n"
            "`/discord unban [user_id]` — User entbannen\n"
            "`/discord mute [user] [min]` — User muten"
        ),
    }

    # ── Zurück zum Menü ───────────────────────────────────────
    if data == "dc_back":
        intro = (
            f"🎮 *DISCORD MANAGER — {BOT_NAME}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📡 Bot-Kanal: `#{DISCORD_BOT_CHANNEL}`\n\n"
            "Wähle eine Kategorie:"
        )
        await query.edit_message_text(intro, parse_mode="Markdown", reply_markup=_build_main_menu())
        return

    # ── Bot-Kanal ─────────────────────────────────────────────
    if data == "dc_bot_channel":
        msg = (
            f"📡 *Bot-Kanal Info*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Aktiver Kanal: `#{DISCORD_BOT_CHANNEL}`\n"
            f"Bot-Name: `{BOT_NAME}`\n\n"
            f"_{BOT_NAME} hört in `#{DISCORD_BOT_CHANNEL}` auf alle Nachrichten._\n"
            f"_In anderen Kanälen nur bei Erwähnung `@{BOT_NAME}` oder per DM._\n\n"
            f"💡 *Kanal ändern:*\n"
            f"In der `.env` Datei:\n"
            f"`DISCORD_BOT_CHANNEL=neuer-kanal-name`\n"
            f"Dann Bot neu starten mit /restart"
        )
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=back_btn)
        return

    # ── Aktivität ─────────────────────────────────────────────
    if data == "dc_activity":
        msg = (
            "📊 *Aktivität anzeigen*\n"
            "`/discord activity` — heute\n"
            "`/discord activity gestern` — heute & gestern\n"
            "`/discord activity 3` — letzte 3 Tage\n"
            "`/discord activity alle` — letzte 7 Tage"
        )
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=back_btn)
        return

    # ── User-Profile ──────────────────────────────────────────
    if data == "dc_userinfo":
        msg = (
            "🧠 *User-Profile*\n"
            "`/discord userinfo` — Alle bekannten User\n"
            "`/discord userinfo [name|id]` — Profil eines Users"
        )
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=back_btn)
        return

    # ── Hilfe-Texte (Kanal, Rolle, Send, User) ────────────────
    if data in help_texts:
        await query.edit_message_text(help_texts[data], parse_mode="Markdown", reply_markup=back_btn)
        return

    # ── Direkte Befehle (Status, Server, Convlog, Invite, Vorstellen) ──
    # Diese schicken eine neue Nachricht — Menü-Nachricht bleibt bestehen
    class FakeUpdate:
        message = query.message

    direct_map = {
        "dc_status":    (_cmd_status,    []),
        "dc_server":    (_cmd_server,    []),
        "dc_convlog":   (_cmd_convlog,   []),
        "dc_invite":    (_cmd_invite,    []),
        "dc_vorstellen":(_cmd_vorstellen,[]),
    }
    if data in direct_map:
        fn, fn_args = direct_map[data]
        await fn(FakeUpdate(), context, fn_args)


# ── Metadaten ─────────────────────────────────────────────────
discord_command.description = "Discord Server komplett managen"
discord_command.category    = "Discord"


# ══════════════════════════════════════════════════════════════
# STATUS (für proactive_brain)
# ══════════════════════════════════════════════════════════════
async def get_status():
    if not DISCORD_AVAILABLE or not DISCORD_TOKEN:
        return "Discord: nicht konfiguriert"
    bot = get_discord_bot()
    if not bot.ready:
        return "Discord: Bot startet..."
    guilds = len(bot.bot.guilds)
    latenz = round(bot.bot.latency * 1000)
    return f"Discord: Online | {guilds} Server | {latenz}ms"


# ══════════════════════════════════════════════════════════════
# SETUP
# ══════════════════════════════════════════════════════════════
def setup(app):
    if DISCORD_AVAILABLE and DISCORD_TOKEN:
        get_discord_bot()
        log.info("🎮 Discord Manager geladen")
    else:
        if not DISCORD_AVAILABLE:
            log.warning("⚠️ discord.py fehlt — pip install discord.py")
        if not DISCORD_TOKEN:
            log.warning("⚠️ DISCORD_BOT_TOKEN fehlt in .env")

    app.add_handler(CommandHandler("discord", discord_command))
    app.add_handler(CallbackQueryHandler(discord_callback, pattern="^dc_"))