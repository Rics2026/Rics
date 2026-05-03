#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mood_timer.py — Adaptiver Proaktiv-Timer für RICS

Kein "Bist du da?" mehr. RICS sendet einfach — und lernt aus dem Verhalten:

  Kein Antworten / kurz angebunden  →  Timer steigt:  30 → 30 → 60 → 90 → 120 min
  Aktives Gespräch läuft            →  Timer sinkt auf 10 min
  Schnelle Antwort + gute Stimmung  →  Timer sinkt bis auf 3 min

Stimmungs-Score (mood_score) fließt separat ein und beeinflusst Schwellwerte.
"""

import os
import json
import time
from datetime import datetime

# ── Konfiguration ─────────────────────────────────────────────────────────────

# Index = skip_level, Wert = Wartezeit in Minuten
INTERVAL_STEPS_MIN = [3, 10, 30, 30, 60, 90, 120]
MAX_SKIP_LEVEL     = len(INTERVAL_STEPS_MIN) - 1
DEFAULT_SKIP_LEVEL = 1   # Start: 10 Minuten

# Stimmungsindikatoren (lowercase)
MOOD_POSITIVE = [
    "super", "geil", "nice", "toll", "danke", "genial", "cool", "perfekt",
    "top", "klasse", "hammer", "schön", "gut", "prima", "läuft", "funktioniert",
    "stimmt", "klar", "natürlich", "haha", "lol", "😊", "😄", "👍", "🎉", "✅",
    "freut mich", "ich freue", "wunderbar", "interessant", "spannend"
]
MOOD_NEGATIVE = [
    "keine zeit", "jetzt nicht", "lass mich", "nicht stören", "bin grad",
    "bin am", "busy", "stress", "gestresst", "stressig", "später", "gleich",
    "hab gerade", "warte", "nicht jetzt", "muss grad", "keine lust"
]

CURT_THRESHOLD          = 20    # Zeichen — darunter gilt Antwort als "kurz/abweisend"
FAST_RESPONSE_SEC       = 180   # < 3 min → sehr schnelle Antwort
RECENT_MSG_SEC          = 300   # < 5 min → aktives Gespräch (interne Logik)
CONVERSATION_PAUSE_SEC  = 900   # < 15 min seit letzter Nachricht → proaktiv komplett pausieren

# State-Datei
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE   = os.path.join(_PROJECT_DIR, "memory", "mood_timer.json")


# ── State I/O ─────────────────────────────────────────────────────────────────

def _load() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "skip_level":         DEFAULT_SKIP_LEVEL,
        "last_proactive_ts":  0.0,
        "last_user_msg_ts":   0.0,
        "last_user_msg_text": "",
        "mood_score":         0.0,   # > 0 = gut drauf, < 0 = gestresst/beschäftigt
        "response_times":     [],    # letzte 10 Antwortzeiten in Sekunden
    }


def _save(state: dict):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[mood_timer] Save-Fehler: {e}")


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def _get_interval_sec(skip_level: int) -> int:
    idx = max(0, min(skip_level, MAX_SKIP_LEVEL))
    return INTERVAL_STEPS_MIN[idx] * 60


def _analyze_text(text: str) -> str:
    """Gibt 'positiv', 'negativ' oder 'neutral' zurück."""
    t   = text.lower()
    pos = sum(1 for w in MOOD_POSITIVE if w in t)
    neg = sum(1 for w in MOOD_NEGATIVE if w in t)
    if pos > neg:
        return "positiv"
    if neg > pos or len(text.strip()) < CURT_THRESHOLD:
        return "negativ"
    return "neutral"


# ── Öffentliche API ───────────────────────────────────────────────────────────

def on_user_message(text: str):
    """
    Aufrufen wenn eine User-Nachricht eingeht (in bot.py nach log_chat).
    Analysiert Stimmung und passt skip_level an.
    """
    state = _load()
    now   = time.time()

    # Ist das eine Antwort auf die letzte proaktive Nachricht?
    time_since_proactive = now - state.get("last_proactive_ts", 0)
    is_first_response    = (
        state.get("last_proactive_ts", 0) > 0 and
        state.get("last_user_msg_ts", 0) <= state.get("last_proactive_ts", 0) and
        time_since_proactive < _get_interval_sec(state.get("skip_level", DEFAULT_SKIP_LEVEL)) * 1.5
    )

    mood    = _analyze_text(text)
    msg_len = len(text.strip())

    # Mood-Score — Rolling mit Decay
    score = state.get("mood_score", 0.0) * 0.8
    if mood == "positiv":  score += 1.0
    elif mood == "negativ": score -= 1.0
    state["mood_score"] = round(max(-5.0, min(5.0, score)), 2)

    # ── Skip-Level anpassen ──────────────────────────────────────────────────
    if is_first_response:
        # Antwort auf proaktive Nachricht → Zeit + Stimmung auswerten
        state["response_times"] = (state.get("response_times", []) + [time_since_proactive])[-10:]

        if time_since_proactive < FAST_RESPONSE_SEC and mood == "positiv":
            # Sehr schnell + sehr gut drauf → stark senken (ggf. bis min)
            state["skip_level"] = max(0, state["skip_level"] - 2)
            print(f"[mood_timer] Schnell+positiv → skip={state['skip_level']} "
                  f"({INTERVAL_STEPS_MIN[max(0,state['skip_level'])]} min)")
        elif time_since_proactive < FAST_RESPONSE_SEC:
            # Schnell aber neutral/negativ
            state["skip_level"] = max(0, state["skip_level"] - 1)
            print(f"[mood_timer] Schnell → skip={state['skip_level']}")
        elif mood == "positiv":
            # Langsam aber gute Stimmung
            state["skip_level"] = max(0, state["skip_level"] - 1)
            print(f"[mood_timer] Positiv → skip={state['skip_level']}")
        elif mood == "negativ" or msg_len < CURT_THRESHOLD:
            # Kurze / abweisende Antwort → erhöhen
            state["skip_level"] = min(MAX_SKIP_LEVEL, state["skip_level"] + 1)
            print(f"[mood_timer] Kurz/negativ → skip={state['skip_level']} "
                  f"({INTERVAL_STEPS_MIN[min(MAX_SKIP_LEVEL, state['skip_level'])]} min)")
        # neutral + langsam → unverändert

    else:
        # Laufendes Gespräch (keine direkte Antwort auf Proaktiv) →
        # bei guter Stimmung leicht senken, aber nie unter Level 1
        if mood == "positiv" and state.get("skip_level", DEFAULT_SKIP_LEVEL) > 1:
            state["skip_level"] = max(1, state["skip_level"] - 1)

    state["last_user_msg_ts"]   = now
    state["last_user_msg_text"] = text[:200]
    _save(state)


def on_proactive_sent():
    """
    Aufrufen nachdem RICS eine proaktive Nachricht erfolgreich gesendet hat.
    Setzt den Timer zurück.
    """
    state = _load()
    state["last_proactive_ts"] = time.time()
    _save(state)
    lvl = state.get("skip_level", DEFAULT_SKIP_LEVEL)
    print(f"[mood_timer] Proaktiv gesendet — nächste Meldung in "
          f"~{INTERVAL_STEPS_MIN[max(0, min(lvl, MAX_SKIP_LEVEL))]} min "
          f"(skip_level={lvl})")


def should_send_now() -> bool:
    """
    Gibt True zurück wenn RICS jetzt eine proaktive Nachricht senden darf.
    Prüft adaptives Interval basierend auf Reaktionsverhalten.
    Inkrementiert skip_level wenn Deadline ohne Antwort verstrichen ist.
    """
    state = _load()
    now   = time.time()

    time_since_proactive = now - state.get("last_proactive_ts", 0)
    time_since_user_msg  = now - state.get("last_user_msg_ts",  0)
    skip_level           = state.get("skip_level", DEFAULT_SKIP_LEVEL)

    # ── Aktives Gespräch? Hard-Pause solange René tippt ─────────────────────
    if time_since_user_msg < CONVERSATION_PAUSE_SEC:
        # René hat in den letzten 15 Min geschrieben → nicht reinquatschen
        print(f"[mood_timer] Aktives Gespräch — Pause ({time_since_user_msg:.0f}s seit letzter Nachricht)")
        return False

    # ── Normaler adaptiver Timer ─────────────────────────────────────────────
    interval_sec = _get_interval_sec(skip_level)

    if time_since_proactive >= interval_sec:
        # Deadline erreicht — hat René auf die letzte Nachricht geantwortet?
        responded = state.get("last_user_msg_ts", 0) > state.get("last_proactive_ts", 0)
        if not responded and state.get("last_proactive_ts", 0) > 0:
            # Keine Antwort → skip_level erhöhen
            new_level = min(MAX_SKIP_LEVEL, skip_level + 1)
            if new_level != skip_level:
                state["skip_level"] = new_level
                _save(state)
                print(f"[mood_timer] Keine Antwort — skip_level {skip_level}→{new_level} "
                      f"({INTERVAL_STEPS_MIN[new_level]} min)")
        return True

    return False


def is_user_active() -> bool:
    """True wenn der User in den letzten CONVERSATION_PAUSE_SEC Sekunden geschrieben hat."""
    state = _load()
    return (time.time() - state.get("last_user_msg_ts", 0)) < CONVERSATION_PAUSE_SEC


def get_status() -> dict:
    """Status-Dict für /status oder Debug-Ausgaben."""
    state = _load()
    now   = time.time()
    lvl   = state.get("skip_level", DEFAULT_SKIP_LEVEL)
    lvl_c = max(0, min(lvl, MAX_SKIP_LEVEL))

    last_p = state.get("last_proactive_ts", 0)
    last_u = state.get("last_user_msg_ts",  0)
    next_in = max(0.0, _get_interval_sec(lvl) - (now - last_p))

    rt = state.get("response_times", [])
    avg_rt = round(sum(rt) / len(rt) / 60, 1) if rt else None

    return {
        "skip_level":          lvl,
        "interval_min":        INTERVAL_STEPS_MIN[lvl_c],
        "next_send_in_min":    round(next_in / 60, 1),
        "mood_score":          round(state.get("mood_score", 0.0), 2),
        "avg_response_min":    avg_rt,
        "last_proactive":      datetime.fromtimestamp(last_p).strftime("%H:%M:%S") if last_p else "—",
        "last_user_msg":       datetime.fromtimestamp(last_u).strftime("%H:%M:%S") if last_u else "—",
        "last_user_msg_text":  state.get("last_user_msg_text", "")[:60],
    }


def setup(app=None):
    """Stub — mood_timer braucht keine Telegram-Handler, muss aber für bot.py-Scan vorhanden sein."""
    pass