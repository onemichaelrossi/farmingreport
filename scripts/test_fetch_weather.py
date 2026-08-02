"""
Local logic test for fetch_weather.py, using synthetic fixtures shaped exactly
like the real Open-Meteo / EA API responses (verified live against the real
APIs on 2026-07-13). Monkeypatches http_get_json so no network is used.
Run: python3 scripts/_test_fetch_weather.py
Delete this file before/after it's served no further purpose if you want a
tidier repo -- it's dev-only and never used by the dashboard or the schedule.
"""
import json
import sys
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_weather as fw  # noqa: E402

TODAY = datetime.now(timezone.utc).date()


def daterange(start, n):
    return [(start + timedelta(days=i)).isoformat() for i in range(n)]


def make_forecast_fixture():
    past_days = fw.PAST_DAYS
    forecast_days = fw.FORECAST_DAYS
    start = TODAY - timedelta(days=past_days)
    dates = daterange(start, past_days + forecast_days)
    n = len(dates)
    daily = {
        "time": dates,
        "temperature_2m_max": [15.0 + (i % 5) for i in range(n)],
        "temperature_2m_min": [8.0 + (i % 3) for i in range(n)],
        "precipitation_sum": [0.0 if i % 4 else 3.2 for i in range(n)],
        "et0_fao_evapotranspiration": [2.1 + 0.1 * (i % 4) for i in range(n)],
        "leaf_wetness_probability_mean": [40 + (i % 30) for i in range(n)],
        "wind_speed_10m_max": [8.0 + (i % 12) for i in range(n)],
        "uv_index_max": [1.0 + (i % 7) for i in range(n)],
    }
    hourly_times = []
    rh, sm, st_, temp = [], [], [], []
    for d in dates:
        base = datetime.fromisoformat(d)
        for h in range(24):
            hourly_times.append((base + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M"))
            rh.append(60 + (h % 20))
            sm.append(0.25 + 0.01 * (h % 5))
            st_.append(12 + (h % 4))
            temp.append(10 + (h % 10))
    hourly = {
        "time": hourly_times,
        "relative_humidity_2m": rh,
        "soil_moisture_0_to_1cm": sm,
        "soil_temperature_0cm": st_,
        "temperature_2m": temp,
    }
    return {"daily": daily, "hourly": hourly, "daily_units": {}, "hourly_units": {}}


def make_archive_fixture(start_date, end_date):
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    n = (end - start).days + 1
    if n <= 0:
        return {"daily": {"time": []}}
    dates = daterange(start, n)
    return {
        "daily": {
            "time": dates,
            "temperature_2m_max": [4.0 + (i % 6) for i in range(n)],
            "temperature_2m_min": [-1.0 + (i % 4) for i in range(n)],
            "precipitation_sum": [0.0 if i % 3 else 1.1 for i in range(n)],
            "et0_fao_evapotranspiration": [0.5 + 0.05 * (i % 5) for i in range(n)],
        }
    }


def make_geocode_fixture():
    return {
        "status": 200,
        "result": {
            "postcode": "SK7 1AT",
            "latitude": 53.338683,
            "longitude": -2.159994,
            "admin_district": "Stockport",
            "region": "North West",
            "country": "England",
        },
    }


def make_ea_stations_fixture():
    return {
        "items": [
            {
                "@id": "http://environment.data.gov.uk/flood-monitoring/id/stations/564769",
                "lat": 53.300189,
                "long": -2.155251,
                "label": "Rainfall station",
                "stationReference": "564769",
                "measures": [
                    {
                        "@id": "http://environment.data.gov.uk/flood-monitoring/id/measures/564769-rainfall-tipping_bucket_raingauge-t-15_min-mm",
                        "parameter": "rainfall",
                        "period": 900,
                        "unitName": "mm",
                    }
                ],
            }
        ]
    }


def fake_http_get_json(url, retries=3):
    if "api.postcodes.io" in url:
        return make_geocode_fixture()
    if "archive-api.open-meteo.com" in url:
        import urllib.parse as up
        qs = dict(up.parse_qsl(up.urlparse(url).query))
        return make_archive_fixture(qs["start_date"], qs["end_date"])
    if "api.open-meteo.com" in url:
        return make_forecast_fixture()
    if "flood-monitoring/id/stations" in url:
        return make_ea_stations_fixture()
    if "/readings?latest" in url:
        return {"items": [{"dateTime": "2026-07-13T07:30:00Z", "value": 0.2}]}
    if "/readings?since=" in url:
        return {"items": [{"value": 0.2}, {"value": 0.0}, {"value": 0.4}]}
    raise AssertionError(f"unexpected URL in test: {url}")


def main():
    fw.http_get_json = fake_http_get_json  # monkeypatch

    site_cfg = {
        "name": "Michael's House",
        "postcode": "SK7 1AT",
        "gdd_base_temp_c": 6,
        "season_start": "01-01",
        "dollar_spot_threshold_pct": 20,
        "flood_search_radius_km": 15,
    }

    data = fw.build_site_data(site_cfg)

    # ---- assertions ----
    assert data["site"]["slug"] == "michael-s-house"
    assert data["site"]["latitude"] == 53.338683
    assert len(data["daily_history"]) > 0, "daily_history should not be empty"
    assert len(data["forecast"]) == fw.FORECAST_DAYS - 1 or len(data["forecast"]) == fw.FORECAST_DAYS, \
        f"expected ~{fw.FORECAST_DAYS} forecast days, got {len(data['forecast'])}"

    gdd_values = [d["gdd_cumulative"] for d in data["daily_history"] if d["gdd_cumulative"] is not None]
    assert gdd_values == sorted(gdd_values), "GDD cumulative should be non-decreasing"
    assert gdd_values[-1] > 0, "GDD should have accumulated some heat units by now"

    risk = data["current"]["dollar_spot_risk_pct"]
    assert risk is None or 0 <= risk <= 100, f"dollar spot risk out of range: {risk}"

    wb = data["current"]["water_balance_7d"]
    assert "net_mm" in wb and wb["state"] in ("surplus", "deficit")

    assert data["flood"] is not None, "flood data should be populated from fixture"
    assert data["flood"]["station_reference"] == "564769"
    assert data["flood"]["rainfall_last_24h_mm"] == 0.6

    assert data["current"]["leaf_wetness_pct"] is not None

    # ---- new batch-A figures ----
    gp = data["current"]["growth_potential_pct"]
    assert gp is not None and 0 <= gp <= 100, f"growth potential out of range: {gp}"
    assert data["current"]["growth_potential_band"] in ("dormant", "slow", "moderate", "active")

    md_score = data["current"]["microdochium_risk_score"]
    assert md_score is not None and 0 <= md_score <= 4, f"microdochium risk out of range: {md_score}"
    assert data["current"]["microdochium_risk_band"] in ("low", "moderate", "high", "very high")

    assert data["current"]["wind_speed_max_kmh"] is not None
    assert data["current"]["uv_index_max"] is not None
    sw = data["current"]["spray_window"]
    assert sw is not None and sw["status"] in ("good", "too_still", "too_windy")

    gp_values = [d["growth_potential_pct"] for d in data["daily_history"] if d.get("growth_potential_pct") is not None]
    assert len(gp_values) > 0, "growth potential should be computed for at least some daily_history rows"
    assert all(0 <= v <= 100 for v in gp_values)

    # ---- advisor recommendations ----
    assert "recommendations" in data and len(data["recommendations"]) >= 1, \
        "build_recommendations should always return at least one item"
    for rec in data["recommendations"]:
        assert rec["severity"] in ("good", "warning", "serious"), f"unexpected severity: {rec['severity']}"
        assert rec["title"] and rec["message"], "each recommendation needs a title and message"
    assert data["top_severity"] in ("good", "warning", "serious")
    sev_ranks = [fw.SEVERITY_RANK[r["severity"]] for r in data["recommendations"]]
    assert sev_ranks == sorted(sev_ranks, reverse=True), "recommendations should be sorted most-severe first"

    out_path = os.path.join(os.path.dirname(__file__), "_test_output_sample.json")
    with open(out_path, "w") as f:
        json.dump(data, f, indent=2)

    print("ALL ASSERTIONS PASSED")
    print(f"Sample output written to {out_path}")
    print(f"GDD season total: {data['current']['gdd_season_total']}")
    print(f"Dollar spot risk: {risk}% ({data['current']['dollar_spot_status']})")
    print(f"Water balance 7d: {wb}")
    print(f"Flood: {data['flood']}")
    print(f"Errors: {data['errors']}")
    print(f"Top severity: {data['top_severity']}")
    for rec in data["recommendations"]:
        print(f"  [{rec['severity']}] {rec['title']}")

    return data


def test_build_recommendations_direct():
    """Exercise build_recommendations() directly against hand-picked figures
    so each severity branch is provably reachable, not just whatever the
    fixture happens to produce."""
    daily_history = [{"gdd_day": 12.0}] * 7

    # Serious dollar spot + serious water deficit + frost + heat in one go
    current = {
        "dollar_spot_risk_pct": 30, "dollar_spot_threshold_pct": 20,
        "water_balance_7d": {"net_mm": -30}, "soil_moisture_0_1cm": 0.05,
        "leaf_wetness_pct": 70, "temp_min_c": 1.0, "temp_max_c": 29.0,
        "growth_potential_pct": 85, "growth_potential_band": "active",
        "microdochium_risk_score": 4,
        "wind_speed_max_kmh": 30, "spray_window": {"status": "too_windy", "rain_caveat": False},
        "uv_index_max": 8,
        "soil_temperature_0cm_c": 12.0,  # in germination range, but soil_moisture above (0.05) is dry
    }
    recs = fw.build_recommendations(current, {"rainfall_last_24h_mm": 20}, daily_history)
    ids = {r["id"] for r in recs}
    assert "dollar_spot" in ids and "water_deficit" in ids and "soil_dry" in ids
    assert "frost" in ids and "heat" in ids and "heavy_rain" in ids and "growth_fast" in ids
    assert "microdochium" in ids and "growth_potential" in ids and "spray_window" in ids
    assert "uv_staff" in ids and "seeding_window" in ids
    # tmin=1.0 is above the hard-frost cutoff (<=0), so this should be the "watch" tier, not "serious"
    frost_rec = next(r for r in recs if r["id"] == "frost")
    assert frost_rec["severity"] == "warning" and "Frost risk" in frost_rec["title"]
    seeding_rec = next(r for r in recs if r["id"] == "seeding_window")
    assert seeding_rec["severity"] == "warning", "soil is dry, so seeding_window should warn, not say 'good'"
    assert fw.top_severity_of(recs) == "serious"

    # Hard frost (below freezing) -> "serious", not just "warning"
    hard_frost = dict(current)
    hard_frost["temp_min_c"] = -1.5
    recs_hf = fw.build_recommendations(hard_frost, {"rainfall_last_24h_mm": 20}, daily_history)
    frost_rec_hf = next(r for r in recs_hf if r["id"] == "frost")
    assert frost_rec_hf["severity"] == "serious" and "Hard frost" in frost_rec_hf["title"]

    # Calm conditions -> falls through to the all-clear fallback
    calm = {
        "dollar_spot_risk_pct": 2, "dollar_spot_threshold_pct": 20,
        "water_balance_7d": {"net_mm": 2}, "soil_moisture_0_1cm": 0.25,
        "leaf_wetness_pct": 10, "temp_min_c": 10.0, "temp_max_c": 18.0,
    }
    recs2 = fw.build_recommendations(calm, None, [{"gdd_day": 5.0}] * 7)
    assert fw.top_severity_of(recs2) == "good"
    assert all(r["severity"] == "good" for r in recs2)

    # Waterlogging closure risk (heavy rain + saturated soil) + early wilt warning
    # (heat streak + mild not-yet-flagged deficit) -- a distinct scenario since
    # "saturated" and "dry" soil can't both be true in one set of figures.
    wet_current = {
        "dollar_spot_risk_pct": 5, "dollar_spot_threshold_pct": 20,
        "water_balance_7d": {"net_mm": -5}, "soil_moisture_0_1cm": 0.45,
        "leaf_wetness_pct": 20, "temp_min_c": 12.0, "temp_max_c": 20.0,
    }
    heat_streak_history = [{"gdd_day": 8.0}] * 4 + [
        {"gdd_day": 8.0, "temp_max_c": 25.0, "is_forecast": False}] * 3
    recs3 = fw.build_recommendations(wet_current, {"rainfall_last_24h_mm": 18}, heat_streak_history)
    ids3 = {r["id"] for r in recs3}
    assert "closure_risk" in ids3, "heavy rain + saturated soil should flag a closure risk"
    assert "wilt_early_warning" in ids3, "3-day heat streak + mild deficit should flag an early warning"
    assert "soil_wet" in ids3

    # Edge tiers: dormant growth potential, near-still spray wind, low microdochium risk
    edge_current = {
        "dollar_spot_risk_pct": None, "dollar_spot_threshold_pct": 20,
        "water_balance_7d": {"net_mm": 0}, "soil_moisture_0_1cm": 0.20,
        "leaf_wetness_pct": 5, "temp_min_c": 8.0, "temp_max_c": 15.0,
        "growth_potential_pct": 5, "growth_potential_band": "dormant",
        "microdochium_risk_score": 1,
        "wind_speed_max_kmh": 1.5, "spray_window": {"status": "too_still", "rain_caveat": False},
    }
    recs4 = fw.build_recommendations(edge_current, None, [{"gdd_day": 6.0}] * 7)
    ids4 = {r["id"] for r in recs4}
    assert "growth_potential" in ids4
    gp_rec4 = next(r for r in recs4 if r["id"] == "growth_potential")
    assert "very low" in gp_rec4["title"].lower()
    assert "spray_window" in ids4
    spray_rec4 = next(r for r in recs4 if r["id"] == "spray_window")
    assert "still" in spray_rec4["title"].lower()
    md_rec4 = next(r for r in recs4 if r["id"] == "microdochium")
    assert md_rec4["severity"] == "good"

    print("test_build_recommendations_direct PASSED")


def test_history_upsert(sample_data: dict):
    """update_history() should backfill on first run, upsert (not duplicate)
    a same-day re-run, and trim to HISTORY_MAX_DAYS."""
    tmp_dir = tempfile.mkdtemp(prefix="farmingreport-history-test-")
    try:
        slug = sample_data["site"]["slug"]

        history1 = fw.update_history(slug, sample_data, history_dir=tmp_dir)
        assert len(history1) == len(sample_data["daily_history"]), \
            "first run should backfill exactly the days in daily_history"
        dates1 = [h["date"] for h in history1]
        assert dates1 == sorted(dates1), "history should be date-sorted"
        assert dates1 == sorted(set(dates1)), "history should have no duplicate dates"
        assert history1[-1]["top_severity"] == sample_data["top_severity"], \
            "today's record should carry the real advisor verdict, not the backfill placeholder"

        # re-run "same day" with a tweaked figure -- should upsert in place, not append
        sample_data2 = json.loads(json.dumps(sample_data))
        sample_data2["daily_history"][-1]["dollar_spot_risk_pct"] = 99.0
        history2 = fw.update_history(slug, sample_data2, history_dir=tmp_dir)
        assert len(history2) == len(history1), "same-day re-run should not add a new row"
        assert history2[-1]["dollar_spot_risk_pct"] == 99.0, "same-day re-run should overwrite today's row"

        print("test_history_upsert PASSED")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # trimming -- a fresh, empty history dir so the whole long list backfills
    # in one go (an existing history would only upsert "today", not backfill)
    trim_dir = tempfile.mkdtemp(prefix="farmingreport-history-trim-test-")
    try:
        big = json.loads(json.dumps(sample_data))
        base_date = datetime.strptime(big["daily_history"][-1]["date"], "%Y-%m-%d").date()
        long_history = []
        for i in range(fw.HISTORY_MAX_DAYS + 50):
            row = dict(big["daily_history"][-1])
            row["date"] = (base_date - timedelta(days=fw.HISTORY_MAX_DAYS + 50 - i)).isoformat()
            long_history.append(row)
        big["daily_history"] = long_history
        history3 = fw.update_history(slug, big, history_dir=trim_dir)
        assert len(history3) == fw.HISTORY_MAX_DAYS, \
            f"history should be trimmed to HISTORY_MAX_DAYS, got {len(history3)}"
        dates3 = [h["date"] for h in history3]
        assert dates3 == sorted(dates3) and len(set(dates3)) == len(dates3)

        # file actually on disk and valid JSON
        path = fw.history_path(slug, trim_dir)
        with open(path) as f:
            on_disk = json.load(f)
        assert on_disk == history3

        print("test_history_trim PASSED")
    finally:
        shutil.rmtree(trim_dir, ignore_errors=True)


if __name__ == "__main__":
    data = main()
    test_build_recommendations_direct()
    test_history_upsert(data)
    print("ALL TESTS PASSED")
