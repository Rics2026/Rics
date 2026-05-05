"""
behavior_engine.py — Dynamisches Verhaltens-Lern-System für RICS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Lernt aus jedem Austausch Verhaltensmuster und passt RICS
dynamisch an den Nutzer an. Nicht nur Fakten — sondern Stil,
Tiefe, Ton, Tempo. Jede Instanz entwickelt sich anders.

Dimensionen:
  laenge       — Antwortlänge: kürzer / länger
  tiefe        — Technische Tiefe: mehr / weniger
  quellen      — Quellenangaben erwünscht
  praezision   — Präzision über Geschwindigkeit
  ton          — Formell vs. Locker
  emoji        — Emoji-Dichte anpassen
  themen       — Bevorzugte Themen/Reaktionen

Befehle:
  /verhalten        — aktives Profil anzeigen
  /verhalten_reset  — alle Muster löschen
  /verhalten_debug  — Detail-Dump inkl. Schwache Muster
"""

import os
import json
import re
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

load_dotenv()

BOT_NAME  = os.getenv("BOT_NAME",  "RICS")
TIMEZONE  = ZoneInfo(os.getenv("TIMEZONE", "Europe/Berlin"))
BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "..", "memory", "behavior_patterns.json")

# ═══════════════════════════════════════════════════════════════════
# SIGNAL-DEFINITIONEN
# ═══════════════════════════════════════════════════════════════════

# Direkte Feedback-Signale (hohe Konfidenz-Wirkung)
DIRECT_SIGNALS: list[dict] = [

    # ── Länge ──────────────────────────────────────────────────────
    {
        "dimension": "laenge",
        "richtung":  "kuerzer",
        "keywords":  ["kürzer", "kuerzer", "zu lang", "weniger text", "kurz bitte",
                      "kompakter", "zu viel text", "kürzer fassen", "kurzfassung",
                      "fass dich kürzer", "nicht so viel"],
        "delta":     +0.18,
    },
    {
        "dimension": "laenge",
        "richtung":  "laenger",
        "keywords":  ["ausführlicher", "mehr detail", "erkläre genauer", "mehr erklären",
                      "geh tiefer", "detaillierter", "zu kurz", "mehr dazu", "erzähl mehr"],
        "delta":     +0.18,
    },

    # ── Technische Tiefe ───────────────────────────────────────────
    {
        "dimension": "tiefe",
        "richtung":  "mehr",
        "keywords":  ["wie genau", "wie funktioniert das", "technisch", "geh tiefer",
                      "hintergrund", "technisch erklären", "warum genau", "mechanismus"],
        "delta":     +0.12,
    },
    {
        "dimension": "tiefe",
        "richtung":  "weniger",
        "keywords":  ["einfacher", "vereinfachen", "einfach erklärt", "nicht so technisch",
                      "layman", "für anfänger", "verständlicher"],
        "delta":     +0.12,
    },

    # ── Quellen ────────────────────────────────────────────────────
    {
        "dimension": "quellen",
        "richtung":  "an",
        "keywords":  ["quelle", "woher weißt du", "beleg", "link", "quelle?", "woher",
                      "ist das sicher", "belege das", "quelle bitte", "quellen"],
        "delta":     +0.20,
    },

    # ── Präzision ──────────────────────────────────────────────────
    {
        "dimension": "praezision",
        "richtung":  "hoch",
        "keywords":  ["stimmt nicht", "falsch", "das ist nicht richtig", "nein das ist",
                      "korrigiere", "das stimmt so nicht", "nicht korrekt", "ungenau"],
        "delta":     +0.15,
    },

    # ── Ton ────────────────────────────────────────────────────────
    {
        "dimension": "ton",
        "richtung":  "lockerer",
        "keywords":  ["locker", "entspannter", "nicht so steif", "leg dich hin",
                      "chill", "relaxed", "nicht so förmlich"],
        "delta":     +0.10,
    },
    {
        "dimension": "ton",
        "richtung":  "formeller",
        "keywords":  ["formeller", "professioneller", "sachlicher", "weniger witze",
                      "seriöser", "kein humor gerade"],
        "delta":     +0.10,
    },

    # ── Positives Feedback (verstärkt aktuelle Muster) ─────────────
    {
        "dimension": "_positive",
        "richtung":  "reinforce",
        "keywords":  ["perfekt", "genau so", "super", "danke genau", "das ist gut",
                      "top", "so lass das", "weiter so", "passt perfekt",
                      "gefällt mir", "gut gemacht"],
        "delta":     +0.05,   # wirkt auf ALLE aktiven Muster
    },
]

# Implizite Signale (aus Chat-Verhalten abgeleitet, niedrigere Wirkung)
IMPLICIT_CHECKS: list[dict] = [
    {
        "dimension": "laenge",
        "richtung":  "kuerzer",
        "check":     "user_msg_short",        # User schreibt selbst kurz → kurze Antworten bevorzugt
        "delta":     +0.03,
    },
    {
        "dimension": "emoji",
        "richtung":  "viel",
        "check":     "user_uses_emojis",      # User nutzt Emojis → Emoji-freundlich OK
        "delta":     +0.04,
    },
    {
        "dimension": "laenge",
        "richtung":  "laenger",
        "check":     "user_msg_long",         # User schreibt selbst ausführlich → mag Details
        "delta":     +0.02,
    },
]

# Injection-Texte für den System-Prompt (pro Dimension + Richtung)
RULE_TEMPLATES: dict[str, dict] = {
    "laenge.kuerzer":   "⚡ Antworte KURZ und kompakt — kein Padding, kein Filler, direkt auf den Punkt.",
    "laenge.laenger":   "📖 Antworte AUSFÜHRLICH — User schätzt Detail und Tiefe.",
    "tiefe.mehr":       "🔧 Technische Tiefe erwünscht — geh gerne ins Detail, erkläre Mechanismen.",
    "tiefe.weniger":    "🧩 Einfache Sprache bevorzugt — keine Fachbegriffe ohne Erklärung.",
    "quellen.an":       "📚 Füge bei Fakten, Zahlen und Behauptungen immer die Quelle/Herkunft hinzu.",
    "praezision.hoch":  "🎯 PRÄZISION hat Priorität — lieber zugeben was unsicher ist als raten.",
    "ton.lockerer":     "😄 Lockerer Umgangston bevorzugt — Humor und Sarkasmus willkommen.",
    "ton.formeller":    "💼 Sachlicher, formeller Ton bevorzugt — weniger Witze.",
    "emoji.viel":       "✨ Emojis im gleichen Stil wie der User sind OK.",
}

# Welche Dimensionen schließen sich gegenseitig aus
CONFLICTS: list[tuple] = [
    ("laenge.kuerzer",  "laenge.laenger"),
    ("tiefe.mehr",      "tiefe.weniger"),
    ("ton.lockerer",    "ton.formeller"),
]

# Ab dieser Konfidenz wird eine Regel aktiv injiziert
ACTIVE_THRESHOLD  = 0.40
# Unter dieser Konfidenz wird die Regel gelöscht
PURGE_THRESHOLD   = 0.12
# Stündlicher Decay wenn keine Bestätigung
DECAY_PER_HOUR    = 0.005
# Max Konfidenz
MAX_CONF          = 0.97


# ═══════════════════════════════════════════════════════════════════
# CORE ENGINE
# ═══════════════════════════════════════════════════════════════════

_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(TIMEZONE)


def _load() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Migration: ältere Dateien ohne manual_rules
            if "manual_rules" not in data:
                data["manual_rules"] = []
            return data
        except Exception:
            pass
    return {
        "patterns":     {},   # key = "dimension.richtung"
        "manual_rules": [],   # explizite Direktanweisungen, kein Decay
        "meta": {
            "total_exchanges": 0,
            "created": _now().isoformat(),
            "last_analysis": None,
        }
    }


def _save(state: dict):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _tageszeit(dt: datetime) -> str:
    h = dt.hour
    if 6 <= h < 10:   return "morgens"
    if 10 <= h < 14:  return "mittags"
    if 14 <= h < 18:  return "nachmittags"
    if 18 <= h < 22:  return "abends"
    return "nachts"


def _count_emojis(text: str) -> int:
    # Emoji-Erkennung via Unicode-Kategorien (kein extra-Paket nötig)
    import unicodedata
    return sum(1 for ch in text if unicodedata.category(ch) in ("So", "Sm") or ord(ch) > 0x1F300)


def _apply_decay(state: dict, now: datetime) -> dict:
    """Reduziert Konfidenz basierend auf Zeit seit letzter Aktivierung."""
    to_purge = []
    for key, pattern in state["patterns"].items():
        try:
            last = datetime.fromisoformat(pattern["last_signal"]).astimezone(TIMEZONE)
            hours_elapsed = (now - last).total_seconds() / 3600
            decay = DECAY_PER_HOUR * hours_elapsed
            pattern["confidence"] = max(0.0, pattern["confidence"] - decay)
            if pattern["confidence"] < PURGE_THRESHOLD:
                to_purge.append(key)
        except Exception:
            pass
    for k in to_purge:
        del state["patterns"][k]
    return state


def _resolve_conflicts(state: dict, changed_key: str) -> dict:
    """Schwächt konfliktierende Muster wenn ein neues gestärkt wird."""
    for (a, b) in CONFLICTS:
        if changed_key == a and b in state["patterns"]:
            state["patterns"][b]["confidence"] *= 0.6
        elif changed_key == b and a in state["patterns"]:
            state["patterns"][a]["confidence"] *= 0.6
    return state


def _bump(state: dict, key: str, delta: float, kontext: str) -> dict:
    """Erhöht Konfidenz eines Musters, legt es ggf. an."""
    now = _now()
    if key not in state["patterns"]:
        dim, richt = key.split(".", 1)
        state["patterns"][key] = {
            "dimension":    dim,
            "richtung":     richt,
            "rule":         RULE_TEMPLATES.get(key, key),
            "confidence":   0.0,
            "signal_count": 0,
            "kontext":      kontext,
            "first_signal": now.isoformat(),
            "last_signal":  now.isoformat(),
        }
    p = state["patterns"][key]
    p["confidence"]   = min(MAX_CONF, p["confidence"] + delta)
    p["signal_count"] = p.get("signal_count", 0) + 1
    p["last_signal"]  = now.isoformat()
    p["kontext"]      = kontext  # aktualisieren: zuletzt in welchem Kontext ausgelöst
    state = _resolve_conflicts(state, key)
    return state


# ═══════════════════════════════════════════════════════════════════
# ÖFFENTLICHE API
# ═══════════════════════════════════════════════════════════════════

def analyze_exchange(user_msg: str, bot_response: str):
    """
    Wird nach JEDEM Austausch aufgerufen.
    user_msg:     was der User geschrieben hat
    bot_response: was RICS geantwortet hat
    """
    with _lock:
        state  = _load()
        now    = _now()
        kontext = _tageszeit(now)

        state  = _apply_decay(state, now)
        state["meta"]["total_exchanges"] = state["meta"].get("total_exchanges", 0) + 1
        state["meta"]["last_analysis"]   = now.isoformat()

        user_lower = user_msg.lower()
        active_keys = [k for k, p in state["patterns"].items()
                       if p["confidence"] >= ACTIVE_THRESHOLD]

        # ── 1. Direkte Signale scannen ──────────────────────────────
        positive_fire = False
        for sig in DIRECT_SIGNALS:
            if sig["dimension"] == "_positive":
                if any(kw in user_lower for kw in sig["keywords"]):
                    positive_fire = True
                continue
            if any(kw in user_lower for kw in sig["keywords"]):
                key = f"{sig['dimension']}.{sig['richtung']}"
                state = _bump(state, key, sig["delta"], kontext)

        # Positives Feedback → verstärkt alle aktiven Muster leicht
        if positive_fire:
            for k in active_keys:
                if k in state["patterns"]:
                    state["patterns"][k]["confidence"] = min(
                        MAX_CONF, state["patterns"][k]["confidence"] + 0.04
                    )

        # ── 2. Implizite Signale ────────────────────────────────────
        msg_word_count = len(user_msg.split())
        emoji_count    = _count_emojis(user_msg)

        for sig in IMPLICIT_CHECKS:
            fire = False
            if sig["check"] == "user_msg_short"  and msg_word_count <= 5:
                fire = True
            elif sig["check"] == "user_msg_long"  and msg_word_count >= 30:
                fire = True
            elif sig["check"] == "user_uses_emojis" and emoji_count >= 2:
                fire = True

            if fire:
                key = f"{sig['dimension']}.{sig['richtung']}"
                state = _bump(state, key, sig["delta"], kontext)

        _save(state)


def add_manual_rule(text: str) -> int:
    """Fügt eine explizite Direktregel hinzu. Gibt die neue ID zurück."""
    with _lock:
        state = _load()
        rules = state.setdefault("manual_rules", [])
        next_id = max((r["id"] for r in rules), default=0) + 1
        rules.append({
            "id":      next_id,
            "text":    text.strip(),
            "created": _now().isoformat(),
        })
        _save(state)
    return next_id


def remove_manual_rule(rule_id: int) -> bool:
    """Löscht eine Direktregel anhand ihrer ID. True wenn gefunden."""
    with _lock:
        state = _load()
        before = len(state.get("manual_rules", []))
        state["manual_rules"] = [r for r in state.get("manual_rules", []) if r["id"] != rule_id]
        found = len(state["manual_rules"]) < before
        if found:
            _save(state)
    return found


def list_manual_rules() -> list[dict]:
    """Gibt alle Direktregeln zurück."""
    with _lock:
        state = _load()
    return state.get("manual_rules", [])


# Phrasen die eindeutig eine Direktanweisung einleiten
_DIRECTIVE_TRIGGERS = [
    "in zukunft",
    "ab jetzt",
    "ab sofort",
    "von jetzt an",
    "denk daran",
    "ich will dass du",
    "ich möchte dass du",
    "du sollst",
    "du solltest immer",
    "mach das immer so",
    "bitte immer",
    "immer wenn du",
    "generell sollst du",
    "als regel",
    "neue regel",
]

# Diese Wörter am Anfang kürzen (Trigger selbst rausschneiden für saubere Regel)
_TRIGGER_STRIP = [
    "in zukunft ", "ab jetzt ", "ab sofort ", "von jetzt an ",
    "denk daran ", "ich will dass du ",
    "ich möchte dass du ", "du sollst ", "bitte ",
]


def detect_directive(user_msg: str) -> str | None:
    """
    Erkennt explizite Verhaltensanweisungen in einer User-Nachricht.
    Gibt den bereinigten Regeltext zurück, oder None wenn keine Direktive erkannt.
    """
    low = user_msg.lower().strip()

    # Trigger-Phrase erkannt?
    found_trigger = None
    for trigger in _DIRECTIVE_TRIGGERS:
        if trigger in low:
            found_trigger = trigger
            break
    if not found_trigger:
        return None

    # Regeltext = alles NACH dem Trigger-Ausdruck
    trigger_pos = low.find(found_trigger)
    rule_text = user_msg[trigger_pos + len(found_trigger):].strip()

    # Führende Füllwörter abschneiden (z.B. "du", "dass du", "bitte")
    for strip in _TRIGGER_STRIP:
        if rule_text.lower().startswith(strip.strip()):
            rule_text = rule_text[len(strip.strip()):].strip()
            break

    if len(rule_text) < 5:
        return None

    # Ersten Buchstaben groß
    rule_text = rule_text[0].upper() + rule_text[1:]

    # Duplikat-Schutz
    existing = list_manual_rules()
    for r in existing:
        existing_words = set(r["text"].lower().split())
        new_words      = set(rule_text.lower().split())
        if len(existing_words) > 0:
            overlap = len(existing_words & new_words) / len(existing_words)
            if overlap > 0.6:
                return None

    return rule_text


def detect_and_store_directive(user_msg: str) -> str | None:
    """
    Kombiniert Erkennung + Speicherung.
    Gibt den gespeicherten Regeltext zurück (für optionale Bestätigung), oder None.
    """
    rule_text = detect_directive(user_msg)
    if rule_text:
        add_manual_rule(rule_text)
        return rule_text
    return None


def get_behavior_section() -> str:
    """
    Gibt den fertigen System-Prompt-Abschnitt zurück.
    Wird von bot.py in den system_msg injiziert.
    Leer wenn keine aktiven Regeln und keine Direktregeln.
    """
    with _lock:
        state = _load()
        state = _apply_decay(state, _now())

    manual = state.get("manual_rules", [])
    active = [
        (k, p) for k, p in state["patterns"].items()
        if p["confidence"] >= ACTIVE_THRESHOLD
        and k in RULE_TEMPLATES
    ]

    if not manual and not active:
        return ""

    active.sort(key=lambda x: x[1]["confidence"], reverse=True)

    lines = ["\n### 🧠 VERHALTENS-PROFIL (gilt immer):"]

    # Direktregeln zuerst — höchste Priorität, kein Decay
    if manual:
        for r in manual:
            lines.append(f"🔒 {r['text']}")

    # Gelernte Muster danach
    if active:
        if manual:
            lines.append("─── gelernte Muster:")
        for key, p in active:
            pct = int(p["confidence"] * 100)
            lines.append(f"{p['rule']}  [{pct}%]")

    return "\n".join(lines) + "\n"


def get_profile_summary(include_weak: bool = False) -> str:
    """Für /verhalten und /verhalten_debug."""
    with _lock:
        state = _load()
        state = _apply_decay(state, _now())

    patterns = state["patterns"]
    manual   = state.get("manual_rules", [])
    meta     = state["meta"]

    lines = [f"🧠 *Verhaltens-Profil* ({meta.get('total_exchanges', 0)} Gespräche analysiert)\n"]
    has_content = False

    # ── Direktregeln ─────────────────────────────────────────────
    if manual:
        has_content = True
        lines.append("*🔒 Direktregeln (permanent, kein Decay):*")
        for r in manual:
            lines.append(f"  `#{r['id']}`  {r['text']}")
        lines.append("")

    # ── Gelernte Muster ──────────────────────────────────────────
    active  = [(k, p) for k, p in patterns.items() if p["confidence"] >= ACTIVE_THRESHOLD]
    pending = [(k, p) for k, p in patterns.items() if PURGE_THRESHOLD <= p["confidence"] < ACTIVE_THRESHOLD]
    active.sort(key=lambda x: x[1]["confidence"], reverse=True)

    if active:
        has_content = True
        lines.append("*✅ Gelernte Muster (aktiv):*")
        for k, p in active:
            bar = "█" * int(p["confidence"] * 10) + "░" * (10 - int(p["confidence"] * 10))
            lines.append(
                f"  {bar} {int(p['confidence']*100)}%  —  {p['rule']}\n"
                f"    ↳ {p.get('signal_count',0)}x ausgelöst | zuletzt: {p.get('kontext','?')}"
            )

    if include_weak and pending:
        lines.append("\n*⏳ Im Aufbau (noch nicht aktiv):*")
        for k, p in pending:
            lines.append(f"  {int(p['confidence']*100)}%  {k}  ({p.get('signal_count',0)}x)")

    if not has_content:
        return "📭 Noch keine Verhaltensmuster gelernt."

    lines.append(f"\n_Letzte Analyse: {meta.get('last_analysis', '—')}_")
    return "\n".join(lines)


def reset_patterns():
    """Löscht alle gelernten Muster."""
    with _lock:
        state = _load()
        state["patterns"] = {}
        state["meta"]["last_reset"] = _now().isoformat()
        _save(state)


# ═══════════════════════════════════════════════════════════════════
# TELEGRAM COMMANDS
# ═══════════════════════════════════════════════════════════════════

async def cmd_verhalten(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Zeigt das aktuelle Verhaltens-Profil."""
    text = get_profile_summary(include_weak=False)
    await update.message.reply_text(text, parse_mode="Markdown")

cmd_verhalten.description = "Zeigt das adaptiv gelernte Verhaltens-Profil"
cmd_verhalten.category    = "Gedächtnis"


async def cmd_verhalten_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Zeigt das vollständige Profil inkl. schwacher Muster."""
    text = get_profile_summary(include_weak=True)
    await update.message.reply_text(text, parse_mode="Markdown")

cmd_verhalten_debug.description = "Verhaltens-Profil mit schwachen Mustern (Debug)"
cmd_verhalten_debug.category    = "Gedächtnis"


async def cmd_verhalten_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Löscht alle gelernten Muster (Direktregeln bleiben!)."""
    reset_patterns()
    await update.message.reply_text(
        "🗑️ Gelernte Muster gelöscht. Direktregeln bleiben erhalten.\nMit /lern_regel_liste prüfen.",
    )

cmd_verhalten_reset.description = "Löscht gelernte Muster (Direktregeln bleiben)"
cmd_verhalten_reset.category    = "Gedächtnis"


async def cmd_lern_regel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /lern_regel <text>
    Speichert eine explizite Direktregel permanent im Verhaltens-Profil.
    """
    text = " ".join(context.args).strip() if context.args else ""
    if not text:
        await update.message.reply_text(
            "Verwendung: /lern_regel <Regel>\n"
            "Beispiel: /lern_regel Aufzählungen: jeden Punkt in eigene Code-Box"
        )
        return
    rule_id = add_manual_rule(text)
    await update.message.reply_text(f"✅ Direktregel #{rule_id} gespeichert:\n{text}")

cmd_lern_regel.description = "Speichert eine explizite Verhaltensregel permanent"
cmd_lern_regel.category    = "Gedächtnis"


async def cmd_lern_regel_liste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Zeigt alle Direktregeln."""
    rules = list_manual_rules()
    if not rules:
        await update.message.reply_text("📭 Keine Direktregeln gespeichert.")
        return
    lines = ["*🔒 Direktregeln:*"]
    for r in rules:
        lines.append(f"  `#{r['id']}`  {r['text']}")
    lines.append("\nMit /lern_regel_loeschen <nr> entfernen.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

cmd_lern_regel_liste.description = "Zeigt alle Direktregeln"
cmd_lern_regel_liste.category    = "Gedächtnis"


async def cmd_lern_regel_loeschen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /lern_regel_loeschen <nr>
    Löscht eine Direktregel anhand ihrer ID.
    """
    if not context.args:
        await update.message.reply_text("Verwendung: /lern_regel_loeschen <nr>")
        return
    try:
        rule_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Bitte eine Zahl angeben.")
        return
    found = remove_manual_rule(rule_id)
    if found:
        await update.message.reply_text(f"🗑️ Direktregel #{rule_id} gelöscht.")
    else:
        await update.message.reply_text(f"Regel #{rule_id} nicht gefunden.")

cmd_lern_regel_loeschen.description = "Löscht eine Direktregel per ID"
cmd_lern_regel_loeschen.category    = "Gedächtnis"


# ═══════════════════════════════════════════════════════════════════
# MODULE SETUP
# ═══════════════════════════════════════════════════════════════════

def setup(app):
    app.add_handler(CommandHandler("verhalten",            cmd_verhalten))
    app.add_handler(CommandHandler("verhalten_debug",      cmd_verhalten_debug))
    app.add_handler(CommandHandler("verhalten_reset",      cmd_verhalten_reset))
    app.add_handler(CommandHandler("lern_regel",           cmd_lern_regel))
    app.add_handler(CommandHandler("lern_regel_liste",     cmd_lern_regel_liste))
    app.add_handler(CommandHandler("lern_regel_loeschen",  cmd_lern_regel_loeschen))
    print(f"✅ behavior_engine geladen — {STATE_FILE}")