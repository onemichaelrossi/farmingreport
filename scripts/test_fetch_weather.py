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


if __name__ == "__main__":
    main()
