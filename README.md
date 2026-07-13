# Farming Report — Weather & Turf Dashboard

A public dashboard of predictive weather, growing degree days (GDD), turf
disease risk and irrigation guidance for one or more UK sites, built for
farm/turf management decisions. It includes a rules-based "turf advisor"
that turns those figures into plain-English recommendations, and an
analysis history that accumulates across runs so trend charts show what a
site's figures were in the past as well as where they are now.

**Live dashboard:** enable GitHub Pages on this repo (Settings → Pages →
branch `main` / root) and it will be served from the repository root.

## How it works

```
config/sites.json     you edit this — add/remove sites by name + postcode
        │
        ▼
scripts/fetch_weather.py   runs on a schedule on your own PC (see scripts/README_SETUP.md)
        │  geocodes postcodes, pulls Open-Meteo + Environment Agency data,
        │  computes GDD / dollar spot risk / water balance, generates
        │  turf advisor recommendations, updates the analysis history
        ▼
data/index.json, data/sites/*.json,
data/sites/*-history.json              committed & pushed automatically
        │
        ▼
index.html, about.html   the public dashboard — reads the JSON above, no
                          build step, works as plain static files on GitHub Pages
```

The dashboard itself is static HTML (no server, no build step, no external
accounts needed to view it) — it just fetches the JSON files in `data/` at
load time.

## Repository layout

- `index.html` — the dashboard.
- `about.html` — glossary and methodology (how the advisor and every figure is calculated).
- `assets/style.css` — shared stylesheet for both pages.
- `config/sites.json` — the list of sites to track. Edit this to add sites.
- `data/index.json` — manifest of all sites (auto-generated).
- `data/sites/<slug>.json` — full dataset per site (auto-generated).
- `data/sites/<slug>-history.json` — long-running per-site analysis history (auto-generated, grows across runs).
- `scripts/fetch_weather.py` — the data-fetch script, run locally on a schedule.
- `scripts/run_fetch.bat`, `scripts/README_SETUP.md` — Windows setup + Task Scheduler instructions.
- `scripts/test_fetch_weather.py` — an offline logic test (no network) for the fetch script's calculations.

## What's shown

- **Turf advisor** — rules-based, plain-English recommendations (e.g. irrigate, watch for disease, mow more/less, frost or heat risk) generated fresh each run from the figures below. See `about.html` for the exact thresholds.
- **Growing Degree Days** — heat accumulation for phenology/treatment timing, charted from the accumulated analysis history so the trend keeps growing across runs.
- **Dollar spot disease risk** — the Smith-Kerns logistic regression model.
- **7-day water balance** — ET₀ (reference evapotranspiration) vs rainfall, as a simple irrigation-timing signal.
- **Soil moisture, soil temperature, leaf wetness** — from Open-Meteo's modelled data.
- **Rainfall & flood risk** — nearest Environment Agency rainfall station (England & Wales only).
- **10-day forecast.**
- **Analysis history** — every run appends/updates a compact daily snapshot per site (`data/sites/<slug>-history.json`), so the GDD, dollar spot and temperature charts show what conditions were in the past as well as where they are now, independent of how much history the weather APIs themselves return.

## Data sources & licensing

- [Open-Meteo](https://open-meteo.com/) — weather, soil and ET₀ data (free, non-commercial use).
- [Environment Agency Real-Time Flood-Monitoring API](https://environment.data.gov.uk/flood-monitoring/doc/reference) — Open Government Licence v3.
- [postcodes.io](https://postcodes.io/) — UK postcode geocoding (open data).

The dollar spot model is a decision aid based on published turf-science
research, not a guarantee — always confirm with a site inspection before
acting on it. The dashboard's `about.html` page has the full glossary,
formulas and advisor thresholds.
