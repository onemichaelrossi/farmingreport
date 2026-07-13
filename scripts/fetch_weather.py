#!/usr/bin/env python3
"""
fetch_weather.py
-----------------
Runs on Michael's PC (via a scheduled task). For every site listed in
config/sites.json it:

  1. Geocodes the postcode (postcodes.io -> lat/lon).
  2. Pulls weather data from Open-Meteo: recent history + 10-day forecast
     (api.open-meteo.com), plus season-to-date history (archive-api.open-meteo.com).
  3. Pulls the nearest Environment Agency rainfall station reading
     (environment.data.gov.uk -- England & Wales only).
  4. Computes Growing Degree Days (GDD), a Smith-Kerns dollar spot disease
     risk %, and a simple ET0-vs-rainfall water balance.
  5. Generates rules-based turf-management recommendations ("the advisor")
     from those figures -- see build_recommendations() below. The rules and
     thresholds are also documented in plain English on the dashboard's
     About page.
  6. Appends a compact daily snapshot (including today's advisor severity)
     to a long-running per-site history file, data/sites/<slug>-history.json.
     This is upserted by date every run, so the dashboard's trend charts
     keep growing across runs instead of being limited to whatever window
     the upstream weather APIs happen to return.
  7. Writes data/sites/<slug>.json, data/sites/<slug>-history.json (per
     site) and data/index.json (manifest).
  8. Commits and pushes data/ to the repo's git remote, if this folder is a
     git checkout with a configured remote.

Only the Python standard library is used, so nothing needs to be pip
installed. Requires Python 3.9+.

All API endpoints/parameter names below were verified live on 2026-07-13.
"""

from __future__ import annotations

import json
import math
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Paths / config
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
SITES_CONFIG_PATH = os.path.join(REPO_ROOT, "config", "sites.json")
DATA_DIR = os.path.join(REPO_ROOT, "data")
SITES_DATA_DIR = os.path.join(DATA_DIR, "sites")
LOG_PATH = os.path.join(SCRIPT_DIR, "fetch.log")

USER_AGENT = "farmingreport-dashboard/1.0 (personal weather dashboard; contact via github repo)"
REQUEST_TIMEOUT = 25
MAX_RETRIES = 3

DEFAULT_GDD_BASE_C = 6.0
DEFAULT_SEASON_START = "01-01"          # MM-DD, resets every year
DEFAULT_DOLLAR_SPOT_THRESHOLD_PCT = 20
DEFAULT_FLOOD_RADIUS_KM = 15

PAST_DAYS = 10          # how many actual days the forecast call also returns
FORECAST_DAYS = 10      # how many days ahead to forecast


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "site"


def http_get_json(url: str, retries: int = MAX_RETRIES):
    """GET a URL and parse it as JSON, with retries and a clear error on failure."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            last_err = f"HTTP {e.code} for {url}: {body[:300]}"
        except Exception as e:  # noqa: BLE001 - deliberately broad, this is a best-effort fetcher
            last_err = f"{type(e).__name__} for {url}: {e}"
        if attempt < retries:
            time.sleep(1.5 * attempt)
    raise RuntimeError(last_err)


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def mean_or_none(values):
    vals = [v for v in values if v is not None]
    return statistics.fmean(vals) if vals else None


# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------

def geocode_postcode(postcode: str) -> dict:
    clean = postcode.strip().replace(" ", "")
    url = f"https://api.postcodes.io/postcodes/{urllib.parse.quote(clean)}"
    data = http_get_json(url)
    if data.get("status") != 200 or not data.get("result"):
        raise RuntimeError(f"postcodes.io could not resolve '{postcode}': {data}")
    r = data["result"]
    return {
        "postcode": r.get("postcode"),
        "latitude": r.get("latitude"),
        "longitude": r.get("longitude"),
        "admin_district": r.get("admin_district"),
        "region": r.get("region"),
        "country": r.get("country"),
    }


DAILY_PARAMS_CORE = "temperature_2m_max,temperature_2m_min,precipitation_sum,et0_fao_evapotranspiration"
DAILY_PARAMS_WITH_LEAF = DAILY_PARAMS_CORE + ",leaf_wetness_probability_mean"
HOURLY_PARAMS = "relative_humidity_2m,soil_moisture_0_to_1cm,soil_temperature_0cm,temperature_2m"


def fetch_forecast(lat: float, lon: float) -> dict:
    """Recent (past_days) + upcoming (forecast_days) daily and hourly data."""
    base = "https://api.open-meteo.com/v1/forecast"
    params_daily = DAILY_PARAMS_WITH_LEAF
    for attempt_params in (params_daily, DAILY_PARAMS_CORE):
        q = {
            "latitude": lat,
            "longitude": lon,
            "daily": attempt_params,
            "hourly": HOURLY_PARAMS,
            "past_days": PAST_DAYS,
            "forecast_days": FORECAST_DAYS,
            "timezone": "Europe/London",
        }
        url = base + "?" + urllib.parse.urlencode(q)
        try:
            return http_get_json(url)
        except RuntimeError as e:
            if attempt_params is params_daily:
                log(f"  forecast: leaf_wetness_probability_mean rejected, retrying without it ({e})")
                continue
            raise
    raise RuntimeError("unreachable")


def fetch_archive(lat: float, lon: float, start_date: str, end_date: str) -> dict | None:
    """Season-to-date history, older than what the forecast call's past_days covers."""
    if start_date > end_date:
        return None
    base = "https://archive-api.open-meteo.com/v1/archive"
    q = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start_date,
        "end_date": end_date,
        "daily": DAILY_PARAMS_CORE,
        "timezone": "Europe/London",
    }
    url = base + "?" + urllib.parse.urlencode(q)
    return http_get_json(url)


def fetch_flood(lat: float, lon: float, radius_km: float) -> dict | None:
    """Nearest EA real-time rainfall/level station reading, if any exist within radius."""
ainfall station + latest reading + rolling 24h total. England & Wales only."""
    base = "https://environment.data.gov.uk/flood-monitoring/id/stations"
    q = {"parameter": "rainfall", "lat": lat, "long": lon, "dist": radius_km}
    url = base + "?" + urllib.parse.urlencode(q)
    try:
        data = http_get_json(url)
    except RuntimeError as e:
        log(f"  flood: station lookup failed: {e}")
        return None

    items = data.get("items", [])
    candidates = []
    for st in items:
        st_lat, st_lon = st.get("lat"), st.get("long")
        measures = st.get("measures") or []
        rainfall_measures = [m for m in measures if m.get("parameter") == "rainfall"]
        if st_lat is None or st_lon is None or not rainfall_measures:
            continue
        dist = haversine_km(lat, lon, st_lat, st_lon)
        candidates.append((dist, st, rainfall_measures[0]))

    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    dist_km, station, measure = candidates[0]
    measure_id = measure["@id"].replace("http://", "https://")

    latest_val, latest_time = None, None
    total_24h = None
    try:
        latest = http_get_json(measure_id + "/readings?latest")
        if latest.get("items"):
            latest_val = latest["items"][0].get("value")
            latest_time = latest["items"][0].get("dateTime")
    except RuntimeError as e:
        log(f"  flood: latest reading failed: {e}")

    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        window = http_get_json(measure_id + f"/readings?since={since}")
        vals = [it.get("value") for it in window.get("items", []) if isinstance(it.get("value"), (int, float))]
        if vals:
            total_24h = round(sum(vals), 2)
    except RuntimeError as e:
        log(f"  flood: 24h window failed: {e}")

    return {
        "station_reference": station.get("stationReference"),
        "station_label": station.get("label"),
        "distance_km": round(dist_km, 2),
        "latest_reading_mm": latest_val,
        "latest_reading_time": latest_time,
        "rainfall_last_24h_mm": total_24h,
        "licence": "Environment Agency, Open Government Licence v3",
    }


# ---------------------------------------------------------------------------
# Derived metrics
# ---------------------------------------------------------------------------

def group_hourly_by_date(hourly: dict) -> dict:
    """{'2026-07-10': {'relative_humidity_2m': [...], 'temperature_2m': [...], ...}}"""
    by_date: dict[str, dict[str, list]] = {}
    times = hourly.get("time", [])
    keys = [k for k in hourly.keys() if k != "time"]
    for i, t in enumerate(times):
        date = t[:10]
        bucket = by_date.setdefault(date, {k: [] for k in keys})
        for k in keys:
            series = hourly.get(k) or []
            if i < len(series):
                bucket[k].append(series[i])
    return by_date


def dollar_spot_risk(mean_rh_5d: float | None, mean_temp_5d: float | None):
    if mean_rh_5d is None or mean_temp_5d is None:
        return None, "insufficient data"
    if not (10 <= mean_temp_5d <= 35):
        return None, "model inactive (5-day mean temp outside 10-35C range)"
    logit = -11.4041 + (0.0894 * mean_rh_5d) + (0.1932 * mean_temp_5d)
    prob = math.exp(logit) / (1 + math.exp(logit)) * 100
    return round(prob, 1), "ok"


# ---------------------------------------------------------------------------
# Turf advisor recommendations
# ---------------------------------------------------------------------------
# Rules-based, not machine-learned: every threshold here is a plain constant
# so the advice stays explainable. The same thresholds are documented in
# plain English on the dashboard's About page (about.html).

SEVERITY_RANK = {"good": 0, "warning": 1, "serious": 2}

FROST_RISK_TEMP_C = 2.0
HEAT_STRESS_TEMP_C = 28.0
LEAF_WETNESS_WATCH_PCT = 60.0
SOIL_MOISTURE_DRY = 0.12
SOIL_MOISTURE_SATURATED = 0.40
WATER_DEFICIT_WATCH_MM = -10.0
WATER_DEFICIT_ACTION_MM = -25.0
WATER_SURPLUS_WATCH_MM = 20.0
HEAVY_RAIN_24H_MM = 15.0
GDD_FAST_GROWTH_PER_DAY = 10.0
GDD_SLOW_GROWTH_PER_DAY = 4.0


def _reco(id_: str, severity: str, title: str, message: str) -> dict:
    return {"id": id_, "severity": severity, "title": title, "message": message}


def build_recommendations(current: dict, flood: dict | None, daily_history: list[dict]) -> list[dict]:
    """Turn today's figures into a short list of rules-based turf-management
    recommendations, most severe first. Always returns at least one item."""
    recs: list[dict] = []

    # --- Dollar spot disease risk ---
    risk = current.get("dollar_spot_risk_pct")
    threshold = current.get("dollar_spot_threshold_pct", DEFAULT_DOLLAR_SPOT_THRESHOLD_PCT)
    if risk is not None:
        if risk >= threshold:
            recs.append(_reco(
                "dollar_spot", "serious", "Dollar spot risk is elevated",
                f"Modelled risk is {risk:.0f}% (threshold {threshold:.0f}%). Inspect turf closely for early "
                f"straw-coloured lesions and consider a preventative fungicide application."))
        elif risk >= threshold * 0.6:
            recs.append(_reco(
                "dollar_spot", "warning", "Dollar spot risk is building",
                f"Modelled risk is {risk:.0f}%, approaching the {threshold:.0f}% threshold. Reduce leaf wetness "
                f"duration where you can (mow or brush off dew early, avoid evening irrigation) and monitor daily."))
        else:
            recs.append(_reco(
                "dollar_spot", "good", "Dollar spot risk is low",
                f"Modelled risk is {risk:.0f}%, well under the {threshold:.0f}% threshold. Routine monitoring is enough."))

    # --- 7-day water balance ---
    wb = current.get("water_balance_7d") or {}
    net = wb.get("net_mm")
    if net is not None:
        if net <= WATER_DEFICIT_ACTION_MM:
            recs.append(_reco(
                "water_deficit", "serious", "Significant moisture deficit",
                f"ET0 has outpaced rainfall by {abs(net):.0f}mm over the last 7 days. Irrigate soon to avoid "
                f"drought stress, especially on free-draining areas."))
        elif net <= WATER_DEFICIT_WATCH_MM:
            recs.append(_reco(
                "water_deficit", "warning", "Moisture deficit building",
                f"7-day water balance is {net:.0f}mm. Plan irrigation in the next few days if no rain is forecast."))
        elif net >= WATER_SURPLUS_WATCH_MM:
            recs.append(_reco(
                "water_surplus", "warning", "Moisture surplus — ground may be soft",
                f"7-day water balance is +{net:.0f}mm. Avoid heavy machinery and reduce traffic on saturated "
                f"turf to prevent compaction and rutting."))
        else:
            recs.append(_reco(
                "water_balance", "good", "Water balance is in a healthy range",
                f"7-day net balance is {net:+.0f}mm — no irrigation action needed today."))

    # --- Soil moisture ---
    sm = current.get("soil_moisture_0_1cm")
    if sm is not None:
        if sm < SOIL_MOISTURE_DRY:
            recs.append(_reco(
                "soil_dry", "warning", "Topsoil is dry",
                f"Modelled topsoil moisture is {sm * 100:.0f}%. Consider irrigation, especially if the water "
                f"balance above also shows a deficit."))
        elif sm > SOIL_MOISTURE_SATURATED:
            recs.append(_reco(
                "soil_wet", "warning", "Topsoil is saturated",
                f"Modelled topsoil moisture is {sm * 100:.0f}%. Avoid mowing, rolling or heavy foot traffic "
                f"until it drains, to prevent compaction and surface damage."))

    # --- Leaf wetness ---
    lw = current.get("leaf_wetness_pct")
    if lw is not None and lw >= LEAF_WETNESS_WATCH_PCT:
        recs.append(_reco(
            "leaf_wetness", "warning", "Extended leaf wetness",
            f"Modelled leaf wetness probability is {lw:.0f}%, which raises disease pressure. Avoid evening "
            f"irrigation and improve airflow/mowing height where practical."))

    # --- Frost / heat stress ---
    tmin, tmax = current.get("temp_min_c"), current.get("temp_max_c")
    if tmin is not None and tmin <= FROST_RISK_TEMP_C:
        recs.append(_reco(
            "frost", "warning", "Frost risk",
            f"Overnight minimum is forecast/recorded at {tmin:.1f}°C. Avoid mowing or heavy traffic on "
            f"frosted turf — frozen leaf blades bruise and die under load."))
    if tmax is not None and tmax >= HEAT_STRESS_TEMP_C:
        recs.append(_reco(
            "heat", "warning", "Heat stress risk",
            f"Daytime maximum is forecast/recorded at {tmax:.1f}°C. Raise mowing height, irrigate early "
            f"morning rather than midday, and avoid mowing during peak heat."))

    # --- Growth rate / mowing guidance from trailing GDD accumulation ---
    recent = [r for r in daily_history if r.get("gdd_day") is not None][-7:]
    if recent:
        avg_gdd_day = sum(r["gdd_day"] for r in recent) / len(recent)
        if avg_gdd_day >= GDD_FAST_GROWTH_PER_DAY:
            recs.append(_reco(
                "growth_fast", "good", "Active growth — mow more frequently",
                f"Growing degree days are accumulating at {avg_gdd_day:.1f}/day over the last week. Expect to "
                f"mow every 3-4 days to avoid removing more than a third of leaf blade in one cut."))
        elif avg_gdd_day <= GDD_SLOW_GROWTH_PER_DAY:
            recs.append(_reco(
                "growth_slow", "good", "Slow growth — ease off mowing and feed",
                f"Growing degree days are accumulating slowly ({avg_gdd_day:.1f}/day over the last week). "
                f"Reduce mowing frequency and hold off on fertiliser until growth picks up."))

    # --- Rainfall / flood ---
    if flood and flood.get("rainfall_last_24h_mm") is not None and flood["rainfall_last_24h_mm"] >= HEAVY_RAIN_24H_MM:
        recs.append(_reco(
            "heavy_rain", "warning", "Heavy rainfall recorded",
            f"{flood['rainfall_last_24h_mm']:.0f}mm has fallen at the nearest Environment Agency station in "
            f"the last 24 hours. Check surface drainage and keep vehicles off saturated ground."))

    if not recs:
        recs.append(_reco(
            "all_clear", "good", "Conditions are within normal ranges",
            "No specific action is flagged today — routine monitoring and maintenance is enough."))

    recs.sort(key=lambda r: SEVERITY_RANK.get(r["severity"], 0), reverse=True)
    return recs


def top_severity_of(recommendations: list[dict]) -> str:
    if not recommendations:
        return "good"
    return max(recommendations, key=lambda r: SEVERITY_RANK.get(r["severity"], 0))["severity"]


def build_site_data(site_cfg: dict) -> dict:
    name = site_cfg["name"]
    postcode = site_cfg["postcode"]
    slug = site_cfg.get("slug") or slugify(name)
    gdd_base = float(site_cfg.get("gdd_base_temp_c", DEFAULT_GDD_BASE_C))
    season_start_mmdd = site_cfg.get("season_start", DEFAULT_SEASON_START)
    dollar_spot_threshold = float(site_cfg.get("dollar_spot_threshold_pct", DEFAULT_DOLLAR_SPOT_THRESHOLD_PCT))
    flood_radius = float(site_cfg.get("flood_search_radius_km", DEFAULT_FLOOD_RADIUS_KM))

    errors: list[str] = []
    log(f"Site '{name}' ({postcode}) -> slug '{slug}'")

    geo = geocode_postcode(postcode)
    lat, lon = geo["latitude"], geo["longitude"]
    log(f"  geocoded to {lat:.4f},{lon:.4f} ({geo.get('admin_district')})")

    today = datetime.now(timezone.utc).date()
    season_start_date = datetime.strptime(f"{today.year}-{season_start_mmdd}", "%Y-%m-%d").date()
    if season_start_date > today:
        season_start_date = season_start_date.replace(year=today.year - 1)

    forecast = fetch_forecast(lat, lon)
    daily = forecast.get("daily", {})
    hourly_by_date = group_hourly_by_date(forecast.get("hourly", {}))

    # Earliest date covered by the forecast call's past_days window
    forecast_earliest = daily.get("time", [today.isoformat()])[0]
    archive_end = (datetime.strptime(forecast_earliest, "%Y-%m-%d").date() - timedelta(days=1))
    archive = None
    if season_start_date <= archive_end:
        try:
            archive = fetch_archive(lat, lon, season_start_date.isoformat(), archive_end.isoformat())
        except RuntimeError as e:
            errors.append(f"archive history unavailable: {e}")
            log(f"  archive fetch failed: {e}")

    # ---- Assemble a single combined daily series: archive (actual) + forecast (actual+future) ----
    combined = {}  # date -> dict
    if archive and archive.get("daily"):
        ad = archive["daily"]
        for i, d in enumerate(ad["time"]):
            combined[d] = {
                "date": d,
                "temp_max_c": ad["temperature_2m_max"][i],
                "temp_min_c": ad["temperature_2m_min"][i],
                "precip_mm": ad["precipitation_sum"][i],
                "et0_mm": ad["et0_fao_evapotranspiration"][i],
                "mean_rh_pct": None,
                "mean_temp_c": None,
                "leaf_wetness_pct": None,
                "is_forecast": False,
            }

    for i, d in enumerate(daily.get("time", [])):
        is_future = d > today.isoformat()
        hb = hourly_by_date.get(d, {})
        combined[d] = {
            "date": d,
            "temp_max_c": daily["temperature_2m_max"][i],
            "temp_min_c": daily["temperature_2m_min"][i],
            "precip_mm": daily["precipitation_sum"][i],
            "et0_mm": daily["et0_fao_evapotranspiration"][i],
            "mean_rh_pct": round(mean_or_none(hb.get("relative_humidity_2m", [])), 1) if hb.get("relative_humidity_2m") else None,
            "mean_temp_c": round(mean_or_none(hb.get("temperature_2m", [])), 2) if hb.get("temperature_2m") else None,
            "leaf_wetness_pct": round(mean_or_none(hb.get("leaf_wetness_probability_mean", [])), 1) if hb.get("leaf_wetness_probability_mean") else (
                daily.get("leaf_wetness_probability_mean", [None] * len(daily.get("time", [])))[i]
                if "leaf_wetness_probability_mean" in daily else None
            ),
            "is_forecast": is_future,
        }

    ordered_dates = sorted(combined.keys())

    # GDD accumulation (actual days only, from season start up to and including today;
    # future forecast days are still shown but flagged is_forecast so the chart can dim them)
    gdd_cumulative = 0.0
    daily_history = []
    for d in ordered_dates:
        row = combined[d]
        tmax, tmin = row["temp_max_c"], row["temp_min_c"]
        gdd_day = None
        if tmax is not None and tmin is not None:
            gdd_day = max(0.0, ((tmax + tmin) / 2.0) - gdd_base)
            gdd_cumulative += gdd_day
        row["gdd_day"] = round(gdd_day, 2) if gdd_day is not None else None
        row["gdd_cumulative"] = round(gdd_cumulative, 1)
        daily_history.append(row)

    # Dollar spot risk: needs a trailing 5-day window of mean_rh/mean_temp (only available
    # where we have hourly data, i.e. the forecast call's past_days..forecast_days window)
    hourly_dates = [d for d in ordered_dates if combined[d]["mean_rh_pct"] is not None and combined[d]["mean_temp_c"] is not None]
    for idx, d in enumerate(hourly_dates):
        if idx < 4:
            continue
        window = hourly_dates[idx - 4: idx + 1]
        rh5 = mean_or_none([combined[w]["mean_rh_pct"] for w in window])
        t5 = mean_or_none([combined[w]["mean_temp_c"] for w in window])
        risk, status = dollar_spot_risk(rh5, t5)
        combined[d]["dollar_spot_risk_pct"] = risk
        combined[d]["dollar_spot_status"] = status
        combined[d]["dollar_spot_mean_rh_5d"] = round(rh5, 1) if rh5 is not None else None
        combined[d]["dollar_spot_mean_temp_5d_c"] = round(t5, 1) if t5 is not None else None

    # today's snapshot for the summary card
    today_iso = today.isoformat()
    today_row = combined.get(today_iso, {})
    current = {
        "date": today_iso,
        "temp_max_c": today_row.get("temp_max_c"),
        "temp_min_c": today_row.get("temp_min_c"),
        "precip_today_mm": today_row.get("precip_mm"),
        "et0_today_mm": today_row.get("et0_mm"),
        "soil_moisture_0_1cm": round(mean_or_none(hourly_by_date.get(today_iso, {}).get("soil_moisture_0_to_1cm", [])), 3)
            if hourly_by_date.get(today_iso, {}).get("soil_moisture_0_to_1cm") else None,
        "soil_temperature_0cm_c": round(mean_or_none(hourly_by_date.get(today_iso, {}).get("soil_temperature_0cm", [])), 1)
            if hourly_by_date.get(today_iso, {}).get("soil_temperature_0cm") else None,
        "leaf_wetness_pct": today_row.get("leaf_wetness_pct"),
        "gdd_season_total": today_row.get("gdd_cumulative"),
        "gdd_base_temp_c": gdd_base,
        "dollar_spot_risk_pct": today_row.get("dollar_spot_risk_pct"),
        "dollar_spot_status": today_row.get("dollar_spot_status"),
        "dollar_spot_threshold_pct": dollar_spot_threshold,
    }

    # 7-day water balance (ET0 vs rainfall) ending today
    last7 = [combined[d] for d in ordered_dates if d <= today_iso][-7:]
    et0_7d = sum(r["et0_mm"] for r in last7 if r.get("et0_mm") is not None)
    precip_7d = sum(r["precip_mm"] for r in last7 if r.get("precip_mm") is not None)
    current["water_balance_7d"] = {
        "et0_total_mm": round(et0_7d, 1),
        "precip_total_mm": round(precip_7d, 1),
        "net_mm": round(precip_7d - et0_7d, 1),
        "state": "surplus" if precip_7d >= et0_7d else "deficit",
    }

    forecast_rows = [combined[d] for d in ordered_dates if d > today_iso]

    flood = None
    try:
        flood = fetch_flood(lat, lon, flood_radius)
        if flood is None:
            errors.append(f"no Environment Agency rainfall station within {flood_radius}km (EA covers England & Wales only)")
    except Exception as e:  # noqa: BLE001
        errors.append(f"flood data unavailable: {e}")
        log(f"  flood fetch failed: {e}")

    daily_history_rows = [combined[d] for d in ordered_dates if d <= today_iso]
    recommendations = build_recommendations(current, flood, daily_history_rows)
    top_severity = top_severity_of(recommendations)

    return {
        "site": {
            "name": name,
            "slug": slug,
            "postcode": geo["postcode"],
            "latitude": lat,
            "longitude": lon,
            "admin_district": geo.get("admin_district"),
            "region": geo.get("region"),
        },
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config": {
            "gdd_base_temp_c": gdd_base,
            "season_start": season_start_date.isoformat(),
            "dollar_spot_threshold_pct": dollar_spot_threshold,
        },
        "current": current,
        "recommendations": recommendations,
        "top_severity": top_severity,
        "daily_history": daily_history_rows,
        "forecast": forecast_rows,
        "flood": flood,
        "errors": errors,
        "sources": {
            "geocoding": "postcodes.io",
            "weather": "Open-Meteo (open-meteo.com)",
            "rainfall": "Environment Agency Real-Time Flood-Monitoring API",
        },
    }


# ---------------------------------------------------------------------------
# Analysis history
# ---------------------------------------------------------------------------
# A long-running, per-site log of daily snapshots, independent of whatever
# history window the upstream weather APIs happen to return. Upserted by
# date on every run (safe to re-run the same day), so the dashboard's trend
# charts keep growing over the lifetime of the schedule instead of being
# capped to a fixed window. Capped at HISTORY_MAX_DAYS to keep the file a
# sane size after years of daily runs.

HISTORY_MAX_DAYS = 730


def history_path(slug: str, history_dir: str | None = None) -> str:
    return os.path.join(history_dir or SITES_DATA_DIR, f"{slug}-history.json")


def load_history(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            hist = json.load(f)
        return hist if isinstance(hist, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def update_history(slug: str, data: dict, history_dir: str | None = None) -> list[dict]:
    """Append/replace today's snapshot in the site's long-run history file
    and return the (possibly backfilled/trimmed) list, writing it to disk."""
    path = history_path(slug, history_dir)
    history = load_history(path)

    if not history and data.get("daily_history"):
        # First run for this site: bootstrap the history file with whatever
        # the API already gave us this run, so the trend charts aren't a
        # single dot on day one. Backfilled rows don't have an advisor
        # verdict of their own (the rules need "today's" full context), so
        # top_severity is left null for them.
        for row in data["daily_history"]:
            rec = dict(row)
            rec.setdefault("soil_moisture_0_1cm", None)
            rec.setdefault("top_severity", None)
            history.append(rec)

    by_date = {h["date"]: h for h in history if h.get("date")}
    today_row = data["daily_history"][-1] if data.get("daily_history") else None
    if today_row is not None:
        record = dict(today_row)
        record["soil_moisture_0_1cm"] = (data.get("current") or {}).get("soil_moisture_0_1cm")
        record["top_severity"] = data.get("top_severity")
        by_date[record["date"]] = record

    history = sorted(by_date.values(), key=lambda r: r["date"])
    if len(history) > HISTORY_MAX_DAYS:
        history = history[-HISTORY_MAX_DAYS:]

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)
    return history


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------

def run_git(args: list[str]) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["git"] + args, cwd=REPO_ROOT, capture_output=True, text=True, timeout=60
        )
        return result.returncode, (result.stdout + result.stderr).strip()
    except FileNotFoundError:
        return 1, "git is not installed or not on PATH"
    except Exception as e:  # noqa: BLE001
        return 1, str(e)


def commit_and_push() -> None:
    code, out = run_git(["rev-parse", "--is-inside-work-tree"])
    if code != 0:
        log("  not a git repository, skipping commit/push. (Run this from inside your farmingreport clone.)")
        return

    run_git(["add", "data/"])
    code, out = run_git(["diff", "--cached", "--quiet"])
    if code == 0:
        log("  no data changes since last run, nothing to commit.")
        return

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    code, out = run_git(["commit", "-m", f"Update weather data ({ts})"])
    if code != 0:
        log(f"  git commit failed: {out}")
        return
    log("  committed data changes.")

    code, out = run_git(["push"])
    if code != 0:
        log(f"  git push failed (data is committed locally, will push next run too): {out}")
    else:
        log("  pushed to remote.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    os.makedirs(SITES_DATA_DIR, exist_ok=True)

    if not os.path.exists(SITES_CONFIG_PATH):
        log(f"ERROR: no config file at {SITES_CONFIG_PATH}")
        return 1

    with open(SITES_CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)
    sites_cfg = config.get("sites", [])
    if not sites_cfg:
        log("ERROR: config/sites.json has no sites listed.")
        return 1

    manifest_entries = []
    any_success = False

    for site_cfg in sites_cfg:
        name = site_cfg.get("name", "Unnamed site")
        slug = site_cfg.get("slug") or slugify(name)
        try:
            data = build_site_data(site_cfg)
            slug = data["site"]["slug"]
            history = update_history(slug, data)
            data["history_days"] = len(history)
            out_path = os.path.join(SITES_DATA_DIR, f"{slug}.json")
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            manifest_entries.append({
                "name": data["site"]["name"],
                "slug": data["site"]["slug"],
                "postcode": data["site"]["postcode"],
                "admin_district": data["site"].get("admin_district"),
                "last_updated": data["generated_at"],
                "status": "ok" if not data["errors"] else "partial",
                "errors": data["errors"],
                "gdd_season_total": data["current"].get("gdd_season_total"),
                "dollar_spot_risk_pct": data["current"].get("dollar_spot_risk_pct"),
                "top_severity": data.get("top_severity"),
            })
            any_success = True
            log(f"  wrote {out_path} (history: {len(history)} days)")
        except Exception as e:  # noqa: BLE001
            log(f"ERROR building data for site '{name}': {e}")
            manifest_entries.append({
                "name": name,
                "slug": slug,
                "postcode": site_cfg.get("postcode"),
                "last_updated": None,
                "status": "error",
                "errors": [str(e)],
            })

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sites": manifest_entries,
    }
    with open(os.path.join(DATA_DIR, "index.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    log(f"wrote {os.path.join(DATA_DIR, 'index.json')}")

    commit_and_push()

    return 0 if any_success else 1


if __name__ == "__main__":
    sys.exit(main())
