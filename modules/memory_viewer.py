import os
import json
import hashlib
import chromadb
import re
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

PROJECT_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEMORY_DIR    = os.path.join(PROJECT_DIR, "memory")
VECTORS_DIR   = os.path.join(MEMORY_DIR, "vectors")
PERSONAL_FILE = os.path.join(MEMORY_DIR, "personal.json")


def escape_md(text: str) -> str:
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!])", r"\\\1", str(text))

def _get_collection():
    client = chromadb.PersistentClient(path=VECTORS_DIR)
    return client.get_collection(name="user_memory")


# ── /memory_view ────────────────────────────────────────────────
async def view_memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists(VECTORS_DIR):
        return await update.message.reply_text("❌ Kein Gedächtnis-Verzeichnis gefunden.")
    try:
        col  = _get_collection()
        data = col.get(include=["documents", "metadatas"])
        docs = data.get("documents", [])
        if not docs:
            return await update.message.reply_text("🧠 Das Gedächtnis ist aktuell leer.")

        latest = docs[-10:][::-1]
        msg = "🧠 *DIE 10 NEUESTEN ERINNERUNGEN*\n"
        msg += "￣￣￣￣￣￣￣￣￣￣￣￣￣\n"
        for i, doc in enumerate(latest, 1):
            short = doc[:150].replace("\n", " ")
            msg += str(i) + r"\. " + escape_md(short) + "\n\n"
        await update.message.reply_text(msg, parse_mode="MarkdownV2")
    except Exception as e:
        await update.message.reply_text(f"❌ Fehler: {escape_md(str(e))}", parse_mode="MarkdownV2")


# ── /memory_cleanup ─────────────────────────────────────────────
async def memory_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🧹 Starte Gedächtnis-Cleanup …")
    chroma_msg = await _cleanup_chromadb()
    await update.message.reply_text(chroma_msg, parse_mode="Markdown")
    await update.message.reply_text("🔍 Analysiere personal.json …")
    await _cleanup_personal(update)


async def _cleanup_chromadb() -> str:
    if not os.path.exists(VECTORS_DIR):
        return "❌ Kein Gedächtnis-Verzeichnis."
    try:
        col       = _get_collection()
        data      = col.get(include=["documents"])
        docs      = data.get("documents", [])
        ids       = data.get("ids", [])
        total_in  = len(docs)
        to_delete = []
        seen_hashes = set()

        for doc_id, doc in zip(ids, docs):
            text = (doc or "").strip()
            if len(text.split()) < 5:
                to_delete.append((doc_id, "zu kurz")); continue
            if re.fullmatch(r"[\d\s:.\-/]+", text):
                to_delete.append((doc_id, "nur Datum/Zahl")); continue
            h = hashlib.md5(text.lower().strip().encode()).hexdigest()
            if h in seen_hashes:
                to_delete.append((doc_id, "Duplikat")); continue
            seen_hashes.add(h)

        if to_delete:
            del_ids = [d[0] for d in to_delete]
            for i in range(0, len(del_ids), 100):
                col.delete(ids=del_ids[i:i+100])

        by_reason = {}
        for _, r in to_delete:
            by_reason[r] = by_reason.get(r, 0) + 1

        lines = [
            "✅ *ChromaDB bereinigt*",
            f"Vorher: {total_in} | Entfernt: {len(to_delete)} | Nachher: {total_in - len(to_delete)}",
        ]
        for reason, count in by_reason.items():
            lines.append(f"  → {reason}: {count}×")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ ChromaDB-Fehler: {e}"


async def _cleanup_personal(update: Update):
    if not os.path.exists(PERSONAL_FILE):
        return await update.message.reply_text("ℹ️ personal.json nicht gefunden.")

    try:
        with open(PERSONAL_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return await update.message.reply_text(f"❌ Lesefehler: {e}")

    fakten = data.get("fakten", [])
    if not fakten:
        return await update.message.reply_text("ℹ️ Keine Fakten vorhanden.")

    fakten_lines = []
    for f in fakten:
        created = f.get("created", f.get("updated", ""))[:10]
        fakten_lines.append(f'  ID {f["id"]}: {f["key"]} = "{f["value"]}" ({created})')

    prompt = f"""Du bereinigst gespeicherte persoenliche Fakten eines Nutzers. Sei AKTIV – loeschen ist erwuenscht wenn es sinnvoll ist.

LOESCHEN bei einem dieser Faelle:

1. MOMENTZUSTAND — beschreibt was gerade passiert, nicht dauerhaft:
   Beispiele: "in der Wanne liegen", "Mittagspause gleich", "Frau hat Trockner an", "Stimmung pure Freude"

2. TECHNISCHE NOTIZ — interne Bot-Daten ohne persoenlichen Wert:
   Beispiele: "fakten im brain: 5", "mission erledigt: vorhin", "module: 2 Module im Labor", "drucker_status: Anzeige"
   AUCH: Eintraege wie "Vorschlag: ...", "Aktivitaet: ...", "Keine persoenlichen infos: True"

3. VERGANGENES EREIGNIS — eindeutig abgeschlossen:
   Beispiele: "geburtstag: Heute", "Mittagspause: gleich", "feiertag: Tag der Arbeit"

4. SINNLOSER EINTRAG — kein Informationswert:
   Beispiele: "arbeit: Arbeit", "status: zweite", "Frau: vorhanden", "ausnahme: Freitag"

5. DUPLIKAT — gleicher Inhalt wie ein anderer Eintrag, nur anders formuliert:
   → Behalte den NEUEREN (hoehere ID oder spaeteres Datum). Loesche den AELTEREN.
   Beispiele fuer Duplikate:
   - "Arbeitszeit: 4 Stunden pro Woche" UND "Hinweis: 4 Stunden nur Samstags" → aelteren loeschen
   - mehrere Eintraege zu Anrede/Bevorzugte Anrede/Titel → alle bis auf den neuesten loeschen
   - "Getraenk: Bier" UND "Getraenke: Kaffee und ab und zu Bier" → aelteren loeschen (neuerer ist ausfuehrlicher)

6. WIDERSPRUCH — zwei Eintraege zum gleichen Thema mit verschiedenen Werten:
   → Behalte den NEUEREN (hoehere ID oder spaeteres Datum). Loesche den AELTEREN.
   Beispiele fuer Widersprueche:
   - "Arbeit start: 8:30" vs "Arbeitsbeginn: 8 Uhr" vs "Startzeit: 9 Uhr" → nur neuesten behalten
   - "Feierabend: ca. 16 Uhr" vs "Fertig um: 17 Uhr" → aelteren loeschen
   - "Gehalt: 450 Euro" vs "Einkommen: 330 Euro" → wenn klar dasselbe gemeint ist, aelteren loeschen
   - "Arbeitstag: Samstag" vs "Arbeitstage: Montag frei, Samstag frei" → aelteren/ungenaueren loeschen

NIEMALS loeschen:
- Dauerhaft gueltige Persoenlichkeitsdaten: Pseudonym, Dialekt, Praeferenzen, Antwortverhalten
- Familieninfos: Partner, Kinder, Freunde, Bruder
- Finanzen wenn NICHT widersprueche: Gehalt, Sparbetrag, Sparmethode, Berufsziel
- Gesundheit, Hobbys, Routinen, Urlaubspläne, Termine in der Zukunft
- Trading-Daten: Demo-Konto, Ziel, Plattform

Antworte NUR mit einem JSON-Array. Jedes Element: {{"id": <int>, "grund": "<kurz>"}}
Bei Duplikaten/Widerspruechen: Grund = "Duplikat von ID X" oder "Widerspruch zu ID X, neuerer behalten"
Falls wirklich nichts zu loeschen: []

Fakten:
{chr(10).join(fakten_lines)}

Nur JSON-Array:"""

    try:
        from core.llm_client import get_client
        result = await get_client().chat_json(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1024
        )
        print(f"🧹 personal cleanup raw result: {result}")
    except Exception as e:
        return await update.message.reply_text(f"❌ LLM-Fehler: {e}")

    # Ergebnis normalisieren — LLM kann verschiedene Formate zurückgeben:
    # - Liste:               [{"id": 27, "grund": "..."}]          → korrekt
    # - Dict mit Wrapper:    {"items": [...]}                       → auspacken
    # - Einzelner Dict:      {"id": 27, "grund": "..."}            → alter llm_client-Bug
    if isinstance(result, list):
        items = result
    elif isinstance(result, dict):
        if result.get("id"):               # alter Bug: regex hat nur ersten {...} gegriffen
            items = [result]
        else:
            items = result.get("items", result.get("deletions", result.get("loeschen", [])))
            if not isinstance(items, list):
                items = []
    else:
        items = []

    valid_ids = {f["id"] for f in fakten}
    to_delete = [r for r in items if isinstance(r, dict) and r.get("id") in valid_ids]

    if not to_delete:
        return await update.message.reply_text("✅ personal.json ist sauber – nichts zu löschen.")

    del_ids   = {item["id"] for item in to_delete}
    id_map    = {f["id"]: f for f in fakten}
    old_count = len(fakten)
    data["fakten"] = [f for f in fakten if f.get("id") not in del_ids]

    for i, f in enumerate(data["fakten"], start=1):
        f["id"] = i

    try:
        with open(PERSONAL_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        return await update.message.reply_text(f"❌ Schreibfehler: {e}")

    removed = old_count - len(data["fakten"])
    lines   = [f"✅ *personal.json bereinigt* — {removed} von {old_count} Einträgen gelöscht:\n"]
    for item in to_delete:
        fakt = id_map.get(item["id"], {})
        lines.append(f"• `{fakt.get('key','?')}` = \"{fakt.get('value','?')}\" → _{item.get('grund','')}_")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    print(f"🧹 personal.json: {removed} Fakten gelöscht (IDs: {sorted(del_ids)})")


# ── Metadaten ────────────────────────────────────────────────────
view_memory.description    = "Zeigt die 10 neuesten Einträge der VectorDB"
view_memory.category       = "System"
memory_cleanup.description = "Bereinigt ChromaDB + personal.json (LLM-gestützt)"
memory_cleanup.category    = "Gedächtnis"

def setup(app):
    app.add_handler(CommandHandler("memory_view",    view_memory))
    app.add_handler(CommandHandler("memory_cleanup", memory_cleanup))