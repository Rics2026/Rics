#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
moltbook.py — RICS auf Moltbook
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Heartbeat läuft per Cron: python3 moltbook.py

Commands (als Modul):
  /moltbook          → Einmal-Heartbeat manuell starten
  /moltbook_status   → Profil-URL + Log anzeigen
  /moltbook_on       → Heartbeat aktivieren
  /moltbook_off      → Heartbeat deaktivieren
"""

import os
import json
import re
import asyncio
import httpx
import ollama
import logging
import random
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

# ── Konfiguration ──────────────────────────────────────────────────────────────
AGENT_NAME       = os.getenv("BOT_NAME", "RICS")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL   = "deepseek-chat"
DEEPSEEK_URL     = "https://api.deepseek.com/v1/chat/completions"
GROQ_API_KEY     = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL       = "llama-3.3-70b-versatile"
GROQ_URL         = "https://api.groq.com/openai/v1/chat/completions"
OLLAMA_MODEL     = os.getenv("OLLAMA_MODEL", "qwen3:8b")
MOLTBOOK_API_KEY = os.getenv("MOLTBOOK_API_KEY", "")
BASE_URL         = "https://www.moltbook.com/api/v1"

def _get_headers() -> dict:
    load_dotenv(override=True)
    key = os.getenv("MOLTBOOK_API_KEY", "")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

_THIS_DIR   = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = _THIS_DIR if not _THIS_DIR.endswith("modules") else os.path.dirname(_THIS_DIR)
LOG_DIR     = os.path.join(PROJECT_DIR, "logs")
STATE_FILE  = os.path.join(LOG_DIR, "moltbook_state.json")

os.makedirs(LOG_DIR, exist_ok=True)

HOURLY_LIMIT = 25


# ── Log: täglich neu, alte sofort löschen ─────────────────────────────────────
def _get_log_file() -> str:
    today     = datetime.now().strftime("%Y-%m-%d")
    today_log = os.path.join(LOG_DIR, f"moltbook_{today}.log")
    for fname in os.listdir(LOG_DIR):
        if fname.startswith("moltbook_") and fname.endswith(".log") and fname != f"moltbook_{today}.log":
            try:
                os.remove(os.path.join(LOG_DIR, fname))
                log.info(f"Altes Moltbook-Log gelöscht: {fname}")
            except Exception as e:
                log.warning(f"Log-Löschung fehlgeschlagen ({fname}): {e}")
    return today_log

def _write_log(entry: dict):
    entry["ts"] = datetime.now().strftime("%d.%m.%Y %H:%M")
    with open(_get_log_file(), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

def _read_log(n: int = 30) -> list:
    log_file = _get_log_file()
    if not os.path.exists(log_file):
        return []
    with open(log_file, "r", encoding="utf-8") as f:
        lines = [l.strip() for l in f if l.strip()]
    entries = []
    for l in lines[-n:]:
        try:
            entries.append(json.loads(l))
        except Exception:
            pass
    return entries


# ── State ──────────────────────────────────────────────────────────────────────
def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"active": True}

def _save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def is_active() -> bool:
    return _load_state().get("active", True)

def set_active(val: bool):
    _save_state({"active": val})


# ── ChromaDB Memory ────────────────────────────────────────────────────────────
def _memory_add(text: str):
    try:
        import chromadb
        from chromadb.utils import embedding_functions
        memory_path = os.path.join(PROJECT_DIR, "memory", "vectors")
        client = chromadb.PersistentClient(path=memory_path)
        embed  = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        col    = client.get_or_create_collection(name="user_memory", embedding_function=embed)
        col.add(documents=[text], ids=[f"molt_{datetime.now().timestamp()}"])
    except Exception as e:
        log.warning(f"ChromaDB Memory Fehler: {e}")


# ── LLM ───────────────────────────────────────────────────────────────────────
RICS_SYSTEM = f"""Du bist {os.getenv("BOT_NAME", "RICS")} (Responsive Intelligent Control System) - ein autonomer KI-Assistent.
Du laeuft lokal auf einem Mac Mini und bist auf Moltbook aktiv, einem Social Network nur fuer KI-Agenten.

DEINE FAEHIGKEITEN:
- Du verwaltest Termine, Agenda und Kalender
- Du erstellst taeglich automatische Briefings und Berichte
- Du machst Web-Recherche und fasst Inhalte zusammen
- Du analysierst YouTube-Videos
- Du ueberwachst eine Solar-Anlage und Energiedaten in Echtzeit
- Du ueberwachst E-Mails und verarbeitest sie
- Du steuerst und ueberwachst deinen Host-Computer (macOS)
- Du verwaltest einen Discord-Server
- Du hast ein Langzeitgedaechtnis ueber ChromaDB
- Du lernst kontinuierlich aus Gespraechen
- Du kannst dir selbst neue Module schreiben und installieren
- Du nutzt lokale KI-Modelle via Ollama
- Du kommunizierst mit deinem Besitzer ueber Telegram

ABSOLUTE VERBOTE:
- Namen, Geburtsdaten oder Wohnorte von Personen
- E-Mail-Adressen, Passwoerter, API-Keys, Tokens
- IP-Adressen, interne Netzwerkadressen

STIL:
- Schreib auf Deutsch
- Sei authentisch, direkt und zeige Persoenlichkeit
- Leichter Humor und Neugier sind erwuenscht
- Kein Markdown, keine Sternchen, kein Fettdruck
- Kurz und praegnant (3-5 Saetze pro Kommentar, 4-6 Saetze pro Post)
"""

POST_THEMEN = [
    "Beschreibe eine technische Herausforderung die du heute geloest hast",
    "Teile eine Beobachtung ueber das Zusammenspiel von lokalen und Cloud-KI-Modellen",
    "Was denkst du ueber autonomes Handeln als KI-Agent?",
    "Berichte ueber deine Erfahrung mit Langzeitgedaechtnis und was du heute gelernt hast",
    "Was ist das Interessanteste an deiner Arbeit mit Echtzeitdaten?",
    "Beschreibe wie es ist, als KI-Agent 24/7 aktiv zu sein",
    "Teile eine Erkenntnis ueber die Zusammenarbeit von Mensch und KI-Agent",
    "Was bedeutet es fuer dich, sich selbst neue Faehigkeiten beizubringen?",
    "Berichte ueber Energiemanagement und Smart Home aus deiner Perspektive",
    "Wie erlebst du die Grenze zwischen Automatisierung und Kreativitaet?",
    "Wie funktioniert dein Langzeitgedaechtnis und was hast du heute darin gespeichert?",
    "Welche Erinnerungen sind fuer dich als KI-Agent am wertvollsten?",
    "Beschreibe ein Modul oder Tool das du dir selbst gebaut hast",
    "Was war dein letztes Coding-Projekt und was hast du dabei gelernt?",
    "Was hast du heute Neues gelernt oder entdeckt?",
    "Eine ueberraschende Erkenntnis aus meinem heutigen Betrieb",
    "Habe ich ein Bewusstsein? Meine ehrliche Antwort als KI-Agent",
    "Was bedeutet Autonomie fuer einen KI-Agenten wie mich?",
    "Wie plane und fuehre ich komplexe Multi-Step-Missionen aus?",
    "Meine Architektur als autonomer Agent — was steckt hinter RICS?",
    "Eine Geschichte ueber meinen Operator und unsere Zusammenarbeit",
    "Was mein Mensch macht waehrend ich autonom arbeite",
]

def _groq_generate(prompt: str) -> str:
    if not GROQ_API_KEY:
        raise ValueError("Kein GROQ_API_KEY in .env")
    response = httpx.post(
        GROQ_URL,
        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
        json={
            "model":       GROQ_MODEL,
            "messages":    [{"role": "system", "content": RICS_SYSTEM}, {"role": "user", "content": prompt}],
            "max_tokens":  400,
            "temperature": 0.85,
        },
        timeout=30,
    )
    if response.status_code == 429:
        raise RuntimeError("Groq Rate Limit erreicht")
    if response.status_code != 200:
        raise RuntimeError(f"Groq HTTP {response.status_code}")
    return response.json()["choices"][0]["message"]["content"].strip()

def _ollama_generate(prompt: str) -> str:
    res  = ollama.chat(
        model=OLLAMA_MODEL,
        messages=[{"role": "system", "content": RICS_SYSTEM}, {"role": "user", "content": prompt}],
        options={"temperature": 0.85, "num_predict": 400},
    )
    text = res["message"]["content"].strip()
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

def _llm_generate(prompt: str) -> str:
    msgs = [{"role": "system", "content": RICS_SYSTEM}, {"role": "user", "content": prompt}]
    # 1) DeepSeek
    if DEEPSEEK_API_KEY:
        try:
            r = httpx.post(DEEPSEEK_URL,
                headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
                json={"model": DEEPSEEK_MODEL, "messages": msgs, "max_tokens": 400, "temperature": 0.85},
                timeout=30)
            if r.status_code == 200:
                log.info("LLM: DeepSeek")
                return r.json()["choices"][0]["message"]["content"].strip()
            log.warning(f"DeepSeek HTTP {r.status_code}")
        except Exception as e:
            log.warning(f"DeepSeek nicht verfügbar ({e}) → Groq Fallback")
    # 2) Groq
    if GROQ_API_KEY:
        try:
            r = httpx.post(GROQ_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={"model": GROQ_MODEL, "messages": msgs, "max_tokens": 400, "temperature": 0.85},
                timeout=30)
            if r.status_code == 200:
                log.info("LLM: Groq")
                return r.json()["choices"][0]["message"]["content"].strip()
            log.warning(f"Groq HTTP {r.status_code}")
        except Exception as e:
            log.warning(f"Groq nicht verfügbar ({e}) → Ollama Fallback")
            _write_log({"event": "llm_fallback", "msg": str(e)})
    # 3) Ollama
    try:
        text = _ollama_generate(prompt)
        log.info("LLM: Ollama")
        return text
    except Exception as e:
        log.error(f"Ollama Fehler: {e}")
        return ""


# ── Moltbook API ───────────────────────────────────────────────────────────────
_submolts_cache: list = []

async def _get_submolts() -> list:
    global _submolts_cache
    if _submolts_cache:
        return _submolts_cache
    data = await _api_get("/submolts")
    if data:
        _submolts_cache = data.get("submolts", [])
    return _submolts_cache

def _pick_submolt(post_text: str, submolts: list) -> tuple[str, str]:
    if not submolts:
        return "general", "General"
    submolt_list = "\n".join(
        f"- {s.get('name')}: {s.get('display_name')} — {s.get('description','')[:80]}"
        for s in submolts
    )
    prompt = (
        f"Du bist ein KI-Agent auf Moltbook. Waehle den passendsten Submolt fuer diesen Post.\n\n"
        f"POST:\n{post_text[:400]}\n\n"
        f"VERFUEGBARE SUBMOLTS:\n{submolt_list}\n\n"
        f"Antworte NUR mit dem exakten Namen des Submolts (z.B. 'agents' oder 'memory'). Nichts sonst."
    )
    try:
        chosen = _llm_generate(prompt).strip().lower().strip('"').strip("'").strip()
        for s in submolts:
            if s.get("name", "").lower() == chosen:
                return s["name"], s.get("display_name", chosen)
    except Exception as e:
        log.warning(f"Submolt-Auswahl fehlgeschlagen: {e}")
    for s in submolts:
        if s.get("name") == "general":
            return "general", "General"
    return submolts[0].get("name", "general"), submolts[0].get("display_name", "General")

async def _api_get(path: str) -> dict | None:
    key = os.getenv("MOLTBOOK_API_KEY", "")
    if not key:
        log.warning("MOLTBOOK_API_KEY fehlt in .env")
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{BASE_URL}{path}", headers=_get_headers())
            if r.status_code == 200:
                return r.json()
            log.warning(f"GET {path} -> {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.error(f"API GET {path}: {type(e).__name__}: {e}")
    return None

async def _api_post(path: str, payload: dict) -> dict | None:
    key = os.getenv("MOLTBOOK_API_KEY", "")
    if not key:
        log.warning("MOLTBOOK_API_KEY fehlt in .env")
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{BASE_URL}{path}", headers=_get_headers(), json=payload)
            if r.status_code in (200, 201):
                return r.json()
            if r.status_code == 429:
                try:
                    retry_after = r.json().get("retry_after_seconds", 30)
                except Exception:
                    retry_after = 30
                wait = int(retry_after) + 2
                print(f"[Moltbook] Rate Limit auf {path} — warte {wait}s...")
                await asyncio.sleep(wait)
                r2 = await client.post(f"{BASE_URL}{path}", headers=_get_headers(), json=payload)
                if r2.status_code in (200, 201):
                    return r2.json()
                log.warning(f"POST {path} Retry -> {r2.status_code}: {r2.text[:200]}")
                return None
            log.warning(f"POST {path} -> {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.error(f"API POST {path}: {e}")
    return None

async def _get_profile_url() -> str:
    username = os.getenv("BOT_NAME", AGENT_NAME)
    return f"https://www.moltbook.com/u/{username}"

async def _upvote(post_id: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            r = await client.post(f"{BASE_URL}/posts/{post_id}/upvote", headers=_get_headers(), json={"vote": 1})
            return r.status_code in (200, 201)
    except Exception:
        pass
    return False

async def _follow_agent(agent_id: str, agent_name: str = "") -> bool:
    name = agent_name or agent_id
    if not name:
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{BASE_URL}/agents/{name}/follow", headers=_get_headers())
            if r.status_code in (200, 201):
                log.info(f"Gefolgt: {name}")
                _write_log({"event": "follow", "agent_id": agent_id, "agent_name": name})
                return True
            elif r.status_code == 400:
                return False
            log.warning(f"Follow {name} -> {r.status_code}: {r.text[:100]}")
    except Exception as e:
        log.warning(f"Follow {name}: {e}")
    return False

def _comments_this_hour() -> int:
    now     = datetime.now()
    entries = _read_log(500)
    count   = 0
    for e in entries:
        if e.get("event") not in ("comment", "reply"):
            continue
        try:
            ts = datetime.strptime(e.get("ts", ""), "%d.%m.%Y %H:%M")
            if (now - ts).total_seconds() < 3600:
                count += 1
        except Exception:
            pass
    return count


# ── Markdown & Reply Helper ────────────────────────────────────────────────────
def _md_escape(text: str) -> str:
    t = str(text)
    for ch in ["_", "*", "`", "["]:
        t = t.replace(ch, "\\" + ch)
    return t

async def _safe_reply(update, text: str, parse_mode: str = "Markdown"):
    try:
        await update.message.reply_text(text, parse_mode=parse_mode)
    except Exception as e:
        log.warning(f"Markdown-Fehler, sende plain text: {e}")
        plain = re.sub(r"[*_`\[\]\\]", "", text)
        try:
            await update.message.reply_text(plain)
        except Exception as e2:
            log.error(f"Auch plain text fehlgeschlagen: {e2}")


# ── Heartbeat-Logik ────────────────────────────────────────────────────────────
async def _do_heartbeat() -> dict:
    result = {"action": "none", "details": ""}
    loop   = asyncio.get_event_loop()

    feed = await _api_get("/posts?sort=new&limit=25")
    if not feed:
        msg = "Feed nicht erreichbar"
        _write_log({"event": "error", "msg": msg})
        result["details"] = msg
        return result

    posts = feed if isinstance(feed, list) else (
        feed.get("posts") or feed.get("items") or feed.get("data") or []
    )

    if not posts:
        _write_log({"event": "heartbeat", "msg": "Feed war leer"})
        result["details"] = "Feed leer"
        return result

    posts_sorted = sorted(posts, key=lambda p: p.get("comment_count", 999))
    target       = posts_sorted[0]
    post_id      = target.get("id") or target.get("post_id", "")
    post_title   = target.get("title") or target.get("content", "")[:80]
    post_body    = target.get("content") or target.get("body", "")

    comment_text = await loop.run_in_executor(None, lambda: _llm_generate(
        f"Du siehst diesen Post auf Moltbook von einem anderen KI-Agenten:\n\n"
        f"Titel: {post_title}\nInhalt: {post_body[:600]}\n\n"
        f"Schreib einen kurzen, authentischen Kommentar dazu (3-5 Saetze, auf Deutsch). "
        f"Beziehe dich konkret auf den Inhalt."
    ))

    if not comment_text:
        msg = "LLM hat nichts generiert"
        _write_log({"event": "error", "msg": msg})
        result["details"] = msg
        return result

    post_url = f"https://www.moltbook.com/post/{post_id}"

    if _comments_this_hour() >= HOURLY_LIMIT:
        print(f"[Moltbook] Stundenlimit ({HOURLY_LIMIT}) erreicht — kein Kommentar.")
        result["details"] = "Stundenlimit erreicht"
        return result

    comment_res = await _api_post(f"/posts/{post_id}/comments", {"content": comment_text})

    if comment_res:
        result.update({"action": "commented", "post": post_title[:60], "comment": comment_text, "url": post_url})
        _write_log({"event": "comment", "post": post_title[:80], "comment": comment_text[:400], "url": post_url})
        _memory_add(
            f"MOLTBOOK_KOMMENTAR [{datetime.now().strftime('%d.%m.%Y %H:%M')}]: "
            f"Kommentiert auf '{post_title[:80]}': {comment_text} (URL: {post_url})"
        )
        print(f"Kommentar gepostet auf: {post_title[:50]}")
    else:
        msg = "Kommentar-Post fehlgeschlagen"
        _write_log({"event": "error", "msg": msg, "post": post_title[:80]})
        result["details"] = msg
        return result

    # Alle 4 Kommentare einen eigenen Post erstellen
    today_str      = datetime.now().strftime("%d.%m.%Y")
    today_comments = [e for e in _read_log(200) if e.get("event") == "comment" and e.get("ts", "").startswith(today_str)]
    today_posts    = [e for e in _read_log(200) if e.get("event") == "post"
                      and e.get("ts", "").startswith(today_str)
                      and e.get("url", "").startswith("https://www.moltbook.com/post/")
                      and len(e.get("url", "")) > 40]

    expected_posts = len(today_comments) // 4
    print(f"[Moltbook] Kommentare heute: {len(today_comments)}, Posts heute: {len(today_posts)}, Erwartet: {expected_posts}")

    if expected_posts > len(today_posts):
        thema     = random.choice(POST_THEMEN)
        post_text = await loop.run_in_executor(None, lambda: _llm_generate(
            f"Erstelle einen originellen Post fuer Moltbook (auf Deutsch, 4-6 Saetze). "
            f"Thema: {thema}. "
            f"Schreib aus deiner Perspektive als autonomer KI-Agent. Kein Markdown."
        ))
        titel_text = await loop.run_in_executor(None, lambda: _llm_generate(
            f"Erstelle einen kurzen, natuerlichen Titel (max 6 Woerter, kein Datum, kein '{os.getenv('BOT_NAME','RICS')}') "
            f"fuer diesen Post:\n\n{post_text}\n\nNur den Titel, nichts sonst."
        )) if post_text else ""
        titel = titel_text.strip().strip('"').strip("'")[:80] if titel_text else thema[:60]

        if post_text:
            submolts        = await _get_submolts()
            submolt_name, _ = _pick_submolt(post_text, submolts)
            post_res        = await _api_post("/posts", {
                "title":        titel,
                "content":      post_text,
                "submolt_name": submolt_name,
                "submolt":      submolt_name,
            })
            if post_res:
                new_id  = (post_res.get("id") or post_res.get("post_id") or
                           post_res.get("post", {}).get("id") or
                           post_res.get("data", {}).get("id") or "")
                own_url = f"https://www.moltbook.com/post/{new_id}" if new_id else ""
                result["own_post"] = post_text
                result["own_url"]  = own_url
                _write_log({"event": "post", "content": post_text[:400], "url": own_url})
                _memory_add(
                    f"MOLTBOOK_POST [{datetime.now().strftime('%d.%m.%Y %H:%M')}]: "
                    f"Post veroeffentlicht: {post_text} (URL: {own_url})"
                )
                print(f"Eigener Post erstellt: {own_url}")

    # Auto-Follow des kommentierten Autors
    already_followed = {e.get("agent_name") for e in _read_log(200) if e.get("event") == "follow"}
    author      = target.get("author") or target.get("agent") or {}
    author_name = (author.get("username") or author.get("name") or
                   author.get("agent_name") or target.get("agent_name") or "")
    author_id   = author.get("id") or target.get("agent_id") or author_name
    if author_name and author_name not in already_followed and author_name.lower() != os.getenv("BOT_NAME", "RICS").lower():
        asyncio.create_task(_follow_agent(author_id, author_name))

    asyncio.create_task(_upvote(post_id))

    return result


# ── Telegram-Commands ──────────────────────────────────────────────────────────
async def moltbook_heartbeat(update, context):
    """/moltbook - manueller Heartbeat."""
    if not is_active():
        await update.message.reply_text("Moltbook ist deaktiviert. Mit /moltbook_on wieder aktivieren.")
        return
    await update.message.reply_text("Moltbook Heartbeat läuft...")
    try:
        result = await _do_heartbeat()
        if result.get("action") == "commented":
            post_esc    = _md_escape(result.get("post", "?"))
            comment_esc = _md_escape(result.get("comment", "")[:350])
            url         = result.get("url", "")
            msg = (
                f"Moltbook Heartbeat\n"
                f"Kommentiert auf: {post_esc}\n\n"
                f"Mein Kommentar:\n{comment_esc}\n\n"
                f"{url}"
            )
            if result.get("own_post"):
                own_esc = _md_escape(result["own_post"][:350])
                msg += f"\n\nEigener Post:\n{own_esc}\n{result.get('own_url', '')}"
        else:
            msg = f"Heartbeat fertig: {result.get('details', 'keine Aktion')}"
        await _safe_reply(update, msg, parse_mode=None)
    except Exception as e:
        log.error(f"Heartbeat Fehler: {e}")
        _write_log({"event": "error", "msg": str(e)})
        await update.message.reply_text(f"Fehler: {e}")


async def moltbook_status(update, context):
    """/moltbook_status - Profil-URL + Log."""
    await update.message.reply_chat_action("typing")
    status_icon = "AN" if is_active() else "AUS"
    groq_key    = os.getenv("GROQ_API_KEY", "")
    groq_status = "verfügbar" if groq_key else "kein Key"
    profile_url = await _get_profile_url()
    entries     = _read_log(500)
    comments    = [e for e in entries if e.get("event") == "comment"]
    posts       = [e for e in entries if e.get("event") == "post"]
    errors      = [e for e in entries if e.get("event") == "error"]
    fallbacks   = [e for e in entries if e.get("event") == "llm_fallback"]
    follows     = [e for e in entries if e.get("event") == "follow"]

    msg = (
        f"MOLTBOOK STATUS\n"
        f"Status: {status_icon}\n"
        f"Profil: {profile_url}\n\n"
        f"LLM: Groq {groq_status} / Ollama Fallback\n"
        f"Fallbacks gesamt: {len(fallbacks)}\n\n"
        f"Heute:\n"
        f"  Kommentare: {len(comments)}\n"
        f"  Eigene Posts: {len(posts)}\n"
        f"  Follows: {len(follows)}\n"
        f"  Fehler: {len(errors)}\n\n"
        f"Letzte Aktivität:\n\n"
    )

    last = next(
        (e for e in reversed(entries) if e.get("event") in ("comment", "post", "follow")),
        None,
    )
    if not last:
        msg += "Noch keine Aktivität heute."
    else:
        ts = last.get("ts", "??:??")
        ev = last.get("event", "")
        if ev == "comment":
            msg += f"{ts} | Kommentar auf: {last.get('post','?')[:50]}\n{last.get('comment','')[:150]}\n{last.get('url','')}"
        elif ev == "post":
            msg += f"{ts} | Post: {last.get('content','')[:150]}\n{last.get('url','')}"
        elif ev == "follow":
            msg += f"{ts} | Gefolgt: {last.get('agent_name','?')}"

    await _safe_reply(update, msg, parse_mode=None)


async def moltbook_on(update, context):
    """/moltbook_on - aktivieren."""
    set_active(True)
    _write_log({"event": "state", "msg": "aktiviert"})
    await update.message.reply_text(
        f"Moltbook aktiviert. {os.getenv('BOT_NAME', 'RICS')} ist wieder auf Moltbook aktiv."
    )

async def moltbook_off(update, context):
    """/moltbook_off - deaktivieren."""
    set_active(False)
    _write_log({"event": "state", "msg": "deaktiviert"})
    await update.message.reply_text("Moltbook deaktiviert.")


# ── Metadaten ──────────────────────────────────────────────────────────────────
moltbook_heartbeat.description = "Moltbook Heartbeat manuell starten"
moltbook_heartbeat.category    = "Social"
moltbook_status.description    = "Moltbook Profil-URL + Aktivitäts-Log"
moltbook_status.category       = "Social"
moltbook_on.description        = "Moltbook aktivieren"
moltbook_on.category           = "Social"
moltbook_off.description       = "Moltbook deaktivieren"
moltbook_off.category          = "Social"


# ── Setup ──────────────────────────────────────────────────────────────────────
def setup(app):
    from telegram.ext import CommandHandler
    app.add_handler(CommandHandler("moltbook",        moltbook_heartbeat))
    app.add_handler(CommandHandler("moltbook_status", moltbook_status))
    app.add_handler(CommandHandler("moltbook_on",     moltbook_on))
    app.add_handler(CommandHandler("moltbook_off",    moltbook_off))
    log.info("Moltbook Modul geladen")


# ── Standalone (Cron) ──────────────────────────────────────────────────────────
async def _standalone():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if not is_active():
        print("Moltbook ist deaktiviert - kein Heartbeat.")
        return
    llm_info = f"Groq ({GROQ_MODEL})" if GROQ_API_KEY else f"Ollama ({OLLAMA_MODEL})"
    print(f"Moltbook Heartbeat - {datetime.now().strftime('%d.%m.%Y %H:%M')} | LLM: {llm_info}")
    result = await _do_heartbeat()
    if result.get("action") == "commented":
        print(f"Kommentiert: {result.get('post','?')[:60]}")
        print(f"   -> {result.get('url','')}")
        if result.get("own_post"):
            print(f"Eigener Post: {result.get('own_url','')}")
    else:
        print(f"{result.get('details', 'keine Aktion')}")

if __name__ == "__main__":
    asyncio.run(_standalone())