import os, json, logging, schedule, time, threading, uuid, re
from datetime import date, timedelta
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s")

ATHLETE_ID    = os.environ["INTERVALS_ATHLETE_ID"]
API_KEY       = os.environ["INTERVALS_API_KEY"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
MODEL         = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

BASE = "https://intervals.icu/api/v1"
AUTH = ("API_KEY", API_KEY)

LAT, LON = 57.4641, 9.9774  # Hjørring

def iget(path, params=None):
    r = requests.get(f"{BASE}{path}", params=params, auth=AUTH, timeout=15)
    r.raise_for_status()
    return r.json()

def fetch_wellness(days=5):
    oldest = (date.today() - timedelta(days=days)).isoformat()
    newest = date.today().isoformat()
    return iget(f"/athlete/{ATHLETE_ID}/wellness",
                {"oldest": oldest, "newest": newest})

def fetch_activities(oldest_date, newest_date):
    return iget(f"/athlete/{ATHLETE_ID}/activities",
                {"oldest": oldest_date, "newest": newest_date})

def fetch_planned(oldest_date, newest_date):
    return iget(f"/athlete/{ATHLETE_ID}/events",
                {"oldest": oldest_date, "newest": newest_date,
                 "category": "WORKOUT"})

# ── Vejr (Open-Meteo, ingen API-nøgle) ────────────────────────────────────────

def fetch_weather():
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": LAT,
                "longitude": LON,
                "daily": ("temperature_2m_max,temperature_2m_min,"
                          "precipitation_probability_max,precipitation_sum,"
                          "windspeed_10m_max"),
                "current": "temperature_2m,precipitation,windspeed_10m",
                "timezone": "Europe/Copenhagen",
                "forecast_days": 2,
            },
            timeout=10,
        )
        r.raise_for_status()
        d = r.json()
        daily = d.get("daily", {})
        curr  = d.get("current", {})

        def day_str(idx, label):
            tmax   = daily.get("temperature_2m_max",             [None]*2)[idx]
            tmin   = daily.get("temperature_2m_min",             [None]*2)[idx]
            rain_p = daily.get("precipitation_probability_max",  [None]*2)[idx]
            rain_m = daily.get("precipitation_sum",              [None]*2)[idx]
            wind   = daily.get("windspeed_10m_max",              [None]*2)[idx]
            parts = []
            if tmin is not None and tmax is not None:
                parts.append(f"{tmin:.0f}–{tmax:.0f}°C")
            if rain_p is not None:
                parts.append(f"regnchance {rain_p:.0f}%")
            if rain_m is not None and rain_m > 0:
                parts.append(f"nedbør {rain_m:.1f} mm")
            if wind is not None:
                parts.append(f"vind op til {wind:.0f} km/t")
            return f"{label}: {', '.join(parts)}" if parts else f"{label}: ingen data"

        return "\n".join([
            day_str(0, "I dag"),
            day_str(1, "I morgen"),
            f"Aktuelt: {curr.get('temperature_2m', '?'):.0f}°C, "
            f"vind {curr.get('windspeed_10m', '?'):.0f} km/t",
        ])
    except Exception as e:
        return f"Vejrdata utilgængelig ({e})"

# ── Formatering ────────────────────────────────────────────────────────────────

def fmt_activity(a):
    lines = []
    lines.append(f"Navn: {a.get('name') or a.get('type') or 'Aktivitet'}")

    moving  = a.get("moving_time", 0)
    elapsed = a.get("elapsed_time", 0)
    dist_m  = a.get("distance", 0)
    elev    = a.get("total_elevation_gain", 0)
    lines.append(f"Varighed: {moving//60} min (elapsed {elapsed//60} min)")
    if dist_m:
        lines.append(f"Distance: {dist_m/1000:.2f} km")
    if elev:
        lines.append(f"D+: {elev:.0f} m")

    avg_hr = a.get("average_heartrate") or a.get("average_bpm")
    max_hr = a.get("max_heartrate")     or a.get("max_bpm")
    lthr   = a.get("icu_athlete_lthr") or a.get("lthr")

    if avg_hr:
        pct = f" ({avg_hr/lthr*100:.1f}% LTHR)" if lthr else ""
        lines.append(f"Gns. HR: {avg_hr:.0f} bpm{pct}")
    if max_hr:
        pct = f" ({max_hr/lthr*100:.1f}% LTHR)" if lthr else ""
        lines.append(f"Max HR: {max_hr:.0f} bpm{pct}")
    if lthr:
        lines.append(f"LTHR (fra aktivitet): {lthr:.0f} bpm")
    else:
        lines.append("LTHR (fra aktivitet): ikke tilgængeligt i data")

    # Kadence — Intervals er enkeltbens, gang med 2
    cad = a.get("average_cadence")
    if cad:
        lines.append(f"Kadence: {cad*2:.0f} spm (reel, råværdi: {cad:.0f})")

    # Feel — Intervals er invers: 6 minus råværdi
    feel_raw = a.get("feel")
    if feel_raw is not None:
        lines.append(f"Feel: {6 - feel_raw}/5 (Intervals råværdi: {feel_raw})")

    rpe  = a.get("perceived_exertion") or a.get("rpe")
    tl   = a.get("icu_training_load")  or a.get("tss")
    trimp = a.get("icu_trimp")         or a.get("trimp")
    iff  = a.get("icu_intensity")      or a.get("intensity_factor")
    if rpe:   lines.append(f"RPE: {rpe}")
    if tl:    lines.append(f"Training Load (TSS): {tl:.0f}")
    if trimp: lines.append(f"TRIMP: {trimp:.0f}")
    if iff:   lines.append(f"Intensity Factor: {iff:.3f}")

    # Zonefordeling
    zones = a.get("icu_hr_in_zones") or a.get("hr_zones") or []
    if zones:
        total_z = sum(zones)
        names   = ["Z1", "Z2", "Z3", "Z4", "Z5"]
        zparts  = []
        for i, z in enumerate(zones[:5]):
            pct = f"{z/total_z*100:.0f}%" if total_z > 0 else "?"
            zparts.append(f"{names[i]}: {z//60} min ({pct})")
        lines.append("Zonefordeling (HR-tid):\n  " + "\n  ".join(zparts))

    desc = a.get("description") or a.get("notes")
    if desc:
        lines.append(f"Noter: {desc}")

    return "\n".join(lines)

def fmt_wellness_series(w_list):
    if not w_list:
        return "Ingen wellness-data"
    lines = []
    for w in w_list:
        parts = [w.get("id", "?")]
        for key, label in [
            ("ctl",        "CTL"),
            ("atl",        "ATL"),
            ("tsb",        "TSB"),
            ("hrv",        "HRV"),
            ("restingHR",  "RHR"),
        ]:
            v = w.get(key)
            if v is not None:
                parts.append(f"{label} {v:.1f}" if isinstance(v, float) else f"{label} {v}")
        slp = w.get("sleepSecs")
        if slp:
            parts.append(f"Søvn {slp/3600:.1f}t")
        ss = w.get("sleepScore")
        if ss:
            parts.append(f"Søvnscore {ss}")
        lines.append("  " + " | ".join(parts))
    return "\n".join(lines)

def fmt_event(e):
    lines = []
    lines.append(f"Navn: {e.get('name', 'Unavngivet session')}")
    start = (e.get("start_date_local") or "")[:10]
    if start:
        lines.append(f"Dato: {start}")
    dur = e.get("moving_time") or e.get("duration")
    if dur:
        lines.append(f"Planlagt varighed: {dur//60} min")
    load = e.get("load") or e.get("tss")
    if load:
        lines.append(f"Planlagt load: {load}")
    desc = e.get("description") or e.get("notes")
    if desc:
        lines.append(f"Noter fra Intervals:\n{desc}")
    return "\n".join(lines)

# ── Datablock ──────────────────────────────────────────────────────────────────

def build_data_block():
    today     = date.today()
    yesterday = today - timedelta(days=1)
    tomorrow  = today + timedelta(days=1)

    wellness    = fetch_wellness(days=5)
    acts_today  = fetch_activities(today.isoformat(),     today.isoformat())
    acts_yest   = fetch_activities(yesterday.isoformat(), yesterday.isoformat())
    planned_td  = fetch_planned(today.isoformat(),        today.isoformat())
    planned_tm  = fetch_planned(tomorrow.isoformat(),     tomorrow.isoformat())
    weather     = fetch_weather()

    s = [f"DATO: {today.strftime('%A %d. %B %Y')} (dansk tid)", ""]

    s += ["═══ VEJR (Hjørring) ═══", weather, ""]

    s += ["═══ WELLNESS (seneste 5 dage) ═══", fmt_wellness_series(wellness), ""]

    s += [f"═══ GÅRSDAGENS AKTIVITET ({yesterday.isoformat()}) ═══"]
    if acts_yest:
        for a in acts_yest:
            s += [fmt_activity(a), ""]
    else:
        s += ["Ingen aktivitet registreret", ""]

    s += [f"═══ DAGENS AKTIVITET ({today.isoformat()}) ═══"]
    if acts_today:
        for a in acts_today:
            s += [fmt_activity(a), ""]
    elif planned_td:
        s += ["Ingen aktivitet registreret endnu — planlagt session:", ""]
        for e in planned_td:
            s += [fmt_event(e), ""]
    else:
        s += ["Ingen aktivitet registreret endnu og ingen planlagt session", ""]

    s += [f"═══ PLANLAGT I MORGEN ({tomorrow.isoformat()}) ═══"]
    if planned_tm:
        for e in planned_tm:
            s += [fmt_event(e), ""]
    else:
        s += ["Ingen planlagte events", ""]

    return "\n".join(s)

# ── System prompt ──────────────────────────────────────────────────────────────

def load_system_prompt():
    """Læser system_prompt.txt ved hver kørsel — redigér filen uden rebuild."""
    path = os.environ.get("SYSTEM_PROMPT_PATH", "system_prompt.txt")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        raise RuntimeError(
            f"system_prompt.txt ikke fundet på '{path}'. "
            "Sørg for at filen er mountet korrekt i containeren."
        )

# ── Anthropic ──────────────────────────────────────────────────────────────────

def ask_claude(data_block):
    system = load_system_prompt()
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 2000,
            "system": system,
            "messages": [{"role": "user", "content": data_block}],
        },
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["content"][0]["text"]

# ── Telegram ───────────────────────────────────────────────────────────────────

TG_BASE = "https://api.telegram.org/bot"

def tg_send(text, parse_mode="HTML", run_id=None):
    """Send én besked — maks 4096 tegn. Splitter automatisk ved behov."""
    token   = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    limit   = 4000  # lidt under 4096 for sikkerhed

    chunks = [text[i:i+limit] for i in range(0, len(text), limit)]
    logging.info("[%s] Sender %d chunk(s) til Telegram", run_id, len(chunks))
    for i, chunk in enumerate(chunks):
        logging.info("[%s] POST chunk %d/%d (%d tegn)", run_id, i+1, len(chunks), len(chunk))
        r = requests.post(
            f"{TG_BASE}{token}/sendMessage",
            json={
                "chat_id":    chat_id,
                "text":       chunk,
                "parse_mode": parse_mode,
            },
            timeout=15,
        )
        logging.info("[%s] Telegram svar: %s", run_id, r.status_code)
        if not r.ok:
            logging.warning("[%s] HTML parse fejl, prøver plain text", run_id)
            r2 = requests.post(
                f"{TG_BASE}{token}/sendMessage",
                json={"chat_id": chat_id, "text": chunk},
                timeout=15,
            )
            r2.raise_for_status()

def send(message, run_id=None):
    today = date.today().strftime("%d/%m")
    full  = f"🏃 <b>Træning {today}</b>\n\n{message}"
    tg_send(full, run_id=run_id)
    logging.info("[%s] Sendt til Telegram (%d tegn)", run_id, len(full))

def send_error(err):
    token   = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    requests.post(
        f"{TG_BASE}{token}/sendMessage",
        json={"chat_id": chat_id, "text": f"⚠️ Træning script fejlede:\n{err}"},
        timeout=15,
    )

# ── Stier (afledt af SYSTEM_PROMPT_PATH) ─────────────────────────────────────

_CONFIG_DIR           = os.path.dirname(os.environ.get("SYSTEM_PROMPT_PATH", "system_prompt.txt"))
_FLAG_PATH            = os.path.join(_CONFIG_DIR, ".ran_today")
_NUTRITION_STATE_PATH = os.path.join(_CONFIG_DIR, "nutrition_state.json")
_run_lock             = threading.Lock()

def _flag_is_set():
    try:
        return open(_FLAG_PATH).read().strip() == date.today().isoformat()
    except Exception:
        return False

def _set_flag():
    try:
        with open(_FLAG_PATH, "w") as f:
            f.write(date.today().isoformat())
    except Exception as e:
        logging.warning("Kunne ikke skrive flag-fil: %s", e)

def reset_flag():
    try:
        open(_FLAG_PATH, "w").close()
    except Exception:
        pass
    logging.info("Daglig flag nulstillet (02:00)")

# ── Ernæring ──────────────────────────────────────────────────────────────────

def load_nutrition_state():
    try:
        with open(_NUTRITION_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_nutrition_state(state):
    try:
        with open(_NUTRITION_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        logging.info("Nutrition state gemt: %s", state)
    except Exception as e:
        logging.warning("Kunne ikke gemme nutrition_state: %s", e)

def load_nutrition_prompt():
    path = os.environ.get(
        "NUTRITION_PROMPT_PATH",
        os.path.join(_CONFIG_DIR, "nutrition_prompt.txt")
    )
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        raise RuntimeError(f"nutrition_prompt.txt ikke fundet på '{path}'.")

def fmt_wellness_with_weight(w_list):
    if not w_list:
        return "Ingen wellness-data"
    lines = []
    for w in w_list:
        parts = [w.get("id", "?")]
        for key, label in [
            ("ctl", "CTL"), ("atl", "ATL"), ("tsb", "TSB"),
            ("hrv", "HRV"), ("restingHR", "RHR"),
        ]:
            v = w.get(key)
            if v is not None:
                parts.append(f"{label} {v:.1f}" if isinstance(v, float) else f"{label} {v}")
        weight = w.get("weight")
        if weight:
            parts.append(f"Vægt {weight:.1f} kg")
        slp = w.get("sleepSecs")
        if slp:
            parts.append(f"Søvn {slp/3600:.1f}t")
        lines.append("  " + " | ".join(parts))
    return "\n".join(lines)

def summarize_zone_time(activities):
    totals = [0, 0, 0, 0, 0]
    for a in activities:
        zones = a.get("icu_hr_in_zones") or a.get("hr_zones") or []
        for i, z in enumerate(zones[:5]):
            totals[i] += z
    names = ["Z1", "Z2", "Z3", "Z4", "Z5"]
    lines = [f"{names[i]}: {z//60} min" for i, z in enumerate(totals) if z > 0]
    return "\n".join(lines) if lines else "Ingen zonetid registreret"

def build_nutrition_block():
    today      = date.today()
    week_start = (today - timedelta(days=7)).isoformat()

    wellness   = fetch_wellness(days=8)
    activities = fetch_activities(week_start, today.isoformat())
    state      = load_nutrition_state()

    s = [f"DATO: {today.strftime('%A %d. %B %Y')} (dansk tid) — UGE {today.isocalendar()[1]}", ""]

    s += ["═══ WELLNESS OG VÆGT (seneste 8 dage) ═══",
          fmt_wellness_with_weight(wellness), ""]

    s += ["═══ UGENTLIG ZONETID (løb) ═══",
          summarize_zone_time(activities), ""]

    s += ["═══ AKTIVITETER DENNE UGE ═══"]
    if activities:
        for a in activities:
            s += [fmt_activity(a), ""]
    else:
        s += ["Ingen aktiviteter registreret", ""]

    s += ["═══ FORRIGE UGES MÅL ═══"]
    if state:
        s += [
            f"Uge startende: {state.get('week_start', '?')}",
            f"Kaloriemål: {state.get('calories', '?')} kcal/dag",
            f"Protein: {state.get('protein_g', '?')} g/dag",
            f"Kulhydrat: {state.get('carbs_g', '?')} g/dag",
            f"Fedt: {state.get('fat_g', '?')} g/dag",
            f"Startvægt uge: {state.get('weight_at_start', '?')} kg",
            "",
        ]
    else:
        s += ["Ingen tidligere mål — første kørsel.", ""]

    return "\n".join(s)

def ask_claude_nutrition(data_block):
    system = load_nutrition_prompt()
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 2500,
            "system": system,
            "messages": [{"role": "user", "content": data_block}],
        },
        timeout=60,
    )
    r.raise_for_status()
    response_text = r.json()["content"][0]["text"]

    # Claude instrueres til at afslutte med en ```json { ... } ``` blok
    match = re.search(r'```json\s*(\{.*?\})\s*```', response_text, re.DOTALL)
    if match:
        try:
            new_state = json.loads(match.group(1))
            new_state["week_start"] = today_iso = date.today().isoformat()
            save_nutrition_state(new_state)
            response_text = response_text[:match.start()].strip()
        except Exception as e:
            logging.warning("Kunne ikke parse nutrition state fra Claude: %s", e)

    return response_text

def run_nutrition():
    run_id = uuid.uuid4().hex[:8]
    logging.info("[%s] Starter ernæringsopdatering (mandag)...", run_id)
    try:
        data_block = build_nutrition_block()
        logging.info("[%s] Data hentet, spørger Claude om ernæring...", run_id)
        message = ask_claude_nutrition(data_block)
        week_no = date.today().isocalendar()[1]
        full = f"🥗 <b>Ernæring — uge {week_no}</b>\n\n{message}"
        tg_send(full, run_id=run_id)
        logging.info("[%s] Ernæringsrapport sendt (%d tegn)", run_id, len(full))
    except Exception as e:
        logging.error("[%s] Fejl i ernæringskørsel: %s", run_id, e, exc_info=True)
        send_error(f"Ernæring fejlede: {e}")

# ── Main ───────────────────────────────────────────────────────────────────────

def run(source="scheduled"):
    run_id = uuid.uuid4().hex[:8]
    logging.info("[%s] Starter check-in (kilde: %s, tråd: %s)...", run_id, source, threading.current_thread().name)
    try:
        data_block = build_data_block()
        logging.info("[%s] Data hentet, spørger Claude (%s)...", run_id, MODEL)
        message = ask_claude(data_block)
        send(message, run_id=run_id)
    except Exception as e:
        logging.error("[%s] Fejl: %s", run_id, e, exc_info=True)
        send_error(str(e))
        raise

def scheduled_run():
    if _flag_is_set():
        logging.info("Allerede kørt i dag — springer 09:00 over")
        return
    with _run_lock:
        if _flag_is_set():
            return
        _set_flag()
    run(source="scheduled")

def manual_run():
    with _run_lock:
        if _flag_is_set():
            logging.info("Manuel trigger modtaget, men er allerede kørt i dag")
            return
        _set_flag()
    run(source="manuel")

# ── Telegram polling ───────────────────────────────────────────────────────────

def poll_telegram():
    token  = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    offset = 0
    url    = f"{TG_BASE}{token}/getUpdates"

    logging.info("Telegram polling startet (offset=%d)", offset)
    while True:
        try:
            r = requests.get(url, params={"timeout": 30, "offset": offset}, timeout=35)
            if not r.ok:
                time.sleep(5)
                continue
            updates = r.json().get("result", [])
            if updates:
                logging.info("Polling: %d ny(e) update(s) modtaget", len(updates))
            for update in updates:
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                from_chat = str(msg.get("chat", {}).get("id"))
                text = msg.get("text", "").strip()
                logging.info("Polling update: chat=%s tekst=%r offset=%d", from_chat, text, offset)
                if from_chat != chat_id:
                    logging.info("Ignorerer besked fra ukendt chat %s", from_chat)
                    continue
                if text.lower() in ("/nu", "/run", "nu", "run"):
                    logging.info("Manuel trigger modtaget via Telegram")
                    threading.Thread(target=manual_run, daemon=True).start()
        except Exception as e:
            logging.warning("Telegram polling fejl: %s", e)
            time.sleep(10)

if __name__ == "__main__":
    if os.environ.get("TEST") == "1":
        logging.info("TEST-kørsel")
        run(source="test")
    else:
        logging.info("Scheduler startet — kl. 09:00 Europe/Copenhagen")
        logging.info("Flag-fil: %s (sat i dag: %s)", _FLAG_PATH, _flag_is_set())
        threading.Thread(target=poll_telegram, daemon=True).start()
        schedule.every().day.at("09:00").do(scheduled_run)
        schedule.every().monday.at("09:10").do(run_nutrition)
        schedule.every().day.at("02:00").do(reset_flag)
        while True:
            schedule.run_pending()
            time.sleep(30)