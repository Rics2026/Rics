import os
import hashlib
import requests as _requests
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

# ============================================================
# ECOTRACKER (unverändert)
# ============================================================

async def get_live_power_raw():
    ip = os.getenv("ECOTRACKER_IP")
    if not ip:
        return None
    try:
        res = _requests.get(f"http://{ip}/v1/json", timeout=3).json()
        p_now = float(res.get('power', 0))
        e_in  = res.get('energyCounterIn', 0) / 1000
        e_out = res.get('energyCounterOut', 0) / 1000
        return {"power": p_now, "in": e_in, "out": e_out}
    except:
        return None

async def get_solar_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_chat_action("typing")
    data = await get_live_power_raw()
    if not data:
        return await update.message.reply_text("❌ Fehler: EcoTracker nicht erreichbar oder IP fehlt.")
    p_now = data["power"]
    status = f"☀️ *Einspeisung:* `{abs(p_now)}` W" if p_now < 0 else f"🏠 *Netzbezug:* `{p_now}` W"
    msg = (
        "🔌 *ENERGIE-MONITOR*\n"
        "￣￣￣￣￣￣￣￣￣￣￣￣￣\n"
        f"{status}\n"
        "￣￣￣￣￣￣￣￣￣￣￣￣￣\n"
        f"📥 *Import:* `{data['in']:.2f}` kWh\n"
        f"📤 *Export:* `{data['out']:.2f}` kWh\n"
        "￣￣￣￣￣￣￣￣￣￣￣￣￣\n"
        "Sir, die Daten sind live vom Sensor."
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

async def get_status():
    data = await get_live_power_raw()
    if not data:
        return "EcoTracker offline."
    p = data["power"]
    status = f"{abs(p)}W Einspeisung" if p < 0 else f"{p}W Netzbezug"
    return f"Strom-Status: {status}"

get_solar_data.description = "Aktueller Stromverbrauch & Einspeisung"
get_solar_data.category = "Energie"


# ============================================================
# GROWATT NOAH 2000 – Session-Auth
# ============================================================

GROWATT_BASE = "https://openapi.growatt.com"
_session     = None


def _hash_pw(pw: str) -> str:
    h = hashlib.md5(pw.encode()).hexdigest()
    for i in range(0, len(h), 2):
        if h[i] == '0':
            h = h[:i] + 'c' + h[i+1:]
    return h


def _login() -> bool:
    global _session
    user = os.getenv("GROWATT_USER")
    pw   = os.getenv("GROWATT_PASS")
    if not user or not pw:
        return False
    try:
        s = _requests.Session()
        s.headers['User-Agent'] = 'ShinePhone/8.1.1 (iPhone; iOS 16.0; Scale/3.00)'
        r = s.post(f"{GROWATT_BASE}/newTwoLoginAPI.do",
                   data={"userName": user, "password": _hash_pw(pw)}, timeout=10)
        try:
            back = r.json().get("back", {})
        except Exception:
            print(f"[Noah] Login: Kein JSON – HTTP {r.status_code}, Antwort: {r.text[:300]!r}")
            return False
        if not back.get("success"):
            print(f"[Noah] Login fehlgeschlagen: {back.get('msg', back.get('error', '?'))}")
            return False
        _session = s
        return True
    except Exception as e:
        print(f"[Noah] Login-Fehler: {e}")
        return False


def _fetch_noah() -> dict | None:
    global _session

    if _session is None and not _login():
        return None

    sn = os.getenv("GROWATT_NOAH_SN")
    if not sn:
        print("[Noah] GROWATT_NOAH_SN fehlt in .env")
        return None

    try:
        r = _session.post(f"{GROWATT_BASE}/noahDeviceApi/noah/getSystemStatus",
                          data={"deviceSn": sn}, timeout=10)
        # Growatt liefert bei abgelaufener Session HTML statt JSON (HTTP 200)
        if r.headers.get("Content-Type", "").startswith("text/html") or r.text.lstrip().startswith("<"):
            print("[Noah] Session abgelaufen (HTML-Response), erneuere Login...")
            _session = None
            if not _login():
                return None
            r = _session.post(f"{GROWATT_BASE}/noahDeviceApi/noah/getSystemStatus",
                              data={"deviceSn": sn}, timeout=10)

        try:
            raw = r.json()
        except Exception:
            print(f"[Noah] Kein JSON – HTTP {r.status_code}, Antwort: {r.text[:300]!r}")
            return None
        obj = raw.get("obj")

        # Zusätzlicher Fallback falls obj leer
        if not obj:
            print("[Noah] Leere Antwort nach Login, gebe auf.")
            return None

        if not obj:
            return None

        soc       = float(obj.get("soc", 0))
        charge    = float(obj.get("chargePower", 0))
        discharge = float(obj.get("disChargePower", 0))
        ppv       = float(obj.get("ppv", 0))
        pac       = float(obj.get("pac", 0))
        today     = float(obj.get("eacToday", 0))
        total     = float(obj.get("eacTotal", 0))
        mode      = int(obj.get("workMode", 0))
        online    = str(obj.get("status", "0")) == "1"

        if not online:
            status = "Offline"
        elif charge > 0:
            status = "Lädt"
        elif discharge > 0:
            status = "Entlädt"
        else:
            status = "Standby"

        mode_map = {"0": "Load First", "1": "Battery First", "2": "Balanced"}
        mode_txt = mode_map.get(str(mode), f"Modus {mode}")

        return {
            "soc":       soc,
            "status":    status,
            "charge":    charge,
            "discharge": discharge,
            "ppv":       ppv,
            "pac":       pac,
            "today":     today,
            "total":     total,
            "mode":      mode_txt,
            "online":    online,
        }

    except Exception as e:
        print(f"[Noah] Datenabruf-Fehler: {e}")
        return None


def _soc_bar(soc: float) -> str:
    filled = round(soc / 10)
    return "🟩" * filled + "⬜" * (10 - filled)


async def get_noah_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_chat_action("typing")
    d = _fetch_noah()
    if not d:
        return await update.message.reply_text(
            "❌ Noah 2000 nicht erreichbar.\n"
            "Prüf ob GROWATT_USER, GROWATT_PASS und GROWATT_NOAH_SN in der .env stehen."
        )
    bar = _soc_bar(d['soc'])
    msg = (
        "🔋 *NOAH 2000 SPEICHER*\n"
        "￣￣￣￣￣￣￣￣￣￣￣￣￣\n"
        f"📡 *Online:* {'✅' if d['online'] else '❌'}\n"
        f"⚡ *Status:* {d['status']}\n"
        f"🔋 *Ladestand:* {bar} `{d['soc']:.0f}%`\n"
        "￣￣￣￣￣￣￣￣￣￣￣￣￣\n"
        f"⬆️ *Lädt:* `{d['charge']:.0f}` W\n"
        f"⬇️ *Entlädt:* `{d['discharge']:.0f}` W\n"
        f"☀️ *Solar:* `{d['ppv']:.0f}` W\n"
        f"🔌 *Einspeisung:* `{d['pac']:.0f}` W\n"
        "￣￣￣￣￣￣￣￣￣￣￣￣￣\n"
        f"📊 *Heute:* `{d['today']:.2f}` kWh\n"
        f"📦 *Gesamt:* `{d['total']:.2f}` kWh\n"
        f"🔧 *Modus:* {d['mode']}\n"
        "￣￣￣￣￣￣￣￣￣￣￣￣￣\n"
        "Sir, Daten live aus der Growatt Cloud."
    )
    await update.message.reply_text(msg, parse_mode='Markdown')


async def get_noah_status() -> str:
    d = _fetch_noah()
    if not d:
        return "Noah 2000 offline oder nicht konfiguriert."
    return (
        f"Noah 2000: {d['status']}, {d['soc']:.0f}% geladen, "
        f"Solar {d['ppv']:.0f}W, Einspeisung {d['pac']:.0f}W, "
        f"heute {d['today']:.2f} kWh"
    )

get_noah_command.description = "Status des Growatt Noah 2000 Speichers"
get_noah_command.category = "Energie"


# ============================================================
# SETUP
# ============================================================

def setup(app):
    app.add_handler(CommandHandler("strom", get_solar_data))
    app.add_handler(CommandHandler("solar", get_noah_command))

    if os.getenv("GROWATT_USER") and os.getenv("GROWATT_PASS"):
        if _login():
            print("✅ [Noah] Growatt-Login erfolgreich.")
        else:
            print("⚠️ [Noah] Growatt-Login fehlgeschlagen.")
    else:
        print("⏭️ [Noah] GROWATT_USER/GROWATT_PASS fehlen – Noah übersprungen.")