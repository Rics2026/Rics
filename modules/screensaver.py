"""
screensaver.py — /screen  Ambient Info-Panel für RICS
Blueprint auf demselben Flask-Server wie web_app.py.

2 Zeilen in web_app.py (nach orch_blueprint):
    from screensaver import screen_blueprint
    app.register_blueprint(screen_blueprint)

Layout: 3 Karten pro Zeile
  Zeile 1: Uhr · Wetter · Energie
  Zeile 2: Agenda · Jobs · News
  Zeile 3: Spritpreise (full-width)

Aufruf: /screen/
Refresh: SCREENSAVER_REFRESH (default 180 s)
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
load_dotenv(os.path.join(PROJECT_DIR, ".env"))

TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Berlin"))
BOT_NAME = os.getenv("BOT_NAME", "RICS")
WOHNORT  = os.getenv("WOHNORT", "")
REFRESH  = int(os.getenv("SCREENSAVER_REFRESH", 180))

screen_blueprint = Blueprint("screen", __name__)

_cache: dict = {}
_cache_lock  = threading.Lock()
_last_fetch  = 0.0


# ── Data Fetchers ─────────────────────────────────────────────────────────

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
                result.update({k: d[k] for k in ("soc","status","charge","discharge","ppv","pac","today","mode","online")})
                result["available"] = True
        except Exception as e:
            logger.debug(f"[Screen] Noah: {e}")
    if "pac" in result and "eco_power" in result:
        result["hausverbrauch"] = round(result["pac"] + result["eco_power"], 1)
    elif "eco_power" in result:
        result["hausverbrauch"] = result["eco_power"]
    return result


def _fetch_fuel() -> dict:
    """Nutzt auto_benzin.py — TankerKönig API mit DDG-Fallback."""
    city = WOHNORT
    if not city:
        return {}
    try:
        if BASE_DIR not in sys.path:
            sys.path.insert(0, BASE_DIR)
        import auto_benzin as _ab

        api_key = _ab.API_KEY

        # ── Mit TankerKönig API ───────────────────────────────
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

        # ── DDG-Fallback (kein API-Key) ───────────────────────
        try:
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS
            all_prices = []
            queries = [
                f"Spritpreise {city} aktuell",
                f"Benzinpreis {city} heute",
                f"Dieselpreis {city} aktuell",
            ]
            with DDGS() as ddgs:
                for q in queries:
                    for r in ddgs.text(q, max_results=5):
                        all_prices.extend(_ab.extract_all_prices(r.get("body", "")))
            avg_price = _ab.avg(all_prices)
            if avg_price:
                return {
                    "e5": "-", "e10": "-",
                    "diesel": "-",
                    "avg": f"{avg_price:.2f}",
                    "city": city, "count": len(all_prices),
                    "cheapest": "", "source": "Web-Schätzung",
                }
        except Exception as e:
            logger.debug(f"[Screen] fuel DDG fallback: {e}")

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
                result.append({"text": item.get("task", item.get("text", item.get("title", "Termin"))),
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
            cmd = j.get("command", "?")
            t   = (f"{int(j['hour']):02d}:{int(j['minute']):02d}" if "hour" in j
                   else str(j.get("time", "?")))
            args = " ".join(str(a) for a in j.get("args", []))
            result.append({"command": f"/{cmd}" + (f" {args}" if args else ""), "time": t})
        result.sort(key=lambda x: x["time"])
        return result
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


# ── Routes ────────────────────────────────────────────────────────────────

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


# ── HTML ─────────────────────────────────────────────────────────────────

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


_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Exo+2:wght@300;400;700;900&display=swap');
:root{
  --c:#00d4ff;--c2:#00ff88;--c3:#7c3aed;--c4:#f59e0b;
  --red:#ff4444;--orange:#fb923c;
  --bg:#020617;--bg2:rgba(10,18,40,.92);
  --border:rgba(0,212,255,.18);--text:#e2e8f0;--sub:#64748b;
  --mono:'Share Tech Mono','Courier New',monospace;
  --sans:'Exo 2',sans-serif;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{background:var(--bg);color:var(--text);font-family:var(--mono);min-height:100vh}
body::before{content:'';position:fixed;inset:0;z-index:0;pointer-events:none;
  background:radial-gradient(ellipse 120% 60% at 50% -10%,rgba(0,212,255,.07),transparent 60%),
             radial-gradient(ellipse 50% 70% at 5% 80%,rgba(124,58,237,.07),transparent 55%),
             radial-gradient(ellipse 40% 40% at 92% 20%,rgba(0,255,136,.04),transparent 50%)}
body::after{content:'';position:fixed;inset:0;z-index:0;pointer-events:none;
  background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,.03) 2px,rgba(0,0,0,.03) 4px)}
#panel{position:relative;z-index:1;max-width:1400px;margin:0 auto;padding:12px 16px 32px;display:flex;flex-direction:column;gap:10px}
.p-hdr{display:flex;align-items:center;justify-content:space-between;padding:12px 18px;
  background:var(--bg2);border:1px solid rgba(0,212,255,.3);border-radius:12px;
  backdrop-filter:blur(16px);box-shadow:0 0 20px rgba(0,212,255,.05)}
.p-left{display:flex;align-items:center;gap:11px}
.p-logo{width:38px;height:38px;background:linear-gradient(135deg,var(--c),var(--c3));border-radius:9px;
  display:flex;align-items:center;justify-content:center;
  font-family:var(--sans);font-weight:900;color:#000;font-size:1.05rem;box-shadow:0 0 12px rgba(0,212,255,.3)}
.p-name{font-family:var(--sans);font-size:1.1rem;font-weight:700;color:var(--c);letter-spacing:3px}
.p-sub{font-size:.58rem;color:var(--sub);letter-spacing:2px;margin-top:1px}
.p-dot{width:10px;height:10px;border-radius:50%;background:var(--c2);box-shadow:0 0 10px var(--c2);
  animation:dot-pulse 2.5s ease-in-out infinite;transition:background .3s,box-shadow .3s}
@keyframes dot-pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.35;transform:scale(.65)}}
.btn-back{padding:.45rem 1.1rem;background:rgba(255,68,68,.1);border:1px solid rgba(255,68,68,.3);
  color:var(--red);border-radius:8px;cursor:pointer;font-size:.82rem;font-weight:700;
  font-family:var(--sans);letter-spacing:.5px;transition:background .2s}
.btn-back:hover{background:rgba(255,68,68,.22)}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.full{grid-column:1/-1}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:12px;
  padding:16px 17px;backdrop-filter:blur(16px);
  box-shadow:0 2px 16px rgba(0,0,0,.4),inset 0 1px 0 rgba(255,255,255,.03);
  animation:card-in .35s ease both;min-height:150px;display:flex;flex-direction:column}
@keyframes card-in{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}
.s-title{font-size:.64rem;font-weight:700;color:var(--sub);letter-spacing:2.5px;
  text-transform:uppercase;margin-bottom:11px;display:flex;align-items:center;gap:7px;flex-shrink:0}
.s-icon{font-size:.92rem;opacity:.85}
.s-line{flex:1;height:1px;background:linear-gradient(90deg,var(--border),transparent)}
.empty{font-size:.78rem;color:var(--sub);opacity:.35;text-align:center;
  flex:1;display:flex;align-items:center;justify-content:center}
.clock-wrap{text-align:center;flex:1;display:flex;flex-direction:column;justify-content:center;padding:4px 0}
.clock-time{font-family:var(--mono);font-size:3.8rem;font-weight:400;color:var(--c);
  letter-spacing:6px;line-height:1;text-shadow:0 0 32px rgba(0,212,255,.5)}
.clock-secs{font-size:1.8rem;color:rgba(0,212,255,.45);margin-left:4px}
.clock-date{font-family:var(--sans);font-weight:300;color:var(--sub);font-size:.85rem;
  letter-spacing:1.3px;margin-top:9px}
.wx-row{display:flex;align-items:center;gap:12px;margin-bottom:11px}
.wx-icon{font-size:2.8rem;filter:drop-shadow(0 0 7px rgba(245,158,11,.35))}
.wx-temp{font-family:var(--sans);font-size:2.5rem;font-weight:700;color:var(--c4);line-height:1;
  text-shadow:0 0 16px rgba(245,158,11,.3)}
.wx-desc{font-size:.82rem;color:var(--text);margin-top:3px}
.wx-feels{font-size:.7rem;color:var(--sub);margin-top:2px}
.wx-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:5px}
.wx-stat{background:rgba(0,0,0,.3);border-radius:6px;padding:6px 8px}
.wx-sl{font-size:.57rem;color:var(--sub);letter-spacing:.8px}
.wx-sv{font-size:.88rem;font-weight:700;color:var(--text);margin-top:2px}
.wx-city{font-size:.66rem;color:var(--sub);margin-top:7px;opacity:.4;text-align:right}
.soc-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px}
.soc-lbl{font-size:.66rem;color:var(--sub)}
.soc-badge{font-size:.6rem;font-weight:700;padding:2px 8px;border-radius:8px;
  letter-spacing:1px;text-transform:uppercase;border:1px solid}
.b-charging{color:var(--c2);border-color:var(--c2);background:rgba(0,255,136,.1)}
.b-discharging{color:var(--c);border-color:var(--c);background:rgba(0,212,255,.1)}
.b-standby{color:var(--sub);border-color:var(--sub);background:rgba(74,85,104,.1)}
.b-offline{color:var(--red);border-color:var(--red);background:rgba(255,68,68,.1)}
.soc-track{height:22px;border-radius:6px;overflow:hidden;background:rgba(0,0,0,.5);
  border:1px solid rgba(255,255,255,.06);position:relative;margin-bottom:8px}
.soc-fill{height:100%;border-radius:5px;position:absolute;left:0;top:0;transition:width 1.2s ease}
.soc-val{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  font-size:.8rem;font-weight:700;color:#fff;text-shadow:0 1px 4px rgba(0,0,0,.9)}
.e-grid{display:grid;grid-template-columns:1fr 1fr;gap:5px}
.e-cell{background:rgba(0,0,0,.35);border-radius:7px;padding:8px 10px}
.e-lbl{font-size:.57rem;color:var(--sub);letter-spacing:.8px;text-transform:uppercase}
.e-val{font-size:1.05rem;font-weight:700;margin-top:3px}
.col-g{color:var(--c2)}.col-c{color:var(--c)}.col-r{color:var(--red)}.col-o{color:var(--orange)}.col-y{color:var(--c4)}
.li{display:flex;align-items:flex-start;gap:8px;padding:6px 0;
  border-bottom:1px solid rgba(255,255,255,.05)}
.li:last-child{border-bottom:none}
.li-badge{flex-shrink:0;white-space:nowrap;font-size:.62rem;padding:2px 7px;border-radius:4px;
  border:1px solid rgba(0,212,255,.22);color:var(--c);background:rgba(0,212,255,.07);margin-top:1px}
.li-badge.urgent{color:var(--red);border-color:rgba(255,68,68,.3);background:rgba(255,68,68,.07)}
.li-badge.soon{color:var(--c4);border-color:rgba(245,158,11,.3);background:rgba(245,158,11,.07)}
.li-badge.job{color:#a78bfa;border-color:rgba(124,58,237,.3);background:rgba(124,58,237,.07)}
.li-text{flex:1;font-size:.82rem;color:var(--text);line-height:1.35}
.li-src{font-size:.64rem;color:var(--sub);margin-top:2px}
.fuel-row{display:grid;grid-template-columns:repeat(3,1fr) 2fr;gap:10px;align-items:center}
.fuel-cell{background:rgba(0,0,0,.35);border-radius:9px;padding:14px 10px;
  text-align:center;border:1px solid rgba(255,255,255,.05)}
.fuel-type{font-size:.62rem;color:var(--sub);letter-spacing:1px}
.fuel-price{font-family:var(--mono);font-size:1.6rem;font-weight:700;
  color:var(--c4);margin-top:6px;text-shadow:0 0 12px rgba(245,158,11,.2)}
.fuel-unit{font-size:.56rem;color:var(--sub);margin-top:2px}
.fuel-info{padding:4px 0}
.fuel-cheapest{font-size:.74rem;color:var(--c2);margin-bottom:5px}
.fuel-meta{font-size:.63rem;color:var(--sub);opacity:.4}
.p-ftr{display:flex;align-items:center;justify-content:space-between;padding:9px 17px;
  background:var(--bg2);border:1px solid var(--border);border-radius:12px;font-size:.66rem;color:var(--sub)}
.cd-wrap{display:flex;align-items:center;gap:8px}
.cd-val{min-width:32px;text-align:right}
.cd-track{width:90px;height:3px;background:rgba(255,255,255,.06);border-radius:2px;overflow:hidden}
.cd-fill{height:100%;border-radius:2px;background:var(--c);box-shadow:0 0 4px var(--c);
  transition:width 1s linear,background .5s}
"""

_HTML_BODY = """
<div id="panel">

  <!-- Header -->
  <div class="p-hdr">
    <div class="p-left">
      <div class="p-logo">{{BOT_INITIAL}}</div>
      <div><div class="p-name">{{BOT_NAME}}</div><div class="p-sub">AMBIENT PANEL</div></div>
    </div>
    <div class="p-dot" id="live-dot"></div>
    <button class="btn-back" onclick="window.parent.closePanel ? window.parent.closePanel() : window.history.back()">&#x2190; Dashboard</button>
  </div>

  <!-- Zeile 1: Uhr · Wetter · Energie -->
  <div class="grid3">

    <div class="card">
      <div class="s-title"><span class="s-icon">🕐</span>Uhr<span class="s-line"></span></div>
      <div class="clock-wrap">
        <div class="clock-time" id="clk-hm">--:--<span class="clock-secs" id="clk-s">--</span></div>
        <div class="clock-date" id="clk-date">...</div>
      </div>
    </div>

    <div class="card" id="card-weather">
      <div class="s-title"><span class="s-icon">🌡</span>Wetter<span class="s-line"></span></div>
      <div id="wx-body"><div class="empty">Keine Daten</div></div>
    </div>

    <div class="card" id="card-energy">
      <div class="s-title"><span class="s-icon">⚡</span>Energie<span class="s-line"></span></div>
      <div id="en-body"><div class="empty">Nicht konfiguriert</div></div>
    </div>

  </div>

  <!-- Zeile 2: Agenda · Jobs · News -->
  <div class="grid3">

    <div class="card" id="card-agenda">
      <div class="s-title"><span class="s-icon">📅</span>Agenda<span class="s-line"></span></div>
      <div id="ag-body"><div class="empty">Keine Termine</div></div>
    </div>

    <div class="card" id="card-jobs">
      <div class="s-title"><span class="s-icon">🤖</span>Geplante Jobs<span class="s-line"></span></div>
      <div id="jo-body"><div class="empty">Keine Jobs</div></div>
    </div>

    <div class="card" id="card-news">
      <div class="s-title"><span class="s-icon">📰</span>News<span class="s-line"></span></div>
      <div id="nw-body"><div class="empty">Keine News</div></div>
    </div>

  </div>

  <!-- Zeile 3: Spritpreise (full-width) -->
  <div class="grid3">
    <div class="card full" id="card-fuel">
      <div class="s-title"><span class="s-icon">⛽</span>Spritpreise<span class="s-line"></span></div>
      <div id="fu-body"><div class="empty">Kein API-Key konfiguriert</div></div>
    </div>
  </div>

  <!-- Footer -->
  <div class="p-ftr">
    <span>&#x27F3;&nbsp;<span id="upd-time">--:--:--</span></span>
    <div class="cd-wrap">
      <span class="cd-val" id="cd-num">-s</span>
      <div class="cd-track"><div class="cd-fill" id="cd-bar" style="width:100%"></div></div>
    </div>
  </div>

</div>
"""

_JS = r"""
'use strict';
const REFRESH_SEC={{REFRESH}};
let countdown=REFRESH_SEC;
const DAYS=['Sonntag','Montag','Dienstag','Mittwoch','Donnerstag','Freitag','Samstag'];
const MONTHS=['Januar','Februar','März','April','Mai','Juni','Juli','August','September','Oktober','November','Dezember'];

function pad(n){return String(n).padStart(2,'0')}
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}

function tickClock(){
  const n=new Date();
  const blink=n.getSeconds()%2===0?':':'<span style="opacity:.15">:</span>';
  document.getElementById('clk-hm').innerHTML=pad(n.getHours())+blink+pad(n.getMinutes())+'<span class="clock-secs">'+pad(n.getSeconds())+'</span>';
  document.getElementById('clk-date').textContent=DAYS[n.getDay()]+', '+n.getDate()+'. '+MONTHS[n.getMonth()]+' '+n.getFullYear();
}

function tickCD(){
  countdown=Math.max(0,countdown-1);
  if(countdown===0){countdown=REFRESH_SEC;fetchAll();}
  document.getElementById('cd-num').textContent=countdown+'s';
  const pct=(countdown/REFRESH_SEC)*100;
  const bar=document.getElementById('cd-bar');
  bar.style.width=pct+'%';
  bar.style.background=pct<15?'var(--red)':pct<40?'var(--orange)':'var(--c)';
}

function dot(s){
  const d=document.getElementById('live-dot');if(!d)return;
  const m={ok:['var(--c2)','0 0 8px var(--c2)'],busy:['var(--c4)','0 0 8px var(--c4)'],err:['var(--red)','0 0 8px var(--red)']};
  const[bg,sh]=m[s]||m.ok;d.style.background=bg;d.style.boxShadow=sh;
}

function renderWeather(w){
  const el=document.getElementById('wx-body');
  if(!w||!w.temp){el.innerHTML='<div class="empty">Keine Daten</div>';return;}
  el.innerHTML=`
    <div class="wx-row">
      <div class="wx-icon">${esc(w.icon)}</div>
      <div><div class="wx-temp">${w.temp}°C</div><div class="wx-desc">${esc(w.desc)}</div><div class="wx-feels">Gefühlt ${w.feels_like}°C</div></div>
    </div>
    <div class="wx-stats">
      <div class="wx-stat"><div class="wx-sl">Feuchte</div><div class="wx-sv">💧${w.humidity}%</div></div>
      <div class="wx-stat"><div class="wx-sl">Wind</div><div class="wx-sv">💨${w.wind}m/s</div></div>
      <div class="wx-stat"><div class="wx-sl">Druck</div><div class="wx-sv">🔵${w.pressure}</div></div>
    </div>
    <div class="wx-city">📍${esc(w.city)}</div>`;
}

function socGrad(s){return s>=70?'linear-gradient(90deg,#00ff88,#00d4ff)':s>=35?'linear-gradient(90deg,#f59e0b,#00ff88)':'linear-gradient(90deg,#ff4444,#f59e0b)'}
function ecell(l,v,c){return`<div class="e-cell"><div class="e-lbl">${l}</div><div class="e-val ${c}">${v}</div></div>`}

function renderEnergy(e){
  const el=document.getElementById('en-body');
  if(!e||!e.available){el.innerHTML='<div class="empty">Nicht konfiguriert</div>';return;}
  let h='';
  if(e.soc!==undefined){
    const bc=e.status==='Lädt'?'b-charging':e.status==='Entlädt'?'b-discharging':e.status==='Offline'?'b-offline':'b-standby';
    h+=`<div class="soc-hdr"><span class="soc-lbl">🔋 Noah 2000</span><span class="soc-badge ${bc}">${esc(e.status)}</span></div>
        <div class="soc-track"><div class="soc-fill" style="width:${e.soc}%;background:${socGrad(e.soc)}"></div><div class="soc-val">${Math.round(e.soc)}%</div></div>`;
  }
  h+='<div class="e-grid">';
  if(e.ppv!==undefined)        h+=ecell('☀️ Solar',e.ppv+' W','col-y');
  if(e.pac!==undefined)        h+=ecell('🔌 Ausgang',e.pac+' W','col-c');
  if(e.eco_power!==undefined)  h+=ecell(e.eco_power<0?'📤 Einspeis.':'📥 Netzbezug',Math.abs(e.eco_power)+' W',e.eco_power<-10?'col-g':e.eco_power>200?'col-r':'col-c');
  if(e.hausverbrauch!==undefined) h+=ecell('🏠 Verbrauch',Math.round(e.hausverbrauch)+' W','col-o');
  if(e.today!==undefined)      h+=ecell('📊 Heute',e.today.toFixed(2)+' kWh','col-c');
  if(e.charge>0)               h+=ecell('⬆️ Laden',e.charge+' W','col-g');
  else if(e.discharge>0)       h+=ecell('⬇️ Entladen',e.discharge+' W','col-r');
  h+='</div>';
  el.innerHTML=h;
}

function renderAgenda(items){
  const el=document.getElementById('ag-body');
  if(!items||!items.length){el.innerHTML='<div class="empty">Keine Termine</div>';return;}
  el.innerHTML=items.map(a=>`<div class="li"><span class="li-badge ${a.urgency||''}">${esc(a.timing)}</span><div class="li-text">${esc(a.time)} · ${esc(a.text)}</div></div>`).join('');
}

function renderJobs(jobs){
  const el=document.getElementById('jo-body');
  if(!jobs||!jobs.length){el.innerHTML='<div class="empty">Keine Jobs</div>';return;}
  el.innerHTML=jobs.map(j=>`<div class="li"><span class="li-badge job">${esc(j.time)}</span><div class="li-text" style="color:var(--sub)">${esc(j.command)}</div></div>`).join('');
}

function renderNews(items){
  const el=document.getElementById('nw-body');
  if(!items||!items.length){el.innerHTML='<div class="empty">Keine News</div>';return;}
  el.innerHTML=items.map(n=>`<div class="li"><div><div class="li-text">${esc(n.title)}</div><div class="li-src">${esc(n.source)}</div></div></div>`).join('');
}

function renderFuel(f){
  const el=document.getElementById('fu-body');
  if(!f||(!f.e5&&!f.avg)){el.innerHTML='<div class="empty">Keine Daten verfügbar</div>';return;}
  const src=f.source?`<span style="color:var(--sub);font-size:.5rem;opacity:.5"> · ${esc(f.source)}</span>`:'';
  if(f.avg){
    // DDG-Fallback: kein API-Key, nur Durchschnitt
    el.innerHTML=`<div class="fuel-row">
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
  el.innerHTML=`<div class="fuel-row">
    <div class="fuel-cell"><div class="fuel-type">E5</div><div class="fuel-price">${esc(f.e5)}</div><div class="fuel-unit">€/L</div></div>
    <div class="fuel-cell"><div class="fuel-type">E10</div><div class="fuel-price">${esc(f.e10)}</div><div class="fuel-unit">€/L</div></div>
    <div class="fuel-cell"><div class="fuel-type">Diesel</div><div class="fuel-price">${esc(f.diesel)}</div><div class="fuel-unit">€/L</div></div>
    <div class="fuel-info">
      ${f.cheapest?`<div class="fuel-cheapest">💚 ${esc(f.cheapest)}</div>`:''}
      <div class="fuel-meta">📍 ${esc(f.city)} · ${f.count} Stationen · 5 km${src}</div>
    </div>
  </div>`;
}

async function fetchAll(){
  dot('busy');
  try{
    const r=await fetch('/screen/api/data');
    if(!r.ok)throw new Error('HTTP '+r.status);
    const d=await r.json();
    renderWeather(d.weather||{});
    renderEnergy(d.energy||{});
    renderAgenda(d.agenda||[]);
    renderJobs(d.jobs||[]);
    renderNews(d.news||[]);
    renderFuel(d.fuel||{});
    if(d.updated)document.getElementById('upd-time').textContent=d.updated;
    dot('ok');countdown=REFRESH_SEC;
  }catch(e){console.warn('[Screen]',e);dot('err');}
}

setInterval(tickClock,1000);setInterval(tickCD,1000);
tickClock();fetchAll();
"""


def setup(app=None):
    threading.Thread(target=_refresh_cache, daemon=True).start()
    logger.info("[Screen] Ambient Panel bereit: /screen/")
    print("🖥️  Ambient Panel: /screen/  (selber Server wie Dashboard)")