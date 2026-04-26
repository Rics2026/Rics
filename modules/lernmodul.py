#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lernmodul.py — RICS Autonomes Web-Lernmodul
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RICS liest selbstständig Artikel zu seinen Interessen-Topics,
extrahiert Fakten per LLM und speichert sie dauerhaft in ChromaDB.

Cron-Aufruf (alle 6h):   0 */6 * * * python3 lernmodul.py
Manuell via Telegram:     /learn [thema]
Status anzeigen:          /learn_status
"""

import os
import re
import json
import asyncio
import hashlib
import logging
import httpx
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

load_dotenv()

log = logging.getLogger(__name__)

# ── Konfiguration ─────────────────────────────────────────────────────────────
BOT_NAME         = os.getenv("BOT_NAME", "RICS")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_URL     = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL   = "deepseek-chat"
GROQ_API_KEY     = os.getenv("GROQ_API_KEY", "")
GROQ_URL         = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL       = "llama-3.3-70b-versatile"
OLLAMA_MODEL     = os.getenv("OLLAMA_MODEL", "qwen3:8b")

_THIS_DIR    = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR  = _THIS_DIR if not _THIS_DIR.endswith("modules") else os.path.dirname(_THIS_DIR)
MEMORY_DIR   = os.path.join(PROJECT_DIR, "memory")
LOG_DIR      = os.path.join(PROJECT_DIR, "logs")
VECTOR_DIR   = os.path.join(MEMORY_DIR, "vectors")
INTERESTS_FILE = os.path.join(MEMORY_DIR, "proactive_interests.json")
STATE_FILE   = os.path.join(MEMORY_DIR, "lernmodul_state.json")
LOG_FILE     = os.path.join(LOG_DIR, "lernmodul.log")

os.makedirs(MEMORY_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

# Maximale Artikel-Zeichenzahl die an den LLM gehen (Token-Schutz)
MAX_ARTICLE_CHARS = 4000
# Wie viele Artikel pro Topic-Lauf
ARTICLES_PER_TOPIC = 2
# Mindestabstand zwischen zwei Lernläufen zum selben Topic (Stunden)
MIN_RELEARN_HOURS = 8
# ChromaDB Collection für Weltwissen (getrennt von personal user_memory)
CHROMA_COLLECTION = "web_knowledge"


# ══════════════════════════════════════════════════════════
# STATE
# ══════════════════════════════════════════════════════════

def _load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"last_learned": {}, "total_facts": 0, "articles_read": 0}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_learned": {}, "total_facts": 0, "articles_read": 0}

def _save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

def _topic_due(topic: str, state: dict) -> bool:
    """True wenn Topic seit > MIN_RELEARN_HOURS nicht gelernt wurde."""
    last = state.get("last_learned", {}).get(topic)
    if not last:
        return True
    try:
        age_h = (datetime.now() - datetime.fromisoformat(last)).total_seconds() / 3600
        return age_h >= MIN_RELEARN_HOURS
    except Exception:
        return True

def _url_already_read(url: str, state: dict) -> bool:
    """True wenn diese URL bereits gelesen wurde."""
    return url in state.get("read_urls", [])

def _mark_url_read(url: str, state: dict):
    """Markiert URL als gelesen — max. 500 URLs behalten."""
    state.setdefault("read_urls", [])
    if url not in state["read_urls"]:
        state["read_urls"].append(url)
        if len(state["read_urls"]) > 500:
            state["read_urls"] = state["read_urls"][-500:]


# ══════════════════════════════════════════════════════════
# WEB: SUCHEN & ARTIKEL LESEN
# ══════════════════════════════════════════════════════════

def _search_ddg(query: str, max_results: int = 3) -> list[dict]:
    """DuckDuckGo-Suche → Liste von {title, href, body}."""
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        return results or []
    except Exception as e:
        log.warning(f"DDG-Suche Fehler für '{query}': {e}")
        return []


def _fetch_article(url: str, timeout: int = 10) -> str:
    """Lädt einen Artikel und gibt bereinigten Plaintext zurück."""
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
        resp = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
        if resp.status_code != 200:
            return ""
        html = resp.text

        # Skripte & Styles raus
        html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"<style[^>]*>.*?</style>",  " ", html, flags=re.DOTALL | re.IGNORECASE)

        # Tags entfernen
        text = re.sub(r"<[^>]+>", " ", html)

        # Whitespace normalisieren
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = text.strip()

        # Auf max. Zeichenzahl kürzen (Mitte bevorzugen wo Inhalt ist)
        if len(text) > MAX_ARTICLE_CHARS:
            text = text[:MAX_ARTICLE_CHARS]

        return text
    except Exception as e:
        log.warning(f"Artikel-Fetch Fehler {url}: {e}")
        return ""


# ══════════════════════════════════════════════════════════
# LLM: FAKTEN EXTRAHIEREN
# ══════════════════════════════════════════════════════════

async def _extract_facts_llm(topic: str, article_text: str, source_url: str) -> list[str]:
    """
    LLM extrahiert konkrete Fakten aus dem Artikel-Text.
    Gibt eine Liste von Fakten-Strings zurück.
    """
    if not article_text.strip():
        return []

    prompt = (
        f"Du bist ein Wissenssystem. Lies den folgenden Artikel-Ausschnitt zum Thema '{topic}' "
        f"und extrahiere die wichtigsten, konkreten und sachlichen Fakten.\n\n"
        f"REGELN:\n"
        f"- Nur nachprüfbare Fakten, keine Meinungen\n"
        f"- Jeder Fakt max. 2 Sätze\n"
        f"- Mindestens 3, maximal 8 Fakten\n"
        f"- Antworte NUR mit einem JSON-Array: [\"Fakt 1\", \"Fakt 2\", ...]\n"
        f"- Kein anderer Text, keine Erklärungen, kein Markdown\n\n"
        f"ARTIKEL ({source_url}):\n"
        f"---\n"
        f"{article_text}\n"
        f"---"
    )

    result_text = ""

    # 1. DeepSeek
    if DEEPSEEK_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    DEEPSEEK_URL,
                    headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
                    json={"model": DEEPSEEK_MODEL, "messages": [{"role": "user", "content": prompt}], "max_tokens": 600}
                )
                result_text = r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            log.warning(f"DeepSeek Fehler: {e}")

    # 2. Groq Fallback
    if not result_text and GROQ_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                    json={"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}], "max_tokens": 600}
                )
                result_text = r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            log.warning(f"Groq Fehler: {e}")

    # 3. Ollama Fallback
    if not result_text:
        try:
            import ollama as _ollama
            resp = _ollama.chat(
                model=OLLAMA_MODEL,
                messages=[{"role": "user", "content": prompt}]
            )
            result_text = resp["message"]["content"].strip()
        except Exception as e:
            log.warning(f"Ollama Fehler: {e}")

    if not result_text:
        return []

    # JSON parsen
    try:
        # Markdown-Backticks entfernen falls LLM sie trotzdem liefert
        clean = re.sub(r"```(?:json)?|```", "", result_text).strip()
        facts = json.loads(clean)
        if isinstance(facts, list):
            return [str(f).strip() for f in facts if f]
    except Exception:
        # Fallback: zeilenweise parsen
        lines = [l.strip().strip('"').strip("'").strip(",") for l in result_text.splitlines()]
        return [l for l in lines if len(l) > 20]

    return []


# ══════════════════════════════════════════════════════════
# CHROMADB: WISSEN SPEICHERN & SUCHEN
# ══════════════════════════════════════════════════════════

def _get_chroma_collection():
    import chromadb
    from chromadb.utils import embedding_functions
    client = chromadb.PersistentClient(path=VECTOR_DIR)
    embed  = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name="all-MiniLM-L6-v2"
    )
    return client.get_or_create_collection(name=CHROMA_COLLECTION, embedding_function=embed)


def _store_facts_chroma(facts: list[str], topic: str, source_url: str) -> int:
    """Speichert Fakten in ChromaDB web_knowledge Collection. Gibt Anzahl gespeicherter zurück."""
    if not facts:
        return 0
    stored = 0
    try:
        col = _get_chroma_collection()
        now_iso = datetime.now().isoformat()
        for fact in facts:
            # Eindeutige ID per Hash (verhindert Duplikate)
            fact_id = hashlib.md5(fact.encode()).hexdigest()
            document = f"[{topic.upper()}] {fact}"
            metadata = {
                "topic":  topic,
                "source": source_url[:200],
                "date":   now_iso[:10],
                "type":   "web_knowledge"
            }
            # Upsert: überschreibt falls gleicher Hash
            col.upsert(
                ids=[fact_id],
                documents=[document],
                metadatas=[metadata]
            )
            stored += 1
    except Exception as e:
        log.error(f"ChromaDB Store Fehler: {e}")
    return stored


def search_web_knowledge(query: str, n: int = 4) -> str:
    """
    Öffentliche Funktion — kann von proactive_brain / interaktion.py
    aufgerufen werden um Web-Wissen in Prompts zu injizieren.
    """
    try:
        col     = _get_chroma_collection()
        results = col.query(query_texts=[query], n_results=n)
        docs    = results.get("documents", [[]])[0]
        metas   = results.get("metadatas", [[]])[0]
        if not docs:
            return ""
        lines = []
        for doc, meta in zip(docs, metas):
            date   = meta.get("date", "")
            source = meta.get("source", "")
            lines.append(f"• {doc}  [{date}]")
        return "\n".join(lines)
    except Exception as e:
        log.warning(f"ChromaDB Query Fehler: {e}")
        return ""


# ══════════════════════════════════════════════════════════
# KERN: EIN TOPIC LERNEN
# ══════════════════════════════════════════════════════════

async def learn_topic(topic: str, notify_chat_id: int = None, bot=None) -> dict:
    """
    Lernt ein Topic: Suche → Artikel lesen → Fakten extrahieren → ChromaDB.
    Gibt Ergebnis-Dict zurück: {topic, facts_stored, articles_read, sources}
    """
    log.info(f"Lernmodul: Starte Topic '{topic}'")

    state = _load_state()  # einmal laden — wird im Loop aktualisiert

    query   = f"{topic} aktuell 2025"
    results = _search_ddg(query, max_results=ARTICLES_PER_TOPIC + 1)

    if not results:
        log.info(f"Keine DDG-Ergebnisse für '{topic}'")
        return {"topic": topic, "facts_stored": 0, "articles_read": 0, "sources": []}

    total_facts  = 0
    articles_read = 0
    sources_used  = []

    for r in results[:ARTICLES_PER_TOPIC]:
        url   = r.get("href", "")
        title = r.get("title", "")
        body  = r.get("body", "")

        if not url:
            continue

        # URL schon gelesen? Überspringen.
        if _url_already_read(url, state):
            log.info(f"  ⏭ Bereits gelesen: {url[:60]}")
            continue

        # Artikel vollständig laden (body aus DDG ist oft zu kurz)
        article_text = _fetch_article(url)

        # Fallback: DDG-Snippet nutzen wenn Fetch leer
        if len(article_text) < 200 and body:
            article_text = body

        if not article_text.strip():
            continue

        articles_read += 1
        log.info(f"  → Lese: {title[:60]} ({len(article_text)} Zeichen)")

        facts = await _extract_facts_llm(topic, article_text, url)
        if facts:
            n = _store_facts_chroma(facts, topic, url)
            total_facts  += n
            sources_used.append({"url": url, "title": title, "facts": n})
            log.info(f"     {n} Fakten gespeichert aus {url[:50]}")

        # URL als gelesen markieren & State sofort speichern
        _mark_url_read(url, state)
        _save_state(state)

        # Kurze Pause zwischen Requests
        await asyncio.sleep(1.5)

    # State aktualisieren (Zähler & last_learned)
    state.setdefault("last_learned", {})[topic] = datetime.now().isoformat()
    state["total_facts"]   = state.get("total_facts", 0) + total_facts
    state["articles_read"] = state.get("articles_read", 0) + articles_read
    _save_state(state)

    # Log-Eintrag
    _write_log({
        "ts":      datetime.now().strftime("%d.%m.%Y %H:%M"),
        "topic":   topic,
        "facts":   total_facts,
        "articles": articles_read,
        "sources": [s["url"] for s in sources_used]
    })

    result = {
        "topic":         topic,
        "facts_stored":  total_facts,
        "articles_read": articles_read,
        "sources":       sources_used
    }

    # Optionale Telegram-Benachrichtigung bei interessantem Fund
    if notify_chat_id and bot and total_facts >= 3:
        snippet = ""
        try:
            found = search_web_knowledge(topic, n=1)
            if found:
                # Ersten Fakt als Vorschau
                snippet = "\n_" + found.split("\n")[0][:120] + "..._"
        except Exception:
            pass

        msg = (
            f"🧠 Hab gerade über *{topic}* gelernt —\n"
            f"{total_facts} neue Fakten aus {articles_read} Artikel(n) in meinem Gedächtnis.{snippet}"
        )
        try:
            await bot.send_message(chat_id=notify_chat_id, text=msg, parse_mode="Markdown")
        except Exception as e:
            log.warning(f"Telegram Notify Fehler: {e}")

    return result


# ══════════════════════════════════════════════════════════
# LOG HILFE
# ══════════════════════════════════════════════════════════

def _write_log(entry: dict):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _read_log(n: int = 20) -> list[dict]:
    if not os.path.exists(LOG_FILE):
        return []
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        entries = []
        for l in reversed(lines[-n:]):
            try:
                entries.append(json.loads(l))
            except Exception:
                pass
        return entries
    except Exception:
        return []


# ══════════════════════════════════════════════════════════
# AUTONOMER LAUF (Cron / proactive_brain)
# ══════════════════════════════════════════════════════════

async def auto_learn(notify_chat_id: int = None, bot=None, max_topics: int = 3):
    """
    Wird per Cron oder proactive_brain aufgerufen.
    Lernt die Top-Topics aus proactive_interests die noch nicht kürzlich gelernt wurden.
    """
    state   = _load_state()
    topics  = _get_top_topics(max_topics)

    if not topics:
        log.info("Lernmodul: Keine Topics aus proactive_interests gefunden.")
        return

    for topic in topics:
        if _topic_due(topic, state):
            await learn_topic(topic, notify_chat_id=notify_chat_id, bot=bot)
            state = _load_state()  # Neu laden nach jedem Topic
            await asyncio.sleep(3)


def _get_top_topics(n: int = 3) -> list[str]:
    """Liest Top-Interessen aus proactive_interests.json."""
    if not os.path.exists(INTERESTS_FILE):
        # Fallback: Standard-Topics
        return ["künstliche intelligenz", "raspberry pi", "python programming"]
    try:
        with open(INTERESTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        sorted_topics = sorted(data.items(), key=lambda x: x[1].get("score", 0), reverse=True)
        return [t for t, _ in sorted_topics[:n] if t not in ("familie", "system")]
    except Exception:
        return []


# ══════════════════════════════════════════════════════════
# TELEGRAM COMMANDS
# ══════════════════════════════════════════════════════════

async def cmd_learn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /learn [thema]
    Ohne Thema: autonomer Lauf mit Top-Interessen
    Mit Thema: dieses Thema sofort lernen
    """
    args = context.args
    chat_id = update.effective_chat.id

    if args:
        topic = " ".join(args).lower().strip()
        msg   = await update.message.reply_text(
            f"🧠 Lerne gerade über *{topic}*...\n_Artikel suchen → lesen → Fakten extrahieren_",
            parse_mode="Markdown"
        )
        result = await learn_topic(topic, notify_chat_id=None, bot=None)

        f  = result["facts_stored"]
        ar = result["articles_read"]

        if f > 0:
            # Zeige frisch gelernte Fakten als Vorschau
            preview = search_web_knowledge(topic, n=3)
            preview_text = ""
            if preview:
                preview_text = f"\n\n*Was ich gelernt habe:*\n{preview}"

            await msg.edit_text(
                f"✅ *{topic.title()}* — {f} Fakten aus {ar} Artikel(n) gespeichert.{preview_text}",
                parse_mode="Markdown"
            )
        else:
            await msg.edit_text(
                f"⚠️ Zu *{topic}* konnte ich keine verwertbaren Fakten finden.\n"
                f"_(Artikel gelesen: {ar})_",
                parse_mode="Markdown"
            )
    else:
        # Autonomer Lauf
        topics = _get_top_topics(3)
        if not topics:
            await update.message.reply_text(
                "⚠️ Keine Interessen-Topics gefunden.\nNutze `/learn [thema]` für direktes Lernen.",
                parse_mode="Markdown"
            )
            return

        msg = await update.message.reply_text(
            f"🧠 Starte Lernlauf für: {', '.join(topics)}\n_Bitte einen Moment..._",
            parse_mode="Markdown"
        )
        state = _load_state()
        learned = []
        for topic in topics:
            if _topic_due(topic, state):
                r = await learn_topic(topic)
                learned.append(f"• *{topic}*: {r['facts_stored']} Fakten")
                state = _load_state()
                await asyncio.sleep(2)
            else:
                learned.append(f"• *{topic}*: ✓ kürzlich gelernt")

        await msg.edit_text(
            "🧠 *Lernlauf abgeschlossen:*\n" + "\n".join(learned),
            parse_mode="Markdown"
        )


async def cmd_learn_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/learn_status — Statistik und letzte Lerneinträge."""
    state   = _load_state()
    log_entries = _read_log(10)

    total_facts    = state.get("total_facts", 0)
    total_articles = state.get("articles_read", 0)
    last_learned   = state.get("last_learned", {})

    # Letzter Eintrag
    last_ts = ""
    if log_entries:
        last_ts = log_entries[0].get("ts", "")

    # Top Topics nach letztem Lernzeitpunkt
    recent_topics = ""
    if last_learned:
        sorted_ll = sorted(last_learned.items(), key=lambda x: x[1], reverse=True)[:5]
        for topic, ts in sorted_ll:
            try:
                age_h = (datetime.now() - datetime.fromisoformat(ts)).total_seconds() / 3600
                recent_topics += f"\n  • *{topic}*: vor {age_h:.0f}h"
            except Exception:
                recent_topics += f"\n  • *{topic}*"

    # ChromaDB Zähler
    chroma_count = 0
    try:
        col = _get_chroma_collection()
        chroma_count = col.count()
    except Exception:
        pass

    text = (
        f"📚 *{BOT_NAME} Lernmodul — Status*\n\n"
        f"🗂 Fakten in ChromaDB: *{chroma_count}*\n"
        f"📰 Artikel gelesen gesamt: *{total_articles}*\n"
        f"💡 Fakten gelernt gesamt: *{total_facts}*\n"
        f"🕐 Letzter Lauf: *{last_ts or 'noch nie'}*\n"
    )

    if recent_topics:
        text += f"\n*Zuletzt gelernte Topics:*{recent_topics}\n"

    # Letzte 3 Log-Einträge
    if log_entries[:3]:
        text += "\n*Letzte Lerneinträge:*\n"
        for e in log_entries[:3]:
            text += f"  • {e.get('ts','')} | {e.get('topic','')} → {e.get('facts',0)} Fakten\n"

    text += "\n_Nutze `/learn [thema]` um gezielt zu lernen._"

    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_learn_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/learn_ask [frage] — Sucht Antwort im gelernten Web-Wissen."""
    args = context.args
    if not args:
        await update.message.reply_text(
            "Frag mich was! Beispiel: `/learn_ask Was ist ein Raspberry Pi 5?`",
            parse_mode="Markdown"
        )
        return

    query   = " ".join(args)
    results = search_web_knowledge(query, n=5)

    if not results:
        await update.message.reply_text(
            f"❌ Zu *{query}* hab ich noch nichts in meinem Web-Wissen.\n"
            f"Versuch: `/learn {query.split()[0].lower()}`",
            parse_mode="Markdown"
        )
        return

    await update.message.reply_text(
        f"🧠 *Was ich über '{query}' weiß:*\n\n{results}",
        parse_mode="Markdown"
    )


# ══════════════════════════════════════════════════════════
# CRON-ENTRY (python3 lernmodul.py)
# ══════════════════════════════════════════════════════════

def start():
    """Einstieg für proactive_brain / bot auto-scan (kein Cron-Kontext)."""
    pass  # Modul ist command-getrieben + Cron


if __name__ == "__main__":
    # Cron-Aufruf: Autonomer Lernlauf ohne Telegram-Notify
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(os.path.join(LOG_DIR, "lernmodul_cron.log")),
            logging.StreamHandler()
        ]
    )
    print(f"[{datetime.now().strftime('%H:%M')}] RICS Lernmodul — Autonomer Lauf startet...")

    async def _cron_main():
        topics = _get_top_topics(3)
        state  = _load_state()
        for topic in topics:
            if _topic_due(topic, state):
                r = await learn_topic(topic)
                print(f"  ✓ {topic}: {r['facts_stored']} Fakten aus {r['articles_read']} Artikel(n)")
                state = _load_state()
                await asyncio.sleep(3)
            else:
                print(f"  ⏭ {topic}: kürzlich gelernt, übersprungen")

    asyncio.run(_cron_main())
    print("Lernlauf abgeschlossen.")


# ── Metadaten ─────────────────────────────────────────────────────────────────
cmd_learn.description       = "Web-Artikel zu einem Thema lernen und in Gedächtnis speichern | Cron: 0 */6 * * * python3 modules/lernmodul.py"
cmd_learn.category          = "Gedächtnis"
cmd_learn_status.description = "Status des autonomen Lernmoduls anzeigen"
cmd_learn_status.category   = "Gedächtnis"
cmd_learn_ask.description   = "Im gelernten Web-Wissen suchen"
cmd_learn_ask.category      = "Gedächtnis"


def setup(app):
    app.add_handler(CommandHandler("learn",        cmd_learn))
    app.add_handler(CommandHandler("learn_status", cmd_learn_status))
    app.add_handler(CommandHandler("learn_ask",    cmd_learn_ask))