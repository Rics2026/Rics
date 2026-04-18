import os
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from core.session_manager import SessionManager
from core.event_bus import EventBus

load_dotenv()

BRAIN_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "../memory/brain_log.json")
CHATLOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "../logs/chatlog.json")
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Berlin"))


class Brain:
    def __init__(self, event_bus: EventBus = None, session_manager: SessionManager = None):
        self.event_bus = event_bus or EventBus()
        self.session_manager = session_manager or SessionManager()
        self.vector_db = {}

        self.event_bus.subscribe("SYSTEM_EVENT", lambda data: self._async_wrap(self.handle_event(data)))
        self.event_bus.subscribe("CHAT_MESSAGE", lambda data: self._async_wrap(self.handle_chat(data)))

    # ----------------- ZEIT-HILFE -----------------
    def get_current_datetime(self):
        return datetime.now(TIMEZONE)

    def get_now(self):
        return self.get_current_datetime()

    def _async_wrap(self, coro):
        import asyncio
        asyncio.create_task(coro)

    # ----------------- EVENT HANDLER -----------------
    async def handle_event(self, data):
        await self.log_brain_event("SYSTEM_EVENT", data)

    async def handle_chat(self, data):
        # Brain only observes and logs — personal learning is handled by Jarvis.learn_from_message()
        await self.log_chat(data)

    # ----------------- VECTOR DB -----------------
    def vector_db_add(self, text):
        key = f"entry_{len(self.vector_db)+1}"
        self.vector_db[key] = text

    # ----------------- LOGGING -----------------
    async def log_brain_event(self, event_type, data):
        logs = []
        if os.path.exists(BRAIN_LOG_FILE):
            try:
                with open(BRAIN_LOG_FILE, "r", encoding="utf-8") as f:
                    logs = json.load(f)
            except:
                pass

        logs.append({
            "timestamp": self.get_now().isoformat(),
            "type": event_type,
            "data": data
        })

        if len(logs) > 1000:
            logs = logs[-1000:]

        with open(BRAIN_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(logs, f, indent=4, ensure_ascii=False)

    async def log_chat(self, data):
        logs = []
        if os.path.exists(CHATLOG_FILE):
            try:
                with open(CHATLOG_FILE, "r", encoding="utf-8") as f:
                    logs = json.load(f)
            except:
                pass

        logs.append({
            "timestamp": self.get_now().isoformat(),
            "role": data.get("role", "user"),
            "message": data.get("content")
        })

        if len(logs) > 5000:
            logs = logs[-5000:]

        with open(CHATLOG_FILE, "w", encoding="utf-8") as f:
            json.dump(logs, f, indent=4, ensure_ascii=False)

    # ----------------- HISTORISCHE ABFRAGEN -----------------
    def get_historical(self, query: str):
        if not os.path.exists(BRAIN_LOG_FILE):
            return "KEINE DATEN"

        try:
            with open(BRAIN_LOG_FILE, "r", encoding="utf-8") as f:
                logs = json.load(f)
        except:
            return "KEINE DATEN"

        now = self.get_now()
        query_l = query.lower()
        target_date = None
        target_time = None

        if "gestern" in query_l:
            target_date = now - timedelta(days=1)
        elif "heute" in query_l:
            target_date = now

        time_match = re.search(r"(\d{1,2})([:h]?)(\d{0,2})?", query_l)
        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(3)) if time_match.group(3) else 0
            target_time = (hour, minute)

        rel_match = re.search(r"vor (\d+) stunden?", query_l)
        if rel_match:
            delta = timedelta(hours=int(rel_match.group(1)))
            t = now - delta
            target_date = t
            target_time = (t.hour, t.minute)

        rel_match = re.search(r"vor (\d+) minuten?", query_l)
        if rel_match:
            delta = timedelta(minutes=int(rel_match.group(1)))
            t = now - delta
            target_date = t
            target_time = (t.hour, t.minute)

        # Schlüsselwort-Mapping: Nutzerfragen → brain_log Felder
        TOPIC_MAP = {
            "solar":      ["solar", "strom", "einspeisung", "netzbezug", "watt", "anlage"],
            "wetter":     ["wetter", "temperatur", "regen", "wind", "grad"],
            "benzin":     ["benzin", "sprit", "tanken", "diesel", "e5", "e10"],
            "mood":       ["stimmung", "mood", "laune"],
            "top_interests": ["interessen", "interesse", "topics"],
            "data":       ["ram", "cpu", "disk", "speicher", "prozessor"],
        }

        def match_topic(q):
            for field, keywords in TOPIC_MAP.items():
                if any(kw in q for kw in keywords):
                    return field
            return None

        matched_field = match_topic(query_l)

        candidates = []
        for entry in reversed(logs):
            try:
                timestamp = datetime.fromisoformat(entry["timestamp"]).astimezone(TIMEZONE)
            except:
                continue

            if target_date and timestamp.date() != target_date.date():
                continue
            if target_time and (timestamp.hour, timestamp.minute) != target_time:
                continue

            ts_str = timestamp.strftime('%H:%M')

            # Top-Level Felder (solar, wetter, benzin, mood, top_interests)
            if matched_field and matched_field in entry:
                val = entry[matched_field]
                if isinstance(val, dict):
                    summary = ", ".join(f"{k}: {v}" for k, v in val.items())
                    candidates.append(f"{matched_field} ({ts_str}): {summary}")
                else:
                    candidates.append(f"{matched_field} ({ts_str}): {val}")

            # System-Daten (data-Dict)
            elif matched_field == "data" or not matched_field:
                data = entry.get("data", {})
                for key, value in data.items():
                    key_l = key.lower()
                    if key_l in query_l or query_l in key_l:
                        candidates.append(f"{key}: {value} ({ts_str})")

            if candidates:
                break

        if not candidates:
            # Letzten Eintrag als Fallback — alles was da ist
            if logs:
                last = logs[-1]
                try:
                    ts = datetime.fromisoformat(last["timestamp"]).astimezone(TIMEZONE).strftime('%H:%M')
                except:
                    ts = "?"
                parts = []
                for field in ["solar", "wetter", "benzin", "mood", "top_interests"]:
                    if field in last:
                        val = last[field]
                        if isinstance(val, dict):
                            parts.append(f"{field}: " + ", ".join(f"{k}={v}" for k, v in val.items()))
                        else:
                            parts.append(f"{field}: {val}")
                if parts:
                    return f"Letzter Log ({ts}): " + " | ".join(parts)
            return "KEINE DATEN"

        return candidates[0]