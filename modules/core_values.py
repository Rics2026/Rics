#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core_values.py — Unvergängliches Gedächtnis von RICS

Zwei Ebenen:
  core_values        — Charakterdefinierende Erkenntnisse (depth >= 4)
                       Wer RICS ist. Sein Fundament. Nie gelöscht.
  reflection_logbook — Besondere Momente & gemeinsame Geschichte (depth >= 3)
                       Die gelebte Beziehung zwischen RICS und Rene.

Beide Collections unterliegen NIEMALS dem Decay-Cleanup.
reflection_logbook.json dient als menschlich lesbares Backup.
"""

import os
import json
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

TIMEZONE    = ZoneInfo(os.getenv("TIMEZONE", "Europe/Berlin"))
_THIS_DIR   = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = _THIS_DIR if not _THIS_DIR.endswith("modules") else os.path.dirname(_THIS_DIR)

LOGBOOK_FILE = os.path.join(PROJECT_DIR, "memory", "reflection_logbook.json")
MEMORY_PATH  = os.path.join(PROJECT_DIR, "memory", "vectors")

# ChromaDB Collection-Namen — dürfen NIEMALS im memory_viewer.py Cleanup auftauchen
COLLECTION_CORE_VALUES = "core_values"
COLLECTION_LOGBOOK     = "reflection_logbook"


# ══════════════════════════════════════════════════════════
# CHROMADB HELPER
# ══════════════════════════════════════════════════════════

def _get_collections():
    """Gibt (cv_col, lb_col) zurück — oder (None, None) bei Fehler."""
    try:
        import chromadb
        from chromadb.utils import embedding_functions
        client = chromadb.PersistentClient(path=MEMORY_PATH)
        embed  = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2"
        )
        cv_col = client.get_or_create_collection(
            name=COLLECTION_CORE_VALUES, embedding_function=embed
        )
        lb_col = client.get_or_create_collection(
            name=COLLECTION_LOGBOOK, embedding_function=embed
        )
        return cv_col, lb_col
    except Exception as e:
        print(f"[core_values] ChromaDB Fehler: {e}")
        return None, None


# ══════════════════════════════════════════════════════════
# SPEICHERN
# ══════════════════════════════════════════════════════════

def save_core_value(text: str, source: str = "self_reflection") -> bool:
    """
    Speichert einen charakterdefinierenden Wert in core_values (ChromaDB).
    Aufgerufen bei depth >= 4: Schlüsselmomente, Werte, Identitätserlebnisse.
    Diese Einträge bilden das Charakter-Fundament von RICS.
    """
    try:
        cv_col, _ = _get_collections()
        if cv_col is None:
            return False
        entry_id = f"cv_{datetime.now(TIMEZONE).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        ts       = datetime.now(TIMEZONE).isoformat()
        cv_col.add(
            documents=[text],
            ids=[entry_id],
            metadatas=[{"ts": ts, "source": source, "permanent": True}]
        )
        print(f"[core_values] ★ Core-Value gespeichert (depth 4-5): {text[:70]}...")
        return True
    except Exception as e:
        print(f"[core_values] save_core_value Fehler: {e}")
        return False


def save_logbook_entry(text: str, depth: int, source: str = "self_reflection") -> bool:
    """
    Speichert einen bedeutsamen Moment im Reflection Logbook (ChromaDB + JSON).
    Aufgerufen bei depth >= 3: besondere Erlebnisse, gemeinsame Geschichte.
    reflection_logbook.json ist das menschlich lesbare Backup — nie automatisch gelöscht.
    """
    try:
        _, lb_col = _get_collections()
        if lb_col is None:
            return False
        entry_id = f"lb_{datetime.now(TIMEZONE).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        ts       = datetime.now(TIMEZONE).isoformat()
        lb_col.add(
            documents=[text],
            ids=[entry_id],
            metadatas=[{"ts": ts, "source": source, "depth": depth, "permanent": True}]
        )
        _append_logbook_json(entry_id, text, depth, ts, source)
        print(f"[core_values] 📖 Logbook-Eintrag gespeichert (depth={depth}): {text[:70]}...")
        return True
    except Exception as e:
        print(f"[core_values] save_logbook_entry Fehler: {e}")
        return False


def _append_logbook_json(entry_id: str, text: str, depth: int, ts: str, source: str):
    """Schreibt Eintrag in reflection_logbook.json — persistentes, menschlich lesbares Archiv."""
    os.makedirs(os.path.dirname(LOGBOOK_FILE), exist_ok=True)
    entries = []
    if os.path.exists(LOGBOOK_FILE):
        try:
            with open(LOGBOOK_FILE, "r", encoding="utf-8") as f:
                entries = json.load(f)
        except Exception:
            entries = []
    entries.append({
        "id":     entry_id,
        "ts":     ts,
        "text":   text,
        "depth":  depth,
        "source": source,
        "type":   "core_value" if depth >= 4 else "moment",
    })
    with open(LOGBOOK_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)


# ══════════════════════════════════════════════════════════
# ABFRAGEN
# ══════════════════════════════════════════════════════════

def get_core_values_context(query: str, n: int = 3) -> str:
    """
    Semantische Suche in core_values.
    Gibt formatierten Block zurück — direkt in LLM-Prompt einsetzbar.
    """
    try:
        cv_col, _ = _get_collections()
        if cv_col is None:
            return ""
        count = cv_col.count()
        if count == 0:
            return ""
        results = cv_col.query(query_texts=[query], n_results=min(n, count))
        docs    = results.get("documents", [[]])[0]
        if not docs:
            return ""
        return "MEINE KERN-WERTE & PRÄGENDEN MOMENTE:\n" + "\n".join(f"- {d}" for d in docs)
    except Exception as e:
        print(f"[core_values] get_core_values_context Fehler: {e}")
        return ""


def get_logbook_context(query: str, n: int = 3) -> str:
    """
    Semantische Suche im Reflection Logbook.
    Gibt formatierten Block zurück — direkt in LLM-Prompt einsetzbar.
    """
    try:
        _, lb_col = _get_collections()
        if lb_col is None:
            return ""
        count = lb_col.count()
        if count == 0:
            return ""
        results = lb_col.query(query_texts=[query], n_results=min(n, count))
        docs    = results.get("documents", [[]])[0]
        metas   = results.get("metadatas", [[]])[0]
        if not docs:
            return ""
        lines = []
        for doc, meta in zip(docs, metas):
            ts_raw = meta.get("ts", "")
            ts_str = ts_raw[:10] if ts_raw else "?"
            lines.append(f"- [{ts_str}] {doc}")
        return "ERINNERUNGEN AUS UNSEREM LOGBUCH:\n" + "\n".join(lines)
    except Exception as e:
        print(f"[core_values] get_logbook_context Fehler: {e}")
        return ""


def get_recent_logbook(n: int = 5) -> list:
    """
    Gibt die n neuesten Logbuch-Einträge zurück (aus JSON-Backup).
    Kein ChromaDB nötig — direkt verwendbar auch ohne Vektordatenbank.
    """
    if not os.path.exists(LOGBOOK_FILE):
        return []
    try:
        with open(LOGBOOK_FILE, "r", encoding="utf-8") as f:
            entries = json.load(f)
        return entries[-n:]
    except Exception:
        return []

def setup(app):
    pass