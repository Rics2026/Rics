"""
screensaver.py — /screen  Ambient Info-Panel für RICS
Blueprint auf demselben Flask-Server wie web_app.py.

Layout:
  Zeile 1 : Uhr · Chat-Frame · Discord-Monitor (row-span 2, hochkant)
  Zeile 2 : Wetter · Energie · (Discord-Monitor läuft weiter)
  Zeile 3 : Agenda · Geplante Jobs · News
  Zeile 4 : Spritpreise (full-width)

Endpoints:
  /screen/            — Panel-HTML
  /screen/api/data    — Wetter/Energie/Sprit/Agenda/Jobs cached (SCREENSAVER_REFRESH)
  /screen/api/chat    — Chatlog-Nachrichten, frisch (kein Cache)
  /screen/api/discord — Discord-Snapshot, gecacht 60 s
"""

import os, sys, json, time, logging, threading
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Blueprint, Response, jsonify, redirect, session
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
MEMORY_DIR  = os.path.join(PROJECT_DIR, "memory")
LOGS_DIR    = os.path.join(PROJECT_DIR, "logs")
load_dotenv(os.path.join(PROJECT_DIR, ".env"))

TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Berlin"))
BOT_NAME = os.getenv("BOT_NAME", "RICS")
WOHNORT  = os.getenv("WOHNORT", "")
REFRESH  = int(os.getenv("SCREENSAVER_REFRESH", 180))

screen_blueprint = Blueprint("screen", __name__)

# ── Haupt-Cache (Wetter/Energie/Sprit/Agenda/Jobs/News) ──────────────────
_cache: dict = {}
_cache_lock  = threading.Lock()
_last_fetch  = 0.0

# ── Discord-Cache ─────────────────────────────────────────────────────────
_discord_cache: dict = {}
_discord_lock        = threading.Lock()
_discord_last        = 0.0
DISCORD_TTL          = 60   # Sekunden


# ══════════════════════════════════════════════════════════════════════════
# DATA FETCHERS
# ══════════════════════════════════════════════════════════════════════════

def _fetch_weather() -> dict:
    token = os.getenv("WETTER_TOKEN", "").strip()
    city  = WOHNORT
    if not token or not city:
        return {}
    try:
        import requests as _r
        res = _r.get(
            "http://api.openweathermap.org/data/2.5/weather",
            params={"q": city, "appid": token, "units": "metric", "lang": "de"},
            timeout=5
        ).json()
        if str(res.get("cod")) != "200":
            return {}
        m = res["main"]
        w = res["weather"][0]
        icon_map = {
            "01": "☀️", "02": "🌤️", "03": "⛅", "04": "☁️",
            "09": "🌧️", "10": "🌦️", "11": "⛈️", "13": "❄️", "50": "🌫️"
        }
        return {
            "temp":       round(m.get("temp",       0), 1),
            "feels_like": round(m.get("feels_like", 0), 1),
            "desc":       w.get("description", "").capitalize(),
            "icon":       icon_map.get(w.get("icon", "01d")[:2], "🌡️"),
            "humidity":   m.get("humidity", 0),
            "wind":       round(res.get("wind", {}).get("speed", 0), 1),
            "pressure":   m.get("pressure", 0),
            "city":       res.get("name", city),
        }
    except Exception as e:
        logger.debug(f"[Screen] weather: {e}")
    return {}


def _fetch_energy() -> dict:
    result    = {"available": False}
    eco_ip    = os.getenv("ECOTRACKER_IP", "").strip()
    noah_user = os.getenv("GROWATT_USER",  "").strip()
    if eco_ip:
        try:
            import requests as _r
            r = _r.get(f"http://{eco_ip}/v1/json", timeout=3).json()
            result["eco_power"] = round(float(r.get("power", 0)), 1)
            result["eco_in"]    = round(r.get("energyCounterIn",  0) / 1000, 2)
            result["eco_out"]   = round(r.get("energyCounterOut", 0) / 1000, 2)
            result["available"] = True
        except Exception as e:
            logger.debug(f"[Screen] EcoTracker: {e}")
    if noah_user:
        try:
            if BASE_DIR not in sys.path:
                sys.path.insert(0, BASE_DIR)
            from solar import _fetch_noah
            d = _fetch_noah()
            if d:
                result.update({k: d[k] for k in
                    ("soc", "status", "charge", "discharge", "ppv", "pac", "today", "mode", "online")})
                result["available"] = True
        except Exception as e:
            logger.debug(f"[Screen] Noah: {e}")
    if "pac" in result and "eco_power" in result:
        result["hausverbrauch"] = round(result["pac"] + result["eco_power"], 1)
    elif "eco_power" in result:
        result["hausverbrauch"] = result["eco_power"]
    return result


def _fetch_fuel() -> dict:
    city = WOHNORT
    if not city:
        return {}
    try:
        if BASE_DIR not in sys.path:
            sys.path.insert(0, BASE_DIR)
        import auto_benzin as _ab
        api_key = _ab.API_KEY
        if api_key:
            coords = _ab.get_coordinates(city)
            if coords:
                lat, lng = coords
                import requests as _r
                res = _r.get(
                    "https://creativecommons.tankerkoenig.de/json/list.php",
                    params={"lat": lat, "lng": lng, "rad": _ab.RADIUS,
                            "type": "all", "sort": "price", "apikey": api_key},
                    timeout=10,
                ).json()
                if res.get("ok"):
                    stations = res.get("stations", [])
                    def best(key):
                        prices = sorted(
                            float(s[key]) for s in stations
                            if s.get(key) and float(s[key]) > 0.5
                        )
                        return f"{prices[0]:.3f}" if prices else "-"
                    open_s   = [s for s in stations if s.get("isOpen")]
                    cheapest = ""
                    if open_s:
                        for k in ("e5", "e10", "diesel"):
                            valid = [s for s in open_s if s.get(k) and float(s[k]) > 0.5]
                            if valid:
                                cheapest = min(valid, key=lambda x: float(x[k])).get("name", "")
                                break
                    return {
                        "e5": best("e5"), "e10": best("e10"), "diesel": best("diesel"),
                        "city": city, "count": len(stations),
                        "cheapest": cheapest, "source": "TankerKönig",
                    }
        try:
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS
            all_prices = []
            with DDGS() as ddgs:
                for q in [f"Spritpreise {city} aktuell", f"Benzinpreis {city} heute"]:
                    for r in ddgs.text(q, max_results=5):
                        all_prices.extend(_ab.extract_all_prices(r.get("body", "")))
            avg_price = _ab.avg(all_prices)
            if avg_price:
                return {"e5": "-", "e10": "-", "diesel": "-",
                        "avg": f"{avg_price:.2f}", "city": city,
                        "count": len(all_prices), "cheapest": "", "source": "Web-Schätzung"}
        except Exception as e:
            logger.debug(f"[Screen] fuel DDG: {e}")
    except Exception as e:
        logger.debug(f"[Screen] fuel: {e}")
    return {}


def _fetch_agenda() -> list:
    path = os.path.join(MEMORY_DIR, "agenda.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            items = json.load(f)
        now, result = datetime.now(tz=TIMEZONE), []
        for item in items:
            try:
                dt = datetime.fromisoformat(item.get("date", ""))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=TIMEZONE)
                if dt < now:
                    continue
                secs = (dt - now).total_seconds()
                h    = secs / 3600
                if secs < 1800:   timing, urgency = "gleich !", "urgent"
                elif h < 24:      timing, urgency = f"in {int(h)}h", "soon"
                elif h < 48:      timing, urgency = "morgen", ""
                else:             timing, urgency = dt.strftime("%d.%m."), ""
                result.append({"text": item.get("text", item.get("title", "Termin")),
                                "time": dt.strftime("%H:%M"), "timing": timing,
                                "urgency": urgency, "ts": dt.isoformat()})
            except Exception:
                pass
        result.sort(key=lambda x: x["ts"])
        return result[:5]
    except Exception as e:
        logger.debug(f"[Screen] agenda: {e}")
    return []


def _fetch_jobs() -> list:
    path = os.path.join(MEMORY_DIR, "jobs.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            jobs = json.load(f)
        result = []
        for j in jobs:
            if not isinstance(j, dict):
                continue
            cmd  = j.get("command", "?")
            t    = (f"{int(j['hour']):02d}:{int(j['minute']):02d}" if "hour" in j
                    else str(j.get("time", "?")))
            args = " ".join(str(a) for a in j.get("args", []))
            result.append({"command": f"/{cmd}" + (f" {args}" if args else ""), "time": t})
        result.sort(key=lambda x: x["time"])
        return result[:6]
    except Exception as e:
        logger.debug(f"[Screen] jobs: {e}")
    return []


def _fetch_news() -> list:
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.news("Deutschland Nachrichten aktuell", region="de-de", max_results=6))
        return [{"title": r.get("title", "")[:80], "source": r.get("source", "")} for r in results]
    except Exception as e:
        logger.debug(f"[Screen] news: {e}")
    return []


# ══════════════════════════════════════════════════════════════════════════
# CHAT FETCHER — liest logs/chatlog.json frisch (kein Cache)
# ══════════════════════════════════════════════════════════════════════════

def _fetch_chat(limit: int = 20) -> list:
    """
    Liest heutigen Tages-Log (logs/YYYY-MM-DD.log) und gibt die letzten
    N Einträge aus dem Web-Chat zurück.
    Erkennt: [WEB] USER: / [WEB] BOT: / [PUSH] RICS: / USER: / BOT:
    """
    today = datetime.now(tz=TIMEZONE).strftime("%Y-%m-%d")
    path  = os.path.join(LOGS_DIR, f"{today}.log")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = [l.rstrip("\n") for l in f if l.strip()]
        messages = []
        for line in lines:
            if line.startswith("[WEB] USER:"):
                messages.append({"role": "user",      "msg": line[11:].strip()[:200], "ts": "", "source": "web"})
            elif line.startswith("[WEB] BOT:"):
                messages.append({"role": "assistant", "msg": line[10:].strip()[:200], "ts": "", "source": "web"})
            elif line.startswith("[PUSH] RICS:"):
                messages.append({"role": "assistant", "msg": "📡 " + line[12:].strip()[:200], "ts": "", "source": "push"})
            elif line.startswith("USER:") and not line.startswith("["):
                messages.append({"role": "user",      "msg": line[5:].strip()[:200], "ts": "", "source": "tg"})
            elif line.startswith("BOT:") and not line.startswith("["):
                messages.append({"role": "assistant", "msg": line[4:].strip()[:200], "ts": "", "source": "tg"})
        return messages[-limit:]
    except Exception as e:
        logger.debug(f"[Screen] chat: {e}")
    return []


# ══════════════════════════════════════════════════════════════════════════
# DISCORD FETCHER
# ══════════════════════════════════════════════════════════════════════════

def _fetch_discord() -> dict:
    """
    Greift direkt auf den laufenden _discord_bot-Singleton zu.
    Liest außerdem immer die heutigen Discord-Logs als Fallback.
    Kein Merge in discord_manager.py nötig.
    """
    result: dict = {"available": False, "recent_activity": []}

    # ── Immer: Discord-Tages-Log lesen ───────────────────────────
    try:
        dc_log_dir = os.path.join(LOGS_DIR, "discord")
        today      = datetime.now(tz=TIMEZONE).strftime("%Y-%m-%d")
        log_path   = os.path.join(dc_log_dir, f"{today}.json")
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8") as f:
                entries = json.load(f)
            if isinstance(entries, list):
                result["recent_activity"] = [
                    {
                        "ts":      ent.get("ts", "")[:16].replace("T", " "),
                        "author":  ent.get("author", "?"),
                        "channel": ent.get("channel", "?"),
                        "msg":     ent.get("user_msg", "")[:80],
                    }
                    for ent in entries[-12:]
                ]
    except Exception as ex:
        logger.debug(f"[Screen] discord log: {ex}")

    # ── Live-Daten: direkt auf Singleton zugreifen ───────────────
    try:
        if BASE_DIR not in sys.path:
            sys.path.insert(0, BASE_DIR)
        import discord_manager as _dm

        # Singleton direkt lesen — NICHT get_discord_bot() aufrufen
        bot_inst = getattr(_dm, "_discord_bot", None)
        if not bot_inst or not getattr(bot_inst, "ready", False):
            return result

        guild = bot_inst.get_guild()
        if not guild:
            return result

        # Online-Member
        online_members = []
        try:
            for member in guild.members:
                st = str(member.status)
                if st not in ("offline", "invisible"):
                    act = None
                    if member.activity and hasattr(member.activity, "name"):
                        act = member.activity.name
                    online_members.append({
                        "name":     member.display_name,
                        "status":   st,
                        "activity": act,
                    })
        except Exception as ex:
            logger.debug(f"[Screen] discord members: {ex}")

        # Kanäle
        channels = []
        try:
            for ch in guild.channels:
                if not hasattr(ch, "name"):
                    continue
                if hasattr(ch, "voice_states"):
                    vc_members = [m.display_name for m in getattr(ch, "members", [])]
                    channels.append({"name": ch.name, "type": "voice", "members": vc_members})
                elif hasattr(ch, "topic"):
                    channels.append({"name": ch.name, "type": "text"})
            channels.sort(key=lambda c: (
                0 if (c["type"] == "voice" and c.get("members")) else
                1 if c["type"] == "voice" else 2
            ))
        except Exception as ex:
            logger.debug(f"[Screen] discord channels: {ex}")

        result.update({
            "available":      True,
            "server_name":    guild.name,
            "member_count":   guild.member_count or 0,
            "online_count":   len(online_members),
            "online_members": online_members[:12],
            "channels":       channels[:20],
        })

    except Exception as ex:
        logger.debug(f"[Screen] discord live: {ex}")

    return result


def _refresh_discord_cache():
    global _discord_last
    data = _fetch_discord()
    with _discord_lock:
        _discord_cache.clear()
        _discord_cache.update(data)
        _discord_last = time.time()


def _delayed_discord_start():
    """Wartet 8s damit der Discord-Bot ready ist, dann ersten Cache füllen."""
    time.sleep(8)
    _refresh_discord_cache()


# ══════════════════════════════════════════════════════════════════════════
# HAUPT-CACHE REFRESH
# ══════════════════════════════════════════════════════════════════════════

def _refresh_cache():
    global _last_fetch
    logger.info("[Screen] Cache wird aktualisiert...")
    data = {
        "weather": _fetch_weather(),
        "energy":  _fetch_energy(),
        "fuel":    _fetch_fuel(),
        "agenda":  _fetch_agenda(),
        "jobs":    _fetch_jobs(),
        "news":    _fetch_news(),
        "updated": datetime.now(tz=TIMEZONE).strftime("%H:%M:%S"),
        "refresh": REFRESH,
    }
    with _cache_lock:
        _cache.clear()
        _cache.update(data)
        _last_fetch = time.time()
    logger.info("[Screen] Cache aktualisiert")


# ══════════════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════════════

@screen_blueprint.route("/screen/")
@screen_blueprint.route("/screen")
def screen_index():
    if not session.get("auth"):
        return redirect("/login")
    return Response(_build_html(), mimetype="text/html; charset=utf-8")


@screen_blueprint.route("/screen/api/data")
def screen_api_data():
    if not session.get("auth"):
        return jsonify({"error": "unauthorized"}), 401
    if time.time() - _last_fetch > max(REFRESH - 15, 30):
        threading.Thread(target=_refresh_cache, daemon=True).start()
    with _cache_lock:
        return jsonify(dict(_cache))


@screen_blueprint.route("/screen/api/chat")
def screen_api_chat():
    if not session.get("auth"):
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"messages": _fetch_chat(18)})


@screen_blueprint.route("/screen/api/discord")
def screen_api_discord():
    if not session.get("auth"):
        return jsonify({"error": "unauthorized"}), 401
    # Cache leer oder abgelaufen → frisch holen
    if time.time() - _discord_last > DISCORD_TTL or not _discord_cache:
        threading.Thread(target=_refresh_discord_cache, daemon=True).start()
    with _discord_lock:
        return jsonify(dict(_discord_cache))


# ══════════════════════════════════════════════════════════════════════════
# HTML BUILD
# ══════════════════════════════════════════════════════════════════════════

def _build_html() -> str:
    initial = BOT_NAME[0].upper() if BOT_NAME else "R"
    return (
        "<!DOCTYPE html>\n<html lang='de'>\n<head>\n"
        "<meta charset='UTF-8'>\n"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>\n"
        "<title>" + BOT_NAME + " \xb7 Panel</title>\n"
        "<style>\n" + _CSS + "\n</style></head>\n<body>\n"
        + _HTML_BODY.replace("{{BOT_NAME}}", BOT_NAME).replace("{{BOT_INITIAL}}", initial)
        + "\n<script>\n" + _JS.replace("{{REFRESH}}", str(REFRESH)) + "\n</script>\n</body></html>"
    )


# ══════════════════════════════════════════════════════════════════════════
# CSS
# ══════════════════════════════════════════════════════════════════════════

_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Exo+2:wght@300;400;700;900&display=swap');
:root{
  --c:#00d4ff;--c2:#00ff88;--c3:#7c3aed;--c4:#f59e0b;
  --red:#ff4444;--orange:#fb923c;--dc:#00e676;
  --bg:#020617;--bg2:rgba(10,18,40,.92);--bg3:rgba(2,6,23,.6);
  --border:rgba(0,212,255,.18);--text:#e2e8f0;--sub:#4a5568;
  --mono:'Share Tech Mono','Courier New',monospace;
  --sans:'Exo 2',sans-serif;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{font-size:clamp(17px,1.37vw,22px)}
html,body{background:var(--bg);color:var(--text);font-family:var(--mono);min-height:100vh}
body::before{content:'';position:fixed;inset:0;z-index:0;pointer-events:none;
  background:radial-gradient(ellipse 120% 60% at 50% -10%,rgba(0,212,255,.07),transparent 60%),
             radial-gradient(ellipse 50% 70% at 5% 80%,rgba(124,58,237,.07),transparent 55%),
             radial-gradient(ellipse 40% 40% at 92% 20%,rgba(0,255,136,.04),transparent 50%)}
body::after{content:'';position:fixed;inset:0;z-index:0;pointer-events:none;
  background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,.03) 2px,rgba(0,0,0,.03) 4px)}

/* ── Outer wrap ── */
html,body{height:100%;overflow:hidden}
#panel{position:relative;z-index:1;width:100%;margin:0;
  height:100vh;box-sizing:border-box;
  padding:8px 12px 8px;display:flex;flex-direction:column;gap:8px}

/* ── Header ── */
.p-hdr{flex-shrink:0;display:flex;align-items:center;justify-content:space-between;padding:9px 16px;
  background:var(--bg2);border:1px solid rgba(0,212,255,.3);border-radius:10px;
  backdrop-filter:blur(16px);box-shadow:0 0 20px rgba(0,212,255,.05)}
.p-left{display:flex;align-items:center;gap:9px}
.p-logo{width:30px;height:30px;background:linear-gradient(135deg,var(--c),var(--c3));border-radius:7px;
  display:flex;align-items:center;justify-content:center;
  font-family:var(--sans);font-weight:900;color:#000;font-size:1.29rem;box-shadow:0 0 10px rgba(0,212,255,.3)}
.p-name{font-family:var(--sans);font-size:1.37rem;font-weight:700;color:var(--c);letter-spacing:3px}
.p-sub{font-size:0.75rem;color:var(--sub);letter-spacing:2px;margin-top:1px}
.p-dot{width:8px;height:8px;border-radius:50%;background:var(--c2);box-shadow:0 0 8px var(--c2);
  animation:dot-pulse 2.5s ease-in-out infinite;transition:background .3s,box-shadow .3s}
@keyframes dot-pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.35;transform:scale(.65)}}

/* ── Freier Canvas ── */
#canvas{flex:1;min-height:0;position:relative;overflow:hidden}

/* ── Cards: absolut positioniert, frei verschiebbar ── */
.card{
  position:absolute;
  background:var(--bg2);border:1px solid var(--border);border-radius:10px;
  padding:12px 14px;backdrop-filter:blur(16px);
  box-shadow:0 2px 12px rgba(0,0,0,.4),inset 0 1px 0 rgba(255,255,255,.03);
  display:flex;flex-direction:column;overflow:hidden;box-sizing:border-box;
  transition:box-shadow .12s,border-color .12s;
  min-width:120px;min-height:80px;
}
.card.active{
  z-index:100;
  box-shadow:0 8px 30px rgba(0,0,0,.6),0 0 20px rgba(0,212,255,.15);
  border-color:rgba(0,212,255,.4);
}

/* Drag: am Titel greifen */
.s-title{cursor:move;user-select:none}

/* ── Resize-Handle ── */
.resize-handle{
  position:absolute;right:0;bottom:0;width:18px;height:18px;
  cursor:se-resize;z-index:20;
  background:linear-gradient(135deg,transparent 45%,rgba(0,212,255,.5) 45%);
  border-radius:0 0 10px 0;
}
.resize-handle:hover{background:linear-gradient(135deg,transparent 45%,var(--c) 45%)}

/* ── Toggle-Bar ── */
#toggle-bar{
  flex-shrink:0;display:flex;flex-wrap:wrap;align-items:center;gap:6px;
  min-height:30px;padding:5px 12px;
  background:rgba(0,0,0,.4);border:1px solid rgba(255,255,255,.06);border-radius:10px;
}
#toggle-bar:empty::before{
  content:'● Alle Fenster eingeblendet';
  font-size:0.72rem;color:var(--sub);opacity:.3;font-family:var(--mono);
}
.tb-chip{display:inline-flex;align-items:center;gap:5px;padding:3px 11px;
  border-radius:20px;cursor:pointer;user-select:none;font-size:0.7rem;font-family:var(--mono);
  border:1px solid rgba(0,212,255,.3);background:rgba(0,212,255,.08);color:var(--c);
  transition:background .15s}
.tb-chip:hover{background:rgba(0,212,255,.2)}
.tb-chip-icon{font-size:0.82rem}

/* Hide-Button */
.card-hide-btn{
  margin-left:5px;flex-shrink:0;width:16px;height:16px;border-radius:4px;
  border:1px solid rgba(255,255,255,.08);background:transparent;
  color:var(--sub);font-size:0.6rem;cursor:pointer;
  display:inline-flex;align-items:center;justify-content:center;
  transition:background .15s,color .15s;padding:0;
}
.card-hide-btn:hover{background:rgba(255,68,68,.15);color:var(--red);border-color:rgba(255,68,68,.3)}
.s-title{font-size:0.75rem;font-weight:700;color:var(--sub);letter-spacing:2.5px;
  text-transform:uppercase;margin-bottom:8px;display:flex;align-items:center;gap:6px;flex-shrink:0}
.s-icon{font-size:1.11rem;opacity:.8}
.s-line{flex:1;height:1px;background:linear-gradient(90deg,var(--border),transparent)}

.empty{font-size:0.94rem;color:var(--sub);opacity:.35;text-align:center;
  padding:18px 0;flex:1;display:flex;align-items:center;justify-content:center}

/* ── Universeller scrollbarer Card-Inhalt ── */
/* Alle *-body Divs in Cards bekommen Scrollbar + flex:1 */
#wx-body,#en-body,#ag-body,#jo-body,#nw-body,#fu-body{
  flex:1;overflow-y:auto;min-height:0;
  scrollbar-width:thin;scrollbar-color:var(--border) transparent;
}
#wx-body::-webkit-scrollbar,#en-body::-webkit-scrollbar,
#ag-body::-webkit-scrollbar,#jo-body::-webkit-scrollbar,
#nw-body::-webkit-scrollbar,#fu-body::-webkit-scrollbar{width:3px}
#wx-body::-webkit-scrollbar-thumb,#en-body::-webkit-scrollbar-thumb,
#ag-body::-webkit-scrollbar-thumb,#jo-body::-webkit-scrollbar-thumb,
#nw-body::-webkit-scrollbar-thumb,#fu-body::-webkit-scrollbar-thumb{
  background:var(--border);border-radius:2px}

/* ── Clock ── */
.clock-wrap{text-align:center;padding:6px 0;flex:1;display:flex;flex-direction:column;justify-content:center}
.clock-time{font-family:var(--mono);font-size:3.54rem;font-weight:400;color:var(--c);
  letter-spacing:5px;line-height:1;text-shadow:0 0 28px rgba(0,212,255,.5)}
.clock-secs{font-size:1.66rem;color:rgba(0,212,255,.45);margin-left:3px}
.clock-date{font-family:var(--sans);font-weight:300;color:var(--sub);font-size:1.26rem;
  letter-spacing:1.2px;margin-top:6px}

/* ── Weather ── */
.wx-row{display:flex;align-items:center;gap:9px;margin-bottom:7px}
.wx-icon{font-size:2.21rem;filter:drop-shadow(0 0 5px rgba(245,158,11,.35))}
.wx-temp{font-family:var(--sans);font-size:2.21rem;font-weight:700;color:var(--c4);line-height:1;
  text-shadow:0 0 12px rgba(245,158,11,.3)}
.wx-desc{font-size:0.95rem;color:var(--text);margin-top:2px}
.wx-feels{font-size:1.08rem;color:var(--sub);margin-top:1px}
.wx-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:4px}
.wx-stat{background:rgba(0,0,0,.3);border-radius:5px;padding:4px 6px}
.wx-sl{font-size:0.92rem;color:var(--sub);letter-spacing:.8px}
.wx-sv{font-size:1.04rem;font-weight:700;color:var(--text);margin-top:1px}
.wx-city{font-size:1.04rem;color:var(--sub);margin-top:5px;opacity:.4;text-align:right}

/* ── Energy ── */
.soc-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:4px}
.soc-lbl{font-size:0.81rem;color:var(--sub)}
.soc-badge{font-size:0.75rem;font-weight:700;padding:2px 6px;border-radius:8px;
  letter-spacing:1px;text-transform:uppercase;border:1px solid}
.b-charging{color:var(--c2);border-color:var(--c2);background:rgba(0,255,136,.1)}
.b-discharging{color:var(--c);border-color:var(--c);background:rgba(0,212,255,.1)}
.b-standby{color:var(--sub);border-color:var(--sub);background:rgba(74,85,104,.1)}
.b-offline{color:var(--red);border-color:var(--red);background:rgba(255,68,68,.1)}
.soc-track{height:16px;border-radius:5px;overflow:hidden;background:rgba(0,0,0,.5);
  border:1px solid rgba(255,255,255,.06);position:relative;margin-bottom:6px}
.soc-fill{height:100%;border-radius:4px;position:absolute;left:0;top:0;transition:width 1.2s ease}
.soc-val{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  font-size:0.95rem;font-weight:700;color:#fff;text-shadow:0 1px 4px rgba(0,0,0,.9)}
.e-grid{display:grid;grid-template-columns:1fr 1fr;gap:4px}
.e-cell{background:rgba(0,0,0,.35);border-radius:5px;padding:5px 7px}
.e-lbl{font-size:1.18rem;color:var(--sub);letter-spacing:.8px;text-transform:uppercase}
.e-val{font-size:1.18rem;font-weight:700;margin-top:1px}
.col-g{color:var(--c2)}.col-c{color:var(--c)}.col-r{color:var(--red)}.col-o{color:var(--orange)}.col-y{color:var(--c4)}

/* ── Chat Frame ── */
.chat-body{flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:4px;
  scrollbar-width:thin;scrollbar-color:var(--border) transparent}
.chat-body::-webkit-scrollbar{width:3px}
.chat-body::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
.chat-msg{padding:4px 6px;border-radius:6px;border-left:2px solid transparent}
.chat-msg.user{border-left-color:var(--c);background:rgba(0,212,255,.04)}
.chat-msg.assistant{border-left-color:var(--c2);background:rgba(0,255,136,.03)}
.chat-msg.system{border-left-color:var(--sub);background:rgba(74,85,104,.05)}
.chat-meta{display:flex;align-items:center;gap:5px;margin-bottom:2px}
.chat-role{font-size:0.66rem;font-weight:700;letter-spacing:1px;text-transform:uppercase}
.chat-role.user{color:var(--c)}
.chat-role.assistant{color:var(--c2)}
.chat-role.system{color:var(--sub)}
.chat-ts{font-size:0.65rem;color:var(--sub);opacity:.45;margin-left:auto}
.chat-src{font-size:0.84rem;color:var(--sub);opacity:.3}
.chat-text{font-size:0.92rem;color:var(--text);line-height:1.35;
  word-break:break-word;white-space:pre-wrap;opacity:.85}

/* ── Discord Monitor ── */
/* Hochkant-Card bekommt overflow auf dem Scroll-Div */
#card-discord{border-color:rgba(0,230,118,.22) !important;
  background:linear-gradient(180deg,rgba(10,18,40,.95),rgba(8,12,32,.95)) !important}
.dc-scroll{flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:0;
  scrollbar-width:thin;scrollbar-color:rgba(0,230,118,.2) transparent}
.dc-scroll::-webkit-scrollbar{width:3px}
.dc-scroll::-webkit-scrollbar-thumb{background:rgba(0,230,118,.25);border-radius:2px}

.dc-server-hdr{display:flex;align-items:center;gap:7px;margin-bottom:10px}
.dc-server-name{font-family:var(--sans);font-size:1.26rem;font-weight:700;color:var(--dc);
  flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  text-shadow:0 0 14px rgba(0,230,118,.4)}
.dc-online-badge-title{font-size:0.66rem;font-weight:700;padding:2px 7px;border-radius:10px;
  letter-spacing:1px;text-transform:uppercase;flex-shrink:0;
  color:var(--c2);border:1px solid rgba(0,255,136,.3);background:rgba(0,255,136,.08)}
.dc-online-badge-title.empty{color:var(--sub);border-color:rgba(74,85,104,.3);
  background:rgba(74,85,104,.06);padding:2px 7px;font-size:0.66rem;border-radius:10px}

.dc-sep{font-size:0.65rem;font-weight:700;color:var(--sub);letter-spacing:2px;
  text-transform:uppercase;margin:8px 0 5px;padding-bottom:3px;
  border-bottom:1px solid rgba(0,230,118,.15)}

.dc-member{display:flex;align-items:center;gap:6px;padding:3px 4px;border-radius:5px;
  margin-bottom:3px;background:rgba(0,0,0,.2)}
.dc-sdot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.dc-s-online{background:#23d160;box-shadow:0 0 5px rgba(35,209,96,.5)}
.dc-s-idle{background:var(--c4);box-shadow:0 0 4px rgba(245,158,11,.4)}
.dc-s-dnd{background:var(--red);box-shadow:0 0 4px rgba(255,68,68,.4)}
.dc-s-offline{background:rgba(74,85,104,.5)}
.dc-mname{font-size:1.18rem;color:var(--text);flex:1;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.dc-mact{font-size:0.94rem;color:var(--sub);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

.dc-ch{display:flex;align-items:center;gap:4px;padding:2px 3px;border-radius:4px;margin-bottom:2px}
.dc-ch-icon{font-size:0.93rem;opacity:.5;flex-shrink:0;width:14px;text-align:center}
.dc-ch-name{font-size:0.88rem;color:var(--sub);flex:1;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.dc-ch-vc{font-size:0.77rem;color:var(--c2);flex-shrink:0;margin-left:auto}

.dc-act{padding:4px 5px;border-radius:5px;background:rgba(0,0,0,.25);
  border-left:2px solid rgba(0,230,118,.35);margin-bottom:4px}
.dc-act-top{display:flex;align-items:center;gap:4px}
.dc-act-who{font-size:0.77rem;color:var(--c);font-weight:700}
.dc-act-ch{font-size:0.93rem;color:var(--sub)}
.dc-act-ts{font-size:0.66rem;color:var(--sub);opacity:.4;margin-left:auto}
.dc-act-msg{font-size:0.88rem;color:var(--text);opacity:.75;margin-top:2px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* ── List items (Agenda / Jobs / News) ── */
.li{display:flex;align-items:flex-start;gap:6px;padding:4px 0;
  border-bottom:1px solid rgba(255,255,255,.04)}
.li:last-child{border-bottom:none}
.li-badge{flex-shrink:0;white-space:nowrap;font-size:0.77rem;padding:1px 5px;border-radius:3px;
  border:1px solid rgba(0,212,255,.22);color:var(--c);background:rgba(0,212,255,.07);margin-top:1px}
.li-badge.urgent{color:var(--red);border-color:rgba(255,68,68,.3);background:rgba(255,68,68,.07)}
.li-badge.soon{color:var(--c4);border-color:rgba(245,158,11,.3);background:rgba(245,158,11,.07)}
.li-badge.job{color:#a78bfa;border-color:rgba(124,58,237,.3);background:rgba(124,58,237,.07)}
.li-text{flex:1;font-size:0.96rem;color:var(--text);line-height:1.3}
.li-src{font-size:1.04rem;color:var(--sub);margin-top:1px}

/* ── Fuel ── */
.fuel-row{display:grid;grid-template-columns:repeat(3,1fr) 2fr;gap:10px;align-items:center}
.fuel-cell{background:rgba(0,0,0,.35);border-radius:7px;padding:10px 8px;
  text-align:center;border:1px solid rgba(255,255,255,.05)}
.fuel-type{font-size:0.77rem;color:var(--sub);letter-spacing:1px}
.fuel-price{font-family:var(--mono);font-size:1.33rem;font-weight:700;
  color:var(--c4);margin-top:4px;text-shadow:0 0 10px rgba(245,158,11,.2)}
.fuel-unit{font-size:0.93rem;color:var(--sub);margin-top:1px}
.fuel-info{padding:4px 0}
.fuel-cheapest{font-size:0.92rem;color:var(--c2);margin-bottom:4px}
.fuel-meta{font-size:0.81rem;color:var(--sub);opacity:.4}

/* ── Footer ── */
.p-ftr{flex-shrink:0;display:flex;align-items:center;justify-content:space-between;padding:7px 14px;
  background:var(--bg2);border:1px solid var(--border);border-radius:10px;
  font-size:0.84rem;color:var(--sub)}
.cd-wrap{display:flex;align-items:center;gap:7px}
.cd-val{min-width:28px;text-align:right}
.cd-track{width:80px;height:3px;background:rgba(255,255,255,.06);border-radius:2px;overflow:hidden}
.cd-fill{height:100%;border-radius:2px;background:var(--c);box-shadow:0 0 4px var(--c);
  transition:width 1s linear,background .5s}
.btn-back{padding:.35rem .9rem;background:rgba(255,68,68,.1);border:1px solid rgba(255,68,68,.3);
  color:var(--red);border-radius:8px;text-decoration:none;font-size:1.04rem;font-weight:700;
  font-family:var(--sans);letter-spacing:.5px;transition:background .2s}
.btn-back:hover{background:rgba(255,68,68,.22)}

/* ── Toggle-Bar ── */
#toggle-bar{
  flex-shrink:0;display:flex;flex-wrap:wrap;align-items:center;gap:6px;
  min-height:30px;padding:5px 12px;
  background:rgba(0,0,0,.4);border:1px solid rgba(255,255,255,.06);border-radius:10px;
}
#toggle-bar:empty::before{
  content:'● Alle Fenster eingeblendet';
  font-size:0.72rem;color:var(--sub);opacity:.3;font-family:var(--mono);
}
.tb-chip{
  display:inline-flex;align-items:center;gap:5px;
  padding:3px 11px;border-radius:20px;cursor:pointer;user-select:none;
  font-size:0.7rem;font-family:var(--mono);
  border:1px solid rgba(0,212,255,.3);background:rgba(0,212,255,.08);color:var(--c);
  transition:background .15s,transform .1s;
}
.tb-chip:hover{background:rgba(0,212,255,.2);transform:scale(1.04)}
.tb-chip-icon{font-size:0.82rem}

/* Hide-Button in Card-Titel */
.card-hide-btn{
  margin-left:5px;flex-shrink:0;width:16px;height:16px;border-radius:4px;
  border:1px solid rgba(255,255,255,.08);background:transparent;
  color:var(--sub);font-size:0.6rem;cursor:pointer;
  display:inline-flex;align-items:center;justify-content:center;
  transition:background .15s,color .15s;padding:0;line-height:1;
}
.card-hide-btn:hover{background:rgba(255,68,68,.15);color:var(--red);border-color:rgba(255,68,68,.3)}
"""


# ══════════════════════════════════════════════════════════════════════════
# HTML BODY
# ══════════════════════════════════════════════════════════════════════════

_HTML_BODY = """
<div id="panel">

  <!-- Header -->
  <div class="p-hdr">
    <div class="p-left">
      <div class="p-logo">{{BOT_INITIAL}}</div>
      <div><div class="p-name">{{BOT_NAME}}</div><div class="p-sub">AMBIENT PANEL</div></div>
    </div>
    <div style="display:flex;align-items:center;gap:10px">
      <div class="p-dot" id="live-dot"></div>
      <button onclick="resetLayout()" title="Layout zurücksetzen"
        style="font-size:.62rem;padding:3px 10px;border-radius:6px;cursor:pointer;
        border:1px solid rgba(0,212,255,.25);background:rgba(0,212,255,.07);
        color:var(--sub);font-family:var(--mono)">⟳ Reset</button>
      <a href="/" class="btn-back">&#x2190; Dashboard</a>
    </div>
  </div>

  <!-- Freier Canvas — alle Cards darin -->
  <div id="canvas">

    <div class="card" id="card-clock">
      <div class="s-title"><span class="s-icon">🕐</span>Uhr<span class="s-line"></span>
        <button class="card-hide-btn" onclick="hideCard('card-clock')">✕</button></div>
      <div class="clock-wrap">
        <div class="clock-time" id="clk-hm">--:--<span class="clock-secs">--</span></div>
        <div class="clock-date" id="clk-date">...</div>
      </div>
      <div class="resize-handle"></div>
    </div>

    <div class="card" id="card-weather">
      <div class="s-title"><span class="s-icon">🌡</span>Wetter<span class="s-line"></span>
        <button class="card-hide-btn" onclick="hideCard('card-weather')">✕</button></div>
      <div id="wx-body"><div class="empty">Keine Daten</div></div>
      <div class="resize-handle"></div>
    </div>

    <div class="card" id="card-chat">
      <div class="s-title"><span class="s-icon">💬</span>Chat<span class="s-line"></span>
        <span id="chat-upd" style="font-size:.55rem;opacity:.3;flex-shrink:0"></span>
        <button class="card-hide-btn" onclick="hideCard('card-chat')">✕</button></div>
      <div class="chat-body" id="chat-body"><div class="empty">Lade...</div></div>
      <div class="resize-handle"></div>
    </div>

    <div class="card" id="card-news">
      <div class="s-title"><span class="s-icon">📰</span>News<span class="s-line"></span>
        <button class="card-hide-btn" onclick="hideCard('card-news')">✕</button></div>
      <div id="nw-body"><div class="empty">Keine News</div></div>
      <div class="resize-handle"></div>
    </div>

    <div class="card" id="card-discord">
      <div class="s-title">
        <span class="s-icon">🎮</span>Discord
        <span class="s-line" style="background:linear-gradient(90deg,rgba(0,230,118,.25),transparent)"></span>
        <span id="dc-online-count" style="font-size:.55rem;font-weight:700;padding:2px 7px;
          border-radius:10px;letter-spacing:1px;text-transform:uppercase;flex-shrink:0;
          color:var(--sub);border:1px solid rgba(74,85,104,.3);background:rgba(74,85,104,.06)">
          ● offline</span>
        <button class="card-hide-btn" onclick="hideCard('card-discord')">✕</button>
      </div>
      <div class="dc-scroll" id="dc-body"><div class="empty">Verbinde...</div></div>
      <div class="resize-handle"></div>
    </div>

    <div class="card" id="card-energy">
      <div class="s-title"><span class="s-icon">⚡</span>Energie<span class="s-line"></span>
        <button class="card-hide-btn" onclick="hideCard('card-energy')">✕</button></div>
      <div id="en-body"><div class="empty">Nicht konfiguriert</div></div>
      <div class="resize-handle"></div>
    </div>

    <div class="card" id="card-fuel">
      <div class="s-title"><span class="s-icon">⛽</span>Sprit<span class="s-line"></span>
        <button class="card-hide-btn" onclick="hideCard('card-fuel')">✕</button></div>
      <div id="fu-body"><div class="empty">Kein API-Key</div></div>
      <div class="resize-handle"></div>
    </div>

    <div class="card" id="card-agenda">
      <div class="s-title"><span class="s-icon">📅</span>Agenda<span class="s-line"></span>
        <button class="card-hide-btn" onclick="hideCard('card-agenda')">✕</button></div>
      <div id="ag-body"><div class="empty">Keine Termine</div></div>
      <div class="resize-handle"></div>
    </div>

    <div class="card" id="card-jobs">
      <div class="s-title"><span class="s-icon">🤖</span>Jobs<span class="s-line"></span>
        <button class="card-hide-btn" onclick="hideCard('card-jobs')">✕</button></div>
      <div id="jo-body"><div class="empty">Keine Jobs</div></div>
      <div class="resize-handle"></div>
    </div>

  </div><!-- /canvas -->

  <!-- Footer -->
  <div class="p-ftr">
    <span>&#x27F3;&nbsp;<span id="upd-time">--:--:--</span></span>
    <div class="cd-wrap">
      <span class="cd-val" id="cd-num">-s</span>
      <div class="cd-track"><div class="cd-fill" id="cd-bar" style="width:100%"></div></div>
    </div>
  </div>

  <!-- Toggle-Bar -->
  <div id="toggle-bar"></div>

</div>
"""


# ══════════════════════════════════════════════════════════════════════════
# JAVASCRIPT
# ══════════════════════════════════════════════════════════════════════════

_JS = r"""
'use strict';
const REFRESH_SEC   = {{REFRESH}};
const CHAT_INTERVAL = 20;
const DC_INTERVAL   = 60;

let countdown   = REFRESH_SEC;
let chatTimer   = 0;
let dcTimer     = 0;

const DAYS   = ['Sonntag','Montag','Dienstag','Mittwoch','Donnerstag','Freitag','Samstag'];
const MONTHS = ['Januar','Februar','März','April','Mai','Juni','Juli','August',
                'September','Oktober','November','Dezember'];

function pad(n){ return String(n).padStart(2,'0'); }
function esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;')
                                     .replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

/* ─── Clock ─────────────────────────────────────────────────────────── */
function tickClock(){
  const n = new Date();
  const blink = n.getSeconds()%2===0 ? ':' : '<span style="opacity:.15">:</span>';
  document.getElementById('clk-hm').innerHTML =
    pad(n.getHours()) + blink + pad(n.getMinutes()) +
    '<span class="clock-secs">' + pad(n.getSeconds()) + '</span>';
  document.getElementById('clk-date').textContent =
    DAYS[n.getDay()] + ', ' + n.getDate() + '. ' + MONTHS[n.getMonth()] + ' ' + n.getFullYear();
}

/* ─── Countdown + Timers ─────────────────────────────────────────────── */
function tickCD(){
  countdown = Math.max(0, countdown - 1);
  chatTimer = Math.max(0, chatTimer  - 1);
  dcTimer   = Math.max(0, dcTimer    - 1);
  if (countdown === 0){ countdown = REFRESH_SEC; fetchMain();    }
  if (chatTimer === 0){ chatTimer = CHAT_INTERVAL; fetchChat();  }
  if (dcTimer   === 0){ dcTimer   = DC_INTERVAL;   fetchDiscord();}

  document.getElementById('cd-num').textContent = countdown + 's';
  const pct = (countdown / REFRESH_SEC) * 100;
  const bar = document.getElementById('cd-bar');
  bar.style.width      = pct + '%';
  bar.style.background = pct<15 ? 'var(--red)' : pct<40 ? 'var(--orange)' : 'var(--c)';
}

/* ─── Status-Dot ─────────────────────────────────────────────────────── */
function dot(s){
  const d = document.getElementById('live-dot'); if(!d) return;
  const m = { ok:['var(--c2)','0 0 8px var(--c2)'],
               busy:['var(--c4)','0 0 8px var(--c4)'],
               err:['var(--red)','0 0 8px var(--red)'] };
  const [bg,sh] = m[s]||m.ok;
  d.style.background = bg; d.style.boxShadow = sh;
}

/* ─── Render: Wetter ─────────────────────────────────────────────────── */
function renderWeather(w){
  const el = document.getElementById('wx-body');
  if(!w||!w.temp){ el.innerHTML='<div class="empty">Keine Daten</div>'; return; }
  el.innerHTML = `
    <div class="wx-row">
      <div class="wx-icon">${esc(w.icon)}</div>
      <div>
        <div class="wx-temp">${w.temp}°C</div>
        <div class="wx-desc">${esc(w.desc)}</div>
        <div class="wx-feels">Gefühlt ${w.feels_like}°C</div>
      </div>
    </div>
    <div class="wx-stats">
      <div class="wx-stat"><div class="wx-sl">Feuchte</div><div class="wx-sv">💧${w.humidity}%</div></div>
      <div class="wx-stat"><div class="wx-sl">Wind</div><div class="wx-sv">💨${w.wind}m/s</div></div>
      <div class="wx-stat"><div class="wx-sl">Druck</div><div class="wx-sv">🔵${w.pressure}</div></div>
    </div>
    <div class="wx-city">📍${esc(w.city)}</div>`;
}

/* ─── Render: Energie ────────────────────────────────────────────────── */
function socGrad(s){
  return s>=70 ? 'linear-gradient(90deg,#00ff88,#00d4ff)'
       : s>=35 ? 'linear-gradient(90deg,#f59e0b,#00ff88)'
               : 'linear-gradient(90deg,#ff4444,#f59e0b)';
}
function ecell(l,v,c){
  return `<div class="e-cell"><div class="e-lbl">${l}</div><div class="e-val ${c}">${v}</div></div>`;
}
function renderEnergy(e){
  const el = document.getElementById('en-body');
  if(!e||!e.available){ el.innerHTML='<div class="empty">Nicht konfiguriert</div>'; return; }
  let h = '';
  if(e.soc !== undefined){
    const bc = e.status==='Lädt'    ? 'b-charging'
             : e.status==='Entlädt' ? 'b-discharging'
             : e.status==='Offline' ? 'b-offline' : 'b-standby';
    h += `<div class="soc-hdr">
            <span class="soc-lbl">🔋 Noah 2000</span>
            <span class="soc-badge ${bc}">${esc(e.status)}</span>
          </div>
          <div class="soc-track">
            <div class="soc-fill" style="width:${e.soc}%;background:${socGrad(e.soc)}"></div>
            <div class="soc-val">${Math.round(e.soc)}%</div>
          </div>`;
  }
  h += '<div class="e-grid">';
  if(e.ppv           !== undefined) h += ecell('☀️ Solar',      e.ppv  + ' W', 'col-y');
  if(e.pac           !== undefined) h += ecell('🔌 Ausgang',    e.pac  + ' W', 'col-c');
  if(e.eco_power     !== undefined) h += ecell(
    e.eco_power < 0 ? '📤 Einspeis.' : '📥 Netzbezug',
    Math.abs(e.eco_power) + ' W',
    e.eco_power < -10 ? 'col-g' : e.eco_power > 200 ? 'col-r' : 'col-c');
  if(e.hausverbrauch !== undefined) h += ecell('🏠 Verbrauch', Math.round(e.hausverbrauch) + ' W', 'col-o');
  if(e.today         !== undefined) h += ecell('📊 Heute',     e.today.toFixed(2) + ' kWh', 'col-c');
  if(e.charge   > 0) h += ecell('⬆️ Laden',    e.charge    + ' W', 'col-g');
  else if(e.discharge > 0) h += ecell('⬇️ Entladen', e.discharge + ' W', 'col-r');
  h += '</div>';
  el.innerHTML = h;
}

/* ─── Render: Agenda ─────────────────────────────────────────────────── */
function renderAgenda(items){
  const el = document.getElementById('ag-body');
  if(!items||!items.length){ el.innerHTML='<div class="empty">Keine Termine</div>'; return; }
  el.innerHTML = items.map(a =>
    `<div class="li">
       <span class="li-badge ${esc(a.urgency||'')}">${esc(a.timing)}</span>
       <div class="li-text">${esc(a.time)} · ${esc(a.text)}</div>
     </div>`
  ).join('');
}

/* ─── Render: Jobs ───────────────────────────────────────────────────── */
function renderJobs(jobs){
  const el = document.getElementById('jo-body');
  if(!jobs||!jobs.length){ el.innerHTML='<div class="empty">Keine Jobs</div>'; return; }
  el.innerHTML = jobs.map(j =>
    `<div class="li">
       <span class="li-badge job">${esc(j.time)}</span>
       <div class="li-text" style="color:var(--sub)">${esc(j.command)}</div>
     </div>`
  ).join('');
}

/* ─── Render: News ───────────────────────────────────────────────────── */
function renderNews(items){
  const el = document.getElementById('nw-body');
  if(!items||!items.length){ el.innerHTML='<div class="empty">Keine News</div>'; return; }
  el.innerHTML = items.map(n =>
    `<div class="li">
       <div>
         <div class="li-text">${esc(n.title)}</div>
         <div class="li-src">${esc(n.source)}</div>
       </div>
     </div>`
  ).join('');
}

/* ─── Render: Sprit ──────────────────────────────────────────────────── */
function renderFuel(f){
  const el = document.getElementById('fu-body');
  if(!f||(!f.e5&&!f.avg)){ el.innerHTML='<div class="empty">Keine Daten verfügbar</div>'; return; }
  const src = f.source
    ? `<span style="color:var(--sub);font-size:.5rem;opacity:.5"> · ${esc(f.source)}</span>` : '';
  if(f.avg){
    el.innerHTML = `<div class="fuel-row">
      <div class="fuel-cell" style="grid-column:1/4">
        <div class="fuel-type">Ø Durchschnitt</div>
        <div class="fuel-price">~${esc(f.avg)}</div>
        <div class="fuel-unit">€/L (Web-Schätzung)</div>
      </div>
      <div class="fuel-info">
        <div class="fuel-meta">📍 ${esc(f.city)}${src}</div>
      </div>
    </div>`;
    return;
  }
  el.innerHTML = `<div class="fuel-row">
    <div class="fuel-cell"><div class="fuel-type">E5</div><div class="fuel-price">${esc(f.e5)}</div><div class="fuel-unit">€/L</div></div>
    <div class="fuel-cell"><div class="fuel-type">E10</div><div class="fuel-price">${esc(f.e10)}</div><div class="fuel-unit">€/L</div></div>
    <div class="fuel-cell"><div class="fuel-type">Diesel</div><div class="fuel-price">${esc(f.diesel)}</div><div class="fuel-unit">€/L</div></div>
    <div class="fuel-info">
      ${f.cheapest ? `<div class="fuel-cheapest">💚 ${esc(f.cheapest)}</div>` : ''}
      <div class="fuel-meta">📍 ${esc(f.city)} · ${f.count} Stationen · 5 km${src}</div>
    </div>
  </div>`;
}

/* ─── Render: Chat ───────────────────────────────────────────────────── */
function renderChat(msgs){
  const el  = document.getElementById('chat-body');
  const upd = document.getElementById('chat-upd');
  if(!msgs||!msgs.length){ el.innerHTML='<div class="empty">Kein Chat</div>'; return; }
  const icon = { user:'👤', assistant:'🤖', system:'⚙️' };
  el.innerHTML = msgs.map(m => {
    const role = m.role || 'system';
    const src  = m.source ? `<span class="chat-src">[${esc(m.source)}]</span>` : '';
    return `<div class="chat-msg ${esc(role)}">
      <div class="chat-meta">
        <span class="chat-role ${esc(role)}">${icon[role]||'•'} ${esc(role)}</span>
        ${src}
        <span class="chat-ts">${esc(m.ts)}</span>
      </div>
      <div class="chat-text">${esc(m.msg)}</div>
    </div>`;
  }).join('');
  el.scrollTop = el.scrollHeight;   // immer ans Ende scrollen
  if(upd) upd.textContent = new Date().toLocaleTimeString('de-DE',{hour:'2-digit',minute:'2-digit'});
}

/* ─── Render: Discord ────────────────────────────────────────────────── */
function dcDot(status){
  const map = { online:'dc-s-online', idle:'dc-s-idle', dnd:'dc-s-dnd', do_not_disturb:'dc-s-dnd' };
  return map[status] || 'dc-s-offline';
}

function renderDiscord(d){
  const el    = document.getElementById('dc-body');
  const badge = document.getElementById('dc-online-count');
  let h = '';

  const ocnt = (d && d.online_count != null) ? d.online_count : 0;

  /* Online-Badge oben rechts im Titel-Bar */
  if(badge){
    if(d && d.available){
      badge.style.color      = 'var(--c2)';
      badge.style.border     = '1px solid rgba(0,255,136,.3)';
      badge.style.background = 'rgba(0,255,136,.08)';
      badge.textContent      = '● ' + ocnt + ' online';
    } else {
      badge.style.color      = 'var(--sub)';
      badge.style.border     = '1px solid rgba(74,85,104,.3)';
      badge.style.background = 'rgba(74,85,104,.06)';
      badge.textContent      = '● offline';
    }
  }

  if(!d || !d.available){
    const acts = d && d.recent_activity;
    if(acts && acts.length){
      h += `<div class="dc-sep">Letzte Aktivität</div>`;
      h += acts.map(a => `
        <div class="dc-act">
          <div class="dc-act-top">
            <span class="dc-act-who">${esc(a.author)}</span>
            <span class="dc-act-ch">#${esc(a.channel)}</span>
            <span class="dc-act-ts">${esc((a.ts||'').slice(11,16))}</span>
          </div>
          <div class="dc-act-msg">${esc(a.msg)}</div>
        </div>`).join('');
    } else {
      h += `<div class="empty" style="margin-top:20px">Keine Daten</div>`;
    }
    el.innerHTML = h; return;
  }

  /* Servername */
  h += `<div class="dc-server-hdr">
          <span class="dc-server-name">${esc(d.server_name||'Server')}</span>
        </div>`;

  /* Online-Member */
  const online = d.online_members || [];
  if(online.length){
    h += `<div class="dc-sep">Online (${online.length})</div>`;
    h += online.map(m => `
      <div class="dc-member">
        <div class="dc-sdot ${dcDot(m.status)}"></div>
        <div class="dc-mname">${esc(m.name)}</div>
        ${m.activity ? `<div class="dc-mact">${esc(m.activity.slice(0,20))}</div>` : ''}
      </div>`).join('');
  }

  /* Kanäle */
  const chs = d.channels || [];
  if(chs.length){
    h += `<div class="dc-sep">Kanäle</div>`;
    h += chs.map(ch => {
      const icon = ch.type === 'voice' ? '🔊' : '#';
      const vcUsers = (ch.type==='voice' && ch.members && ch.members.length)
        ? `<span class="dc-ch-vc">👥${ch.members.length}</span>` : '';
      return `<div class="dc-ch">
        <span class="dc-ch-icon">${icon}</span>
        <span class="dc-ch-name">${esc(ch.name)}</span>
        ${vcUsers}
      </div>`;
    }).join('');
  }

  /* Letzte Nachrichten */
  const acts = d.recent_activity || [];
  if(acts.length){
    h += `<div class="dc-sep" style="margin-top:10px">Letzte Nachrichten</div>`;
    h += acts.map(a => `
      <div class="dc-act">
        <div class="dc-act-top">
          <span class="dc-act-who">${esc(a.author)}</span>
          <span class="dc-act-ch">#${esc(a.channel)}</span>
          <span class="dc-act-ts">${esc((a.ts||'').slice(11,16))}</span>
        </div>
        <div class="dc-act-msg">${esc(a.msg)}</div>
      </div>`).join('');
  }

  el.innerHTML = h;
}

/* ─── Fetch-Funktionen ───────────────────────────────────────────────── */
async function fetchMain(){
  dot('busy');
  try{
    const r = await fetch('/screen/api/data');
    if(!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    renderWeather(d.weather || {});
    renderEnergy(d.energy   || {});
    renderAgenda(d.agenda   || []);
    renderJobs(d.jobs       || []);
    renderNews(d.news       || []);
    renderFuel(d.fuel       || {});
    if(d.updated) document.getElementById('upd-time').textContent = d.updated;
    dot('ok'); countdown = REFRESH_SEC;
  } catch(e){ console.warn('[Screen/main]', e); dot('err'); }
}

async function fetchChat(){
  try{
    const r = await fetch('/screen/api/chat');
    if(!r.ok) return;
    const d = await r.json();
    renderChat(d.messages || []);
  } catch(e){ console.warn('[Screen/chat]', e); }
}

async function fetchDiscord(){
  try{
    const r = await fetch('/screen/api/discord');
    if(!r.ok) return;
    const d = await r.json();
    renderDiscord(d);
  } catch(e){ console.warn('[Screen/discord]', e); }
}

/* ─── Freies Canvas: Drag & Resize ──────────────────────────────────── */
(function(){
  const STORE  = 'rics_layout_v3';
  const STORE_HIDE = 'rics_hidden_v3';

  const CARD_META = {
    'card-clock':   {icon:'🕐', label:'Uhr'},
    'card-weather': {icon:'🌡', label:'Wetter'},
    'card-chat':    {icon:'💬', label:'Chat'},
    'card-news':    {icon:'📰', label:'News'},
    'card-discord': {icon:'🎮', label:'Discord'},
    'card-energy':  {icon:'⚡', label:'Energie'},
    'card-fuel':    {icon:'⛽', label:'Sprit'},
    'card-agenda':  {icon:'📅', label:'Agenda'},
    'card-jobs':    {icon:'🤖', label:'Jobs'},
  };

  // Standard-Layout: Prozent des Canvas (0–100)
  function defaultLayout() {
    const W = 100, H = 100;
    const cw = W/3, rh = H/5;
    return {
      'card-clock':   {x:0,      y:0,     w:cw,   h:rh},
      'card-weather': {x:0,      y:rh,    w:cw,   h:rh*3},
      'card-chat':    {x:cw,     y:0,     w:cw,   h:rh*2},
      'card-news':    {x:cw,     y:rh*2,  w:cw,   h:rh*2},
      'card-discord': {x:cw*2,   y:0,     w:cw,   h:rh*2},
      'card-energy':  {x:cw*2,   y:rh*2,  w:cw,   h:rh*2},
      'card-fuel':    {x:0,      y:rh*4,  w:cw,   h:rh},
      'card-agenda':  {x:cw,     y:rh*4,  w:cw,   h:rh},
      'card-jobs':    {x:cw*2,   y:rh*4,  w:cw,   h:rh},
    };
  }

  function loadLayout(){
    try{
      const s=localStorage.getItem(STORE);
      return s ? JSON.parse(s) : defaultLayout();
    }catch(e){return defaultLayout();}
  }
  function saveLayout(lay){
    localStorage.setItem(STORE, JSON.stringify(lay));
  }

  function applyLayout(lay){
    const canvas = document.getElementById('canvas');
    const W = canvas.offsetWidth, H = canvas.offsetHeight;
    Object.entries(lay).forEach(([id,p])=>{
      const el = document.getElementById(id);
      if(!el) return;
      el.style.left   = (p.x/100*W)+'px';
      el.style.top    = (p.y/100*H)+'px';
      el.style.width  = (p.w/100*W)+'px';
      el.style.height = (p.h/100*H)+'px';
    });
  }

  let layout = loadLayout();

  function init(){
    applyLayout(layout);
    applyHidden();
    initInteractions();
  }

  // ── Hide / Show ──────────────────────────────────────────────────
  function getHidden(){try{return JSON.parse(localStorage.getItem(STORE_HIDE)||'[]');}catch(e){return[];}}
  function saveHidden(a){localStorage.setItem(STORE_HIDE,JSON.stringify(a));}

  window.hideCard = function(id){
    const el=document.getElementById(id); if(el) el.style.display='none';
    const h=getHidden(); if(!h.includes(id)) h.push(id); saveHidden(h); renderBar();
  };
  window.showCard = function(id){
    const el=document.getElementById(id); if(el) el.style.display='';
    saveHidden(getHidden().filter(x=>x!==id)); renderBar();
  };
  function applyHidden(){getHidden().forEach(id=>{const el=document.getElementById(id);if(el)el.style.display='none';}); renderBar();}
  function renderBar(){
    const bar=document.getElementById('toggle-bar'); if(!bar) return;
    bar.innerHTML='';
    getHidden().forEach(id=>{
      const m=CARD_META[id]||{icon:'◻',label:id};
      const chip=document.createElement('div');
      chip.className='tb-chip'; chip.title='Einblenden';
      chip.innerHTML=`<span class="tb-chip-icon">${m.icon}</span>${m.label}`;
      chip.onclick=()=>showCard(id); bar.appendChild(chip);
    });
  }

  // ── Reset ────────────────────────────────────────────────────────
  window.resetLayout = function(){
    localStorage.removeItem(STORE); localStorage.removeItem(STORE_HIDE); location.reload();
  };

  // ── Drag & Resize ────────────────────────────────────────────────
  function initInteractions(){
    const canvas = document.getElementById('canvas');
    let action=null; // {type:'drag'|'resize', id, startX, startY, startL, startT, startW, startH}

    canvas.querySelectorAll('.card').forEach(card=>{
      const id = card.id;

      // Drag: an s-title
      const title = card.querySelector('.s-title');
      if(title){
        title.addEventListener('mousedown', function(e){
          if(e.target.classList.contains('card-hide-btn')) return;
          e.preventDefault();
          bringToFront(card);
          action={type:'drag', id,
            startX:e.clientX, startY:e.clientY,
            startL:parseFloat(card.style.left)||0,
            startT:parseFloat(card.style.top)||0};
        });
      }

      // Resize: an .resize-handle
      const rh = card.querySelector('.resize-handle');
      if(rh){
        rh.addEventListener('mousedown', function(e){
          e.preventDefault(); e.stopPropagation();
          bringToFront(card);
          action={type:'resize', id,
            startX:e.clientX, startY:e.clientY,
            startW:parseFloat(card.style.width)||200,
            startH:parseFloat(card.style.height)||150};
        });
      }

      // Vorne bringen beim Klick
      card.addEventListener('mousedown', function(){ bringToFront(card); });
    });

    document.addEventListener('mousemove', function(e){
      if(!action) return;
      const canvas = document.getElementById('canvas');
      const W=canvas.offsetWidth, H=canvas.offsetHeight;
      const el = document.getElementById(action.id);
      if(!el) return;

      if(action.type==='drag'){
        const dx=e.clientX-action.startX, dy=e.clientY-action.startY;
        const newL = Math.max(0, Math.min(W - el.offsetWidth,  action.startL+dx));
        const newT = Math.max(0, Math.min(H - el.offsetHeight, action.startT+dy));
        el.style.left = newL+'px';
        el.style.top  = newT+'px';
      } else {
        const dx=e.clientX-action.startX, dy=e.clientY-action.startY;
        const maxW = W - parseFloat(el.style.left||0);
        const maxH = H - parseFloat(el.style.top||0);
        el.style.width  = Math.max(120, Math.min(maxW, action.startW+dx))+'px';
        el.style.height = Math.max(80,  Math.min(maxH, action.startH+dy))+'px';
      }
    });

    document.addEventListener('mouseup', function(e){
      if(!action) return;
      const canvas = document.getElementById('canvas');
      const W=canvas.offsetWidth, H=canvas.offsetHeight;
      const el = document.getElementById(action.id);
      if(el && layout[action.id]){
        layout[action.id].x = parseFloat(el.style.left)/W*100;
        layout[action.id].y = parseFloat(el.style.top)/H*100;
        layout[action.id].w = parseFloat(el.style.width)/W*100;
        layout[action.id].h = parseFloat(el.style.height)/H*100;
        saveLayout(layout);
      }
      action=null;
    });
  }

  let zTop=10;
  function bringToFront(el){
    zTop++; el.style.zIndex=zTop;
  }

  // Canvas-Größe ändert sich → Layout neu anwenden
  window.addEventListener('resize', ()=>{ layout=loadLayout(); applyLayout(layout); });

  // Start
  document.addEventListener('DOMContentLoaded', init);
  // Fallback falls DOMContentLoaded schon durch
  if(document.readyState!=='loading') init();
})();

/* ─── Init ───────────────────────────────────────────────────────────── */
setInterval(tickClock, 1000);
setInterval(tickCD,    1000);
tickClock();
fetchMain();
fetchChat();
fetchDiscord();
chatTimer = CHAT_INTERVAL;
dcTimer   = DC_INTERVAL;
"""


def setup(app=None):
    threading.Thread(target=_refresh_cache,          daemon=True).start()
    threading.Thread(target=_delayed_discord_start,  daemon=True).start()
    logger.info("[Screen] Ambient Panel bereit: /screen/")
    print("🖥️  Ambient Panel: /screen/  (selber Server wie Dashboard)")