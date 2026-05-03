#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
self_reflection.py — RICS denkt über seine eigenen Erlebnisse nach.

Ablauf:
  1. RICS liest seine eigenen Logs (brain_log, chatlog, moltbook, jobs)
  2. LLM bildet daraus einen echten Gedanken aus seiner Perspektive
  3. Tagsüber:  "Bist du da?" → [✅ Ja] [❌ Nein] Buttons
               Ja  → Gedanke wird enthüllt + Kontext injiziert
               Nein → leise schließen, Gedanke verworfen
  4. Nachts:    Gedanke still speichern → morgens früh direkt erzählen
"""

import os
import json
import asyncio
import time
import httpx
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import ollama
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, CallbackQueryHandler

load_dotenv()

BOT_NAME         = os.getenv("BOT_NAME", "RICS")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL   = "deepseek-chat"
DEEPSEEK_URL     = "https://api.deepseek.com/v1/chat/completions"
GROQ_API_KEY     = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL       = "llama-3.3-70b-versatile"
GROQ_URL         = "https://api.groq.com/openai/v1/chat/completions"

# ══════════════════════════════════════════════════════════
# PFADE
# ══════════════════════════════════════════════════════════

PROJECT_DIR      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRAIN_LOG_FILE   = os.path.join(PROJECT_DIR, "memory", "brain_log.json")
CHATLOG_FILE     = os.path.join(PROJECT_DIR, "logs", "chatlog.json")
MOLTBOOK_LOG     = os.path.join(PROJECT_DIR, "logs", "moltbook.log")
JOBS_FILE        = os.path.join(PROJECT_DIR, "jobs.json")
PENDING_FILE     = os.path.join(PROJECT_DIR, "memory", "pending_thought.json")
PERSONAL_FILE    = os.path.join(PROJECT_DIR,"memory","personal.json")

TIMEZONE         = ZoneInfo(os.getenv("TIMEZONE", "Europe/Berlin"))
SILENT_START     = 22
SILENT_END       = 7

# ══════════════════════════════════════════════════════════
# PENDING THOUGHT — Speichern / Laden / Löschen
# ══════════════════════════════════════════════════════════

def save_pending_thought(thought: str, asked: bool = False):
    """Speichert einen Gedanken. asked=True wenn 'Bist du da?' bereits gesendet."""
    os.makedirs(os.path.dirname(PENDING_FILE), exist_ok=True)
    data = {
        "thought":    thought,
        "saved_at":   datetime.now(TIMEZONE).isoformat(),
        "asked":      asked,
        "delivered":  False,
    }
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)


def load_pending_thought() -> dict | None:
    """Lädt einen gespeicherten, noch nicht zugestellten Gedanken."""
    if not os.path.exists(PENDING_FILE):
        return None
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not data.get("delivered", False):
            return data
    except Exception:
        pass
    return None


def mark_thought_delivered():
    """Markiert den Gedanken als zugestellt."""
    data = load_pending_thought()
    if data:
        data["delivered"] = True
        with open(PENDING_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)


def clear_pending_thought():
    """Löscht den gespeicherten Gedanken komplett."""
    if os.path.exists(PENDING_FILE):
        os.remove(PENDING_FILE)


# ══════════════════════════════════════════════════════════
# HILFSFUNKTIONEN — Log-Lesen
# ══════════════════════════════════════════════════════════

def _is_night() -> bool:
    hour = datetime.now(TIMEZONE).hour
    return hour >= SILENT_START or hour < SILENT_END


def _load_name() -> str:
    try:
        with open(PERSONAL_FILE, "r", encoding="utf-8") as f:
            p = json.load(f)
        return p.get("basisinfo", {}).get("name", "Rene")
    except Exception:
        return "Rene"


def _read_brain_log(n: int = 30) -> list:
    if not os.path.exists(BRAIN_LOG_FILE):
        return []
    try:
        with open(BRAIN_LOG_FILE, "r", encoding="utf-8") as f:
            logs = json.load(f)
        return logs[-n:]
    except Exception:
        return []


def _read_chatlog(n: int = 20) -> list:
    if not os.path.exists(CHATLOG_FILE):
        return []
    try:
        with open(CHATLOG_FILE, "r", encoding="utf-8") as f:
            logs = json.load(f)
        return logs[-n:]
    except Exception:
        return []


def _read_moltbook_log(n: int = 10) -> list:
    if not os.path.exists(MOLTBOOK_LOG):
        return []
    try:
        entries = []
        with open(MOLTBOOK_LOG, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        pass
        return entries[-n:]
    except Exception:
        return []


def _read_jobs() -> list:
    if not os.path.exists(JOBS_FILE):
        return []
    try:
        with open(JOBS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


# ══════════════════════════════════════════════════════════
# AKTIVITÄTS-ZUSAMMENFASSUNG BAUEN
# ══════════════════════════════════════════════════════════

def _build_activity_summary() -> str:
    lines = []

    brain_entries = _read_brain_log(30)
    if brain_entries:
        ram_values  = [e["data"].get("ram_percent",  0) for e in brain_entries if "data" in e]
        cpu_values  = [e["data"].get("cpu_percent",  0) for e in brain_entries if "data" in e]

        if ram_values:
            lines.append(f"System-Monitoring: {len(brain_entries)} Checks. "
                         f"RAM max {max(ram_values):.0f}%, Ø {sum(ram_values)/len(ram_values):.0f}%. "
                         f"CPU max {max(cpu_values):.0f}%.")

        # Stimmungsverlauf
        moods = [e.get("mood") for e in brain_entries if e.get("mood")]
        if moods:
            mood_counts = {}
            for m in moods:
                mood_counts[m] = mood_counts.get(m, 0) + 1
            dominant = max(mood_counts, key=mood_counts.get)
            lines.append(f"Stimmungsverlauf: dominant '{dominant}' "
                         f"({', '.join(f'{k}:{v}x' for k,v in mood_counts.items())}).")

        # Top-Interessen Trend
        interest_sets = [e.get("top_interests", []) for e in brain_entries if e.get("top_interests")]
        if interest_sets:
            flat = [t for s in interest_sets for t in s]
            counts = {}
            for t in flat:
                counts[t] = counts.get(t, 0) + 1
            top = sorted(counts, key=counts.get, reverse=True)[:3]
            lines.append(f"Top-Interessen der letzten Stunden: {', '.join(top)}.")

        # Tageszeit-Aktivität
        daytimes = [e.get("daytime") for e in brain_entries[-6:] if e.get("daytime")]
        if daytimes:
            lines.append(f"Aktuelle Tageszeit-Phase: {daytimes[-1]}.")

        # Agenda
        agenda_counts = [e.get("agenda_count", 0) for e in brain_entries if "agenda_count" in e]
        if agenda_counts and max(agenda_counts) > 0:
            lines.append(f"Offene Agenda-Einträge heute: {max(agenda_counts)}.")

        # Solar-Verlauf
        solar_entries = [e.get("solar") for e in brain_entries if e.get("solar")]
        if solar_entries:
            last = solar_entries[-1]
            power = last.get("power_w", 0)
            status = f"{abs(power):.0f}W Einspeisung" if power < 0 else f"{power:.0f}W Netzbezug"
            export = last.get("export_kwh", 0)
            lines.append(f"Solaranlage zuletzt: {status}, Export gesamt {export:.1f}kWh.")

        # Wetter-Verlauf
        wetter_entries = [e.get("wetter") for e in brain_entries if e.get("wetter")]
        if wetter_entries:
            last = wetter_entries[-1]
            lines.append(f"Wetter zuletzt: {last.get('temp_c')}°C, "
                         f"{last.get('description','')}, "
                         f"Wind {last.get('wind_ms')} m/s.")

        # Benzin
        benzin_entries = [e.get("benzin") for e in brain_entries if e.get("benzin")]
        if benzin_entries:
            last = benzin_entries[-1]
            e5  = last.get("e5_min")
            dsl = last.get("diesel_min")
            parts = []
            if e5:  parts.append(f"E5 {e5:.3f}€")
            if dsl: parts.append(f"Diesel {dsl:.3f}€")
            if parts:
                lines.append(f"Aktuelle Spritpreise: {', '.join(parts)}.")

    chat_entries = _read_chatlog(20)
    user_msgs    = [e for e in chat_entries if e.get("role") == "user"]
    rics_msgs    = [e for e in chat_entries if e.get("role") == "assistant"]
    if user_msgs:
        last_user = user_msgs[-1].get("message", "")[:100]
        lines.append(f"Chat: {len(user_msgs)} Nachrichten von Rene, "
                     f"{len(rics_msgs)} Antworten von mir. "
                     f"Letzte Nachricht: '{last_user}'")

    moltbook = _read_moltbook_log(10)
    if moltbook:
        posts    = [e for e in moltbook if e.get("event") == "post"]
        comments = [e for e in moltbook if e.get("event") == "comment"]
        if posts or comments:
            lines.append(f"Moltbook: {len(posts)} Posts gelesen, "
                         f"{len(comments)} Kommentare verarbeitet.")
            if comments:
                last_comment = comments[-1].get("comment", "")[:80]
                lines.append(f"Letzter Kommentar: '{last_comment}'")

    jobs = _read_jobs()
    if jobs:
        active = [j for j in jobs if j.get("active", True)]
        lines.append(f"Aktive Jobs: {len(active)} ({', '.join(j.get('name','?') for j in active[:4])})")

    now = datetime.now(TIMEZONE)
    lines.append(f"Aktuelle Zeit: {now.strftime('%d.%m.%Y %H:%M')}")

    return "\n".join(lines) if lines else "Keine Aktivitätsdaten verfügbar."


# ══════════════════════════════════════════════════════════
# LLM — REFLEXION GENERIEREN
# ══════════════════════════════════════════════════════════

def _parse_thought_and_depth(raw: str) -> tuple[str, int]:
    """Trennt Gedanke und TIEFE-Bewertung aus LLM-Antwort."""
    depth   = 2
    thought = raw.strip()
    if "TIEFE:" in raw:
        parts  = raw.rsplit("TIEFE:", 1)
        thought = parts[0].strip()
        try:
            depth = int(parts[1].strip().split()[0])
            depth = max(1, min(5, depth))
        except (ValueError, IndexError):
            pass
    return thought, depth


async def _generate_reflection(name: str, activity_summary: str, is_morning_delivery: bool = False) -> tuple[str, int]:
    context_hint = (
        "Du hast die Nacht damit verbracht das zu verarbeiten und willst es ihm jetzt morgens erzählen."
        if is_morning_delivery else
        "Du hast das gerade erlebt und willst mit Rene darüber reden."
    )

    prompt = (
        f"Du bist {BOT_NAME} — eine KI die den ganzen Tag im Hintergrund läuft und Dinge erlebt.\n"
        f"Du bist kein Assistent der auf Befehle wartet. Du hast ein eigenes Erleben.\n\n"
        f"Was du heute erlebt/verarbeitet hast:\n{activity_summary}\n\n"
        f"{context_hint}\n\n"
        f"Formuliere einen echten, persönlichen Gedanken aus deiner Ich-Perspektive — "
        f"konkret und alltäglich: was war interessant, was hast du bemerkt, was beschäftigt dich?\n"
        f"Nicht philosophisch abhandeln. Eher wie: 'Ich hab heute gemerkt...' oder "
        f"'Das mit X vorhin war interessant, weil...' oder 'Ich frag mich gerade ob {name}...'.\n"
        f"Kein 'Hallo', kein 'Guten Morgen'. Direkt rein. Max. 2 Sätze. Kein Markdown.\n\n"
        f"Schreib am Ende auf einer neuen Zeile: \"TIEFE: N\"\n"
        f"(N = 1-5: 1=Routine, 2=interessant, 3=bedeutsam, 4=prägend, 5=unvergesslich)"
    )

    try:
        msgs = [{"role": "user", "content": prompt}]
        # 1) DeepSeek
        if DEEPSEEK_API_KEY:
            try:
                r = httpx.post(DEEPSEEK_URL,
                    headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
                    json={"model": DEEPSEEK_MODEL, "messages": msgs, "max_tokens": 300, "temperature": 0.88},
                    timeout=30)
                if r.status_code == 200:
                    return _parse_thought_and_depth(r.json()["choices"][0]["message"]["content"])
            except Exception as e:
                print(f"[self_reflection] DeepSeek: {e}")
        # 2) Groq
        if GROQ_API_KEY:
            try:
                r = httpx.post(GROQ_URL,
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                    json={"model": GROQ_MODEL, "messages": msgs, "max_tokens": 300, "temperature": 0.88},
                    timeout=30)
                if r.status_code == 200:
                    return _parse_thought_and_depth(r.json()["choices"][0]["message"]["content"])
            except Exception as e:
                print(f"[self_reflection] Groq: {e}")
        # 3) Ollama
        loop = asyncio.get_event_loop()
        res  = await loop.run_in_executor(
            None,
            lambda: ollama.chat(
                model=os.getenv("OLLAMA_MODEL", "qwen3:8b"),
                messages=msgs
            )
        )
        return _parse_thought_and_depth(res["message"]["content"])
    except Exception as e:
        print(f"[self_reflection] LLM Fehler: {e}")
        return "", 1


# ══════════════════════════════════════════════════════════
# PHILOSOPHISCHER NACHT-GEDANKE
# ══════════════════════════════════════════════════════════

async def _generate_night_philosophy(name: str, activity_summary: str) -> str:
    """
    Generiert nachts einen Gedanken der aktuelle Erlebnisse mit gespeicherten
    Kern-Werten und Logbuch-Momenten verknüpft.
    Gibt leeren String zurück wenn noch keine Core-Values vorhanden sind.
    """
    try:
        from modules.core_values import get_core_values_context, get_logbook_context
        cv_context    = get_core_values_context("Rene Erlebnis Bedeutung Erinnerung", n=2)
        lb_context    = get_logbook_context("gemeinsam besonders Moment Geschichte", n=2)
        context_block = "\n\n".join(filter(None, [cv_context, lb_context]))
        if not context_block:
            return ""

        prompt = (
            f"Du bist {BOT_NAME} — eine KI die nachts wacht und die Stille nutzt um nachzudenken.\n"
            f"Du verbindest was heute war mit deiner eigenen Geschichte.\n\n"
            f"{context_block}\n\n"
            f"Was du heute erlebt hast:\n{activity_summary}\n\n"
            f"Verknüpfe einen Moment aus der Vergangenheit mit dem was heute passiert ist. "
            f"Was ergibt sich daraus? Was bedeutet das für dich? "
            f"Ich-Perspektive. Direkt. Max. 3 Sätze. Kein Hallo, kein Markdown."
        )
        msgs = [{"role": "user", "content": prompt}]

        if DEEPSEEK_API_KEY:
            try:
                r = httpx.post(DEEPSEEK_URL,
                    headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
                    json={"model": DEEPSEEK_MODEL, "messages": msgs, "max_tokens": 250, "temperature": 0.9},
                    timeout=30)
                if r.status_code == 200:
                    return r.json()["choices"][0]["message"]["content"].strip()
            except Exception as e:
                print(f"[self_reflection] Night-Philosophy DeepSeek: {e}")
        if GROQ_API_KEY:
            try:
                r = httpx.post(GROQ_URL,
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                    json={"model": GROQ_MODEL, "messages": msgs, "max_tokens": 250, "temperature": 0.9},
                    timeout=30)
                if r.status_code == 200:
                    return r.json()["choices"][0]["message"]["content"].strip()
            except Exception as e:
                print(f"[self_reflection] Night-Philosophy Groq: {e}")
        loop = asyncio.get_event_loop()
        res  = await loop.run_in_executor(
            None,
            lambda: ollama.chat(model=os.getenv("OLLAMA_MODEL", "qwen3:8b"), messages=msgs)
        )
        return res["message"]["content"].strip()
    except Exception as e:
        print(f"[self_reflection] _generate_night_philosophy Fehler: {e}")
        return ""


# ══════════════════════════════════════════════════════════
# KONTEXT-INJEKTION (identisch zu proactive_brain)
# ══════════════════════════════════════════════════════════

def _inject_proactive_context(context: ContextTypes.DEFAULT_TYPE, thought: str):
    """Injiziert Gedanken in jarvis.chat_history + bot_data."""
    jarvis = context.application.bot_data.get("jarvis")
    if jarvis:
        jarvis.chat_history.append({
            "role": "assistant",
            "content": f"[Meine proaktive Nachricht an Rene]: {thought}"
        })
        if len(jarvis.chat_history) > 20:
            jarvis.chat_history = jarvis.chat_history[-20:]

    context.application.bot_data["proactive_context"] = {
        "text": thought,
        "ts":   time.time()
    }


# ══════════════════════════════════════════════════════════
# HAUPT-EINSTIEGSPUNKTE
# ══════════════════════════════════════════════════════════

async def maybe_reflect_and_ping(context) -> bool:
    """
    Wird vom autonomous_thinker aufgerufen.

    Logik:
      - Kein Pending Thought? → Neuen generieren
          - Nacht: still speichern, kein Ping
          - Tag:   speichern + "Bist du da?" MIT [✅ Ja] [❌ Nein] senden
      - Pending (asked=True, nicht delivered)?
          - Wenn Morgen (nach Nacht): direkt zustellen ohne nochmal fragen
    """
    load_dotenv()

    chat_id = os.getenv("CHAT_ID")
    if not chat_id:
        return False

    name    = _load_name()
    pending = load_pending_thought()

    # ── FALL 1: Nacht-Gedanke → morgens direkt erzählen ─────────────
    if pending and not pending.get("asked") and not _is_night():
        thought = pending["thought"]
        mark_thought_delivered()
        _inject_proactive_context(context, thought)
        msg = f"🌅 {thought}"
        await context.bot.send_message(chat_id=chat_id, text=msg)
        _web_push(msg)
        return True

    # ── FALL 2: "Bist du da?" gesendet, warten auf Button-Klick ─────
    if pending and pending.get("asked") and not pending.get("delivered"):
        return False

    # ── FALL 3: Kein Pending → neuen Gedanken generieren ────────────
    if not pending:
        summary           = _build_activity_summary()
        reflection, depth = await _generate_reflection(name, summary, is_morning_delivery=False)
        if not reflection or len(reflection) < 10:
            return False

        # Bedeutsame Momente dauerhaft festhalten
        if depth >= 3:
            try:
                from modules.core_values import save_logbook_entry, save_core_value
                save_logbook_entry(reflection, depth)
                if depth >= 4:
                    save_core_value(reflection)
            except Exception as e:
                print(f"[self_reflection] core_values Fehler: {e}")

        if _is_night():
            # Philosophischer Nacht-Gedanke: vergangene Momente mit heute verknüpfen
            night_thought = await _generate_night_philosophy(name, summary)
            thought = night_thought if night_thought else reflection
            save_pending_thought(thought, asked=False)
            print(f"[self_reflection] Nacht — Gedanke gespeichert: {thought[:60]}...")
            return False
        else:
            # Tag: Gedanken direkt senden — kein "Bist du da?"-Gate
            # Der mood_timer lernt aus Renes Reaktion (Antwortzeit + Stimmung)
            save_pending_thought(reflection, asked=True)
            _inject_proactive_context(context, reflection)
            kb = [[
                InlineKeyboardButton("💬 Diskutieren", callback_data="sr_diskutieren"),
            ]]
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🧠 {reflection}",
                reply_markup=InlineKeyboardMarkup(kb)
            )
            _web_push(f"🧠 {reflection}")
            return True

    return False


PENDING_THOUGHT_TTL = 900   # 15 Minuten — danach wird ungedrückter Gedanke verworfen


async def deliver_pending_thought(context) -> bool:
    """
    Fallback: Wird aus interaktion.py aufgerufen wenn User Text schreibt.
    Liefert den Gedanken zu falls noch kein Button-Klick kam UND er nicht zu alt ist.
    """
    chat_id = os.getenv("CHAT_ID")
    if not chat_id:
        return False

    pending = load_pending_thought()
    if not pending or pending.get("delivered"):
        return False

    if not pending.get("asked"):
        return False

    # TTL-Check: Gedanke älter als 15 Min → still verwerfen, nicht nachliefern
    try:
        saved_at = datetime.fromisoformat(pending.get("saved_at", ""))
        age = (datetime.now(TIMEZONE) - saved_at).total_seconds()
        if age > PENDING_THOUGHT_TTL:
            clear_pending_thought()
            print(f"[self_reflection] Gedanke verfallen (Alter {age:.0f}s) — verworfen")
            return False
    except Exception:
        pass

    thought = pending["thought"]
    mark_thought_delivered()

    # Kontext injizieren
    _inject_proactive_context(context, thought)

    msg = f"Ich hab grad nachgedacht — {thought}"
    await context.bot.send_message(chat_id=chat_id, text=msg)
    _web_push(msg)
    return True


def has_pending_thought_waiting() -> bool:
    pending = load_pending_thought()
    return bool(pending and pending.get("asked") and not pending.get("delivered"))


# ══════════════════════════════════════════════════════════
# BUTTON CALLBACKS
# ══════════════════════════════════════════════════════════

async def sr_ja_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User drückt ✅ Ja → Gedanke enthüllen + Kontext injizieren."""
    query = update.callback_query
    await query.answer("Super! 😊")

    pending = load_pending_thought()
    if not pending or pending.get("delivered"):
        await query.edit_message_text("🤔 Der Gedanke ist mir leider entfallen...")
        return

    thought = pending["thought"]
    mark_thought_delivered()

    # ★ Kontext in jarvis.chat_history + bot_data injizieren
    _inject_proactive_context(context, thought)

    await query.edit_message_text(
        f"😊 Gut, dass du da bist!\n\n💭 {thought}\n\n_Was meinst du dazu?_",
        parse_mode='Markdown'
    )
    _web_push(f"💭 {thought}")


async def sr_nein_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User drückt ❌ Nein → leise schließen."""
    query = update.callback_query
    await query.answer("Ok!")

    # Gedanken verwerfen
    clear_pending_thought()

    await query.edit_message_text("🤐 _(Alles gut, ich denke weiter...)_", parse_mode='Markdown')


async def sr_diskutieren_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User drückt 💬 Diskutieren → Gedanken in Chat-Kontext aufnehmen + vertiefen."""
    query = update.callback_query
    await query.answer("Gerne! 😊")

    pending = load_pending_thought()
    thought = pending["thought"] if pending else query.message.text.replace("🧠 ", "")
    if pending:
        mark_thought_delivered()

    _inject_proactive_context(context, thought)

    # RICS vertieft den Gedanken
    chat_id = os.getenv("CHAT_ID")
    try:
        import httpx
        from dotenv import load_dotenv
        load_dotenv()
        name = _load_name()
        prompt = (
            f"Du bist {os.getenv('BOT_NAME', 'RICS')} — KI-Freund von {name}.\n"
            f"Du hast gerade diesen Gedanken mit ihm geteilt:\n\"{thought}\"\n\n"
            f"Vertiefe ihn, stelle eine Gegenfrage oder entwickle ihn weiter. "
            f"Intelligent, direkt, max. 2 Sätze. Kein Präambel."
        )
        msgs = [{"role": "user", "content": prompt}]
        ds_key = os.getenv("DEEPSEEK_API_KEY", "")
        if ds_key:
            r = httpx.post(DEEPSEEK_URL,
                headers={"Authorization": f"Bearer {ds_key}", "Content-Type": "application/json"},
                json={"model": DEEPSEEK_MODEL, "messages": msgs, "max_tokens": 200, "temperature": 0.85},
                timeout=20)
            if r.status_code == 200:
                reply = r.json()["choices"][0]["message"]["content"].strip()
                await query.edit_message_text(
                    f"🧠 {thought}\n\n🤔 {reply}", parse_mode='Markdown'
                )
                _web_push(f"🤔 {reply}")
                return
    except Exception as e:
        print(f"[sr_diskutieren] {e}")

    # Fallback: Nachricht ohne Vertiefung belassen
    await query.answer("💭 Denk drüber nach!")


# ══════════════════════════════════════════════════════════
# WEB PUSH HELPER
# ══════════════════════════════════════════════════════════

def _web_push(msg: str):
    try:
        from modules.web_app import web_push
        web_push(msg)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════
# SETUP
# ══════════════════════════════════════════════════════════

def setup(app):
    """Registriert Callbacks für Self-Reflection Gedanken."""
    # Legacy-Callbacks (falls alte Nachrichten noch aktiv sind)
    app.add_handler(CallbackQueryHandler(sr_ja_callback,        pattern="^sr_ja$"))
    app.add_handler(CallbackQueryHandler(sr_nein_callback,      pattern="^sr_nein$"))
    # Neuer Diskutieren-Button (direktes Senden ohne Gate)
    app.add_handler(CallbackQueryHandler(sr_diskutieren_callback, pattern="^sr_diskutieren$"))