import os
import hashlib
import requests as _requests
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

# ============================================================
# ECOTRACKER  –  liest den Netzanschlusspunkt
#   power > 0  →  Netzbezug  (Haus braucht mehr als Noah liefert)
#   power < 0  →  Einspeisung (Noah liefert mehr als Haus braucht)
# ============================================================

async def get_live_power_raw():
    """Rohwerte vom EcoTracker (Netzanschlusspunkt)."""
    ip = os.getenv("ECOTRACKER_IP")
    if not ip:
        return None
    try:
        res = _requests.get(f"http://{ip}/v1/json", timeout=3).json()
        p_now = float(res.get('power', 0))
        e_in  = res.get('energyCounterIn',  0) / 1000
        e_out = res.get('energyCounterOut', 0) / 1000
        return {"power": p_now, "in": e_in, "out": e_out}
    except Exception:
        return None


async def get_solar_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """EcoTracker-Rohwerte – /strom."""
    await update.message.reply_chat_action("typing")
    data = await get_live_power_raw()
    if not data:
        return await update.message.reply_text(
            "❌ Fehler: EcoTracker nicht erreichbar oder IP fehlt."
        )
    p_now  = data["power"]
    status = (
        f"☀️ *Einspeisung:* `{abs(p_now):.0f}` W"
        if p_now < 0 else
        f"🏠 *Netzbezug:* `{p_now:.0f}` W"
    )
    msg = (
        "🔌 *NETZ-MONITOR (EcoTracker)*\n"
        "￣￣￣￣￣￣￣￣￣￣￣￣￣\n"
        f"{status}\n"
        "￣￣￣￣￣￣￣￣￣￣￣￣￣\n"
        f"📥 *Import:* `{data['in']:.2f}` kWh\n"
        f"📤 *Export:* `{data['out']:.2f}` kWh\n"
        "￣￣￣￣￣￣￣￣￣￣￣￣￣\n"
        "_Rohwert Netzanschlusspunkt – ohne Noah-Korrektur._"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')


get_solar_data.description = "Netzstrom-Monitor (EcoTracker)"
get_solar_data.category    = "Energie"


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
        r = s.post(
            f"{GROWATT_BASE}/newTwoLoginAPI.do",
            data={"userName": user, "password": _hash_pw(pw)},
            timeout=10,
        )
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
        r = _session.post(
            f"{GROWATT_BASE}/noahDeviceApi/noah/getSystemStatus",
            data={"deviceSn": sn},
            timeout=10,
        )
        if r.headers.get("Content-Type", "").startswith("text/html") or r.text.lstrip().startswith("<"):
            print("[Noah] Session abgelaufen (HTML-Response), erneuere Login...")
            _session = None
            if not _login():
                return None
            r = _session.post(
                f"{GROWATT_BASE}/noahDeviceApi/noah/getSystemStatus",
                data={"deviceSn": sn},
                timeout=10,
            )

        try:
            raw = r.json()
        except Exception:
            print(f"[Noah] Kein JSON – HTTP {r.status_code}, Antwort: {r.text[:300]!r}")
            return None

        obj = raw.get("obj")
        if not obj:
            print("[Noah] Leere Antwort nach Login, gebe auf.")
            return None

        soc       = float(obj.get("soc",           0))
        charge    = float(obj.get("chargePower",    0))
        discharge = float(obj.get("disChargePower", 0))
        ppv       = float(obj.get("ppv",            0))
        pac       = float(obj.get("pac",            0))
        today     = float(obj.get("eacToday",       0))
        total     = float(obj.get("eacTotal",       0))
        mode      = int(obj.get("workMode",         0))
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
            "soc": soc, "status": status, "charge": charge,
            "discharge": discharge, "ppv": ppv, "pac": pac,
            "today": today, "total": total, "mode": mode_txt, "online": online,
        }

    except Exception as e:
        print(f"[Noah] Datenabruf-Fehler: {e}")
        return None


def _soc_bar(soc: float) -> str:
    filled = round(soc / 10)
    return "🟩" * filled + "⬜" * (10 - filled)


async def get_noah_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/solar – Noah-Detailstatus."""
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
        f"☀️ *Solar PV:* `{d['ppv']:.0f}` W\n"
        f"🔌 *Ausgang (pac):* `{d['pac']:.0f}` W\n"
        "￣￣￣￣￣￣￣￣￣￣￣￣￣\n"
        f"📊 *Heute:* `{d['today']:.2f}` kWh\n"
        f"📦 *Gesamt:* `{d['total']:.2f}` kWh\n"
        f"🔧 *Modus:* {d['mode']}\n"
        "￣￣￣￣￣￣￣￣￣￣￣￣￣\n"
        "_Daten live aus der Growatt Cloud._"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')


async def get_noah_status() -> str:
    """Kurztext (rückwärtskompatibel)."""
    d = _fetch_noah()
    if not d:
        return "Noah 2000 offline oder nicht konfiguriert."
    return (
        f"Noah 2000: {d['status']}, {d['soc']:.0f}% geladen, "
        f"Solar PV {d['ppv']:.0f}W, Ausgang {d['pac']:.0f}W, "
        f"heute {d['today']:.2f} kWh"
    )


get_noah_command.description = "Status des Growatt Noah 2000 Speichers"
get_noah_command.category    = "Energie"


# ============================================================
# KOMBINIERTE ENERGIE-ÜBERSICHT
#
#   Formel:  Hausverbrauch = noah_pac + eco_power
#
#   Beispiele:
#     eco= +5W,  noah=500W  →  Haus 505W,  Noah deckt 99%
#     eco=-200W, noah=800W  →  Haus 600W,  200W gehen ins Netz
#     eco=+2000W,noah=800W  →  Haus 2800W, Noah hilft mit 800W
#     eco= +0W,  noah=  0W  →  Haus   0W   (Nacht)
# ============================================================

async def get_combined_energy_data() -> dict | None:
    """
    Kombiniert EcoTracker (Netzpunkt) und Noah 2000.
    Einheitliches Dict für Anzeige, LLM-Kontext und proactive_brain.
    """
    eco  = await get_live_power_raw()
    noah = _fetch_noah()

    eco_power   = float(eco["power"])       if eco  else None
    noah_pac    = float(noah["pac"])        if noah else None
    noah_ppv    = float(noah["ppv"])        if noah else None
    noah_soc    = float(noah["soc"])        if noah else None
    noah_charge = float(noah["charge"])     if noah else None
    noah_disch  = float(noah["discharge"])  if noah else None
    noah_today  = float(noah["today"])      if noah else None

    # Hausverbrauch = Noah-Ausgang + Netzpunkt
    if eco_power is not None and noah_pac is not None:
        hausverbrauch = noah_pac + eco_power
    elif eco_power is not None:
        hausverbrauch = eco_power
    elif noah_pac is not None:
        hausverbrauch = noah_pac
    else:
        hausverbrauch = None

    # Solaranteil (%)
    solar_pct = None
    if hausverbrauch and hausverbrauch > 0 and noah_pac is not None:
        solar_pct = min(100.0, noah_pac / hausverbrauch * 100)

    # Situationstexte
    if eco_power is None:
        netz_status = "EcoTracker offline"
    elif eco_power < -10:
        netz_status = f"Einspeisung {abs(eco_power):.0f}W ins Netz"
    elif eco_power < 30:
        netz_status = "Netzbezug ~0W (ausgeglichen)"
    else:
        netz_status = f"Netzbezug {eco_power:.0f}W"

    if noah is None or not noah.get("online"):
        noah_status_txt = "Noah offline"
    elif noah_pac and noah_pac > 10:
        noah_status_txt = f"Noah liefert {noah_pac:.0f}W"
    elif noah_charge and noah_charge > 10:
        noah_status_txt = f"Noah lädt ({noah_charge:.0f}W)"
    else:
        noah_status_txt = "Noah Standby"

    return {
        "eco_power_w":    eco_power,
        "noah_pac_w":     noah_pac,
        "noah_ppv_w":     noah_ppv,
        "noah_soc_pct":   noah_soc,
        "noah_charge_w":  noah_charge,
        "noah_disch_w":   noah_disch,
        "noah_today_kwh": noah_today,
        "noah_online":    noah["online"] if noah else False,
        "noah_mode":      noah["mode"]   if noah else "unbekannt",
        "noah_status":    noah["status"] if noah else "offline",
        "hausverbrauch_w": hausverbrauch,
        "solar_pct":       round(solar_pct, 1) if solar_pct is not None else None,
        "netz_status":     netz_status,
        "noah_status_txt": noah_status_txt,
        "import_kwh": eco["in"]  if eco else None,
        "export_kwh": eco["out"] if eco else None,
    }


async def get_energie_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/energie – kombinierte Gesamtübersicht."""
    await update.message.reply_chat_action("typing")
    d = await get_combined_energy_data()
    if not d:
        return await update.message.reply_text("❌ Keine Energiedaten verfügbar.")

    hv = d["hausverbrauch_w"]
    if hv is not None:
        hint = f" _(davon {d['solar_pct']:.0f}% Solar)_" if d["solar_pct"] is not None else ""
        hv_line = f"🏠 *Hausverbrauch:* `{hv:.0f}` W{hint}"
    else:
        hv_line = "🏠 *Hausverbrauch:* `—`"

    ep = d["eco_power_w"]
    if ep is None:
        netz_line = "🔌 *Netz:* EcoTracker offline"
    elif ep < -10:
        netz_line = f"📤 *Einspeisung:* `{abs(ep):.0f}` W _(ins Netz)_"
    elif ep < 30:
        netz_line = f"⚖️ *Netz:* `{ep:.0f}` W _(ausgeglichen)_"
    else:
        netz_line = f"📥 *Netzbezug:* `{ep:.0f}` W"

    soc = d["noah_soc_pct"]
    bar = _soc_bar(soc) if soc is not None else "——"

    if d["noah_online"]:
        noah_block = (
            "￣￣￣￣￣￣￣￣￣￣￣￣￣\n"
            f"🔋 *Noah Ladestand:* {bar} `{soc:.0f}%`\n"
            f"☀️ *Solar PV:* `{d['noah_ppv_w']:.0f}` W\n"
            f"⚡ *Noah Ausgang:* `{d['noah_pac_w']:.0f}` W\n"
        )
        if d["noah_charge_w"] and d["noah_charge_w"] > 5:
            noah_block += f"⬆️ *Lädt:* `{d['noah_charge_w']:.0f}` W\n"
        if d["noah_disch_w"] and d["noah_disch_w"] > 5:
            noah_block += f"⬇️ *Entlädt:* `{d['noah_disch_w']:.0f}` W\n"
        if d["noah_today_kwh"] is not None:
            noah_block += f"📊 *Heute:* `{d['noah_today_kwh']:.2f}` kWh\n"
        noah_block += f"🔧 *Modus:* {d['noah_mode']}\n"
    else:
        noah_block = "￣￣￣￣￣￣￣￣￣￣￣￣￣\n🔋 *Noah 2000:* offline\n"

    counter_block = ""
    if d["import_kwh"] is not None:
        counter_block = (
            "￣￣￣￣￣￣￣￣￣￣￣￣￣\n"
            f"📥 *Gesamt Import:* `{d['import_kwh']:.2f}` kWh\n"
            f"📤 *Gesamt Export:* `{d['export_kwh']:.2f}` kWh\n"
        )

    msg = (
        "⚡ *ENERGIE-ÜBERSICHT*\n"
        "￣￣￣￣￣￣￣￣￣￣￣￣￣\n"
        f"{hv_line}\n"
        f"{netz_line}\n"
        f"{noah_block}"
        f"{counter_block}"
        "￣￣￣￣￣￣￣￣￣￣￣￣￣\n"
        "_Noah-Ausgang + Netzpunkt korrekt abgeglichen._"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')


async def get_energy_status() -> str:
    """Einzeiler für Statusabfragen & proactive_brain (Noah + EcoTracker korrekt)."""
    d = await get_combined_energy_data()
    if not d:
        return "Energiedaten nicht verfügbar."

    parts = []
    if d["hausverbrauch_w"] is not None:
        parts.append(f"Hausverbrauch {d['hausverbrauch_w']:.0f}W")

    ep = d["eco_power_w"]
    if ep is not None:
        if ep < -10:
            parts.append(f"Einspeisung {abs(ep):.0f}W")
        elif ep > 30:
            parts.append(f"Netzbezug {ep:.0f}W")
        else:
            parts.append("Netz ausgeglichen")

    if d["noah_online"]:
        parts.append(f"Noah {d['noah_soc_pct']:.0f}% ({d['noah_status_txt']})")
        if d["noah_ppv_w"] and d["noah_ppv_w"] > 5:
            parts.append(f"Solar PV {d['noah_ppv_w']:.0f}W")
        if d["solar_pct"] is not None:
            parts.append(f"Solaranteil {d['solar_pct']:.0f}%")
    else:
        parts.append("Noah offline")

    return "Energie: " + ", ".join(parts)


get_energie_command.description = "Energie-Gesamtübersicht (Noah + Netz kombiniert)"
get_energie_command.category    = "Energie"


# ============================================================
# RÜCKWÄRTSKOMPATIBILITÄT
# ============================================================

async def get_status() -> str:
    """Alias – rückwärtskompatibel."""
    return await get_energy_status()


# ============================================================
# SETUP
# ============================================================

def setup(app):
    app.add_handler(CommandHandler("strom",   get_solar_data))
    app.add_handler(CommandHandler("solar",   get_noah_command))
    app.add_handler(CommandHandler("energie", get_energie_command))

    if os.getenv("GROWATT_USER") and os.getenv("GROWATT_PASS"):
        if _login():
            print("✅ [Noah] Growatt-Login erfolgreich.")
        else:
            print("⚠️ [Noah] Growatt-Login fehlgeschlagen.")
    else:
        print("⏭️ [Noah] GROWATT_USER/GROWATT_PASS fehlen – Noah übersprungen.")