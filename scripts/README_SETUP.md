# Setting up the data-fetch script on your PC (Windows)

This folder (`scripts/`) contains the program that fetches weather data and
publishes it to the dashboard. It's designed to run from a normal clone of
this repository on your own machine, on a schedule, so the public dashboard
always shows current data without you having to do anything by hand.

## How it fits together

- **`config/sites.json`** (one level up) — the list of sites you want tracked. Edit this to add, rename or remove sites.
- **`scripts/fetch_weather.py`** — pulls weather, soil, and rainfall data for every site in `sites.json`, works out Growing Degree Days, dollar spot disease risk and a simple irrigation water balance, turns those into turf advisor recommendations, updates each site's analysis history, and writes the results into `data/`.
- **`scripts/run_fetch.bat`** — a one-line wrapper so Windows Task Scheduler can run the Python script.
- **`index.html`** — the public dashboard. It just reads the JSON files in `data/`, so as soon as `fetch_weather.py` commits and pushes new data, the live site updates too.
- **`about.html`** — the glossary and methodology page, linked from the dashboard header/footer.

The script only uses Python's standard library — nothing needs to be
`pip install`-ed.

## One-off setup

1. **Install Python**, if you don't already have it: [python.org/downloads](https://www.python.org/downloads/). During install, tick "Add python.exe to PATH".

2. **Clone the repository** to a folder you're happy to leave in place long-term, e.g.:

   ```
   cd C:\Users\Michael\Documents
   git clone https://github.com/<your-username>/farmingreport.git
   ```

   This folder *is* the "working files" folder — the script commits and pushes from inside it, so don't move it once it's set up.

3. **Check git can push without prompting for a password every time.** If `git clone` above worked and you're not asked for credentials repeatedly when you `git push`, you're already set up (e.g. via Git Credential Manager or an SSH key) and can skip to step 4.

4. **Test it once by hand.** Open Command Prompt in the cloned folder and run:

   ```
   cd farmingreport\scripts
   python fetch_weather.py
   ```

   You should see log lines for each site, and `data\index.json` / `data\sites\*.json` should be updated. Check `scripts\fetch.log` if anything looks wrong — every run appends a timestamped line there.

5. **Register the scheduled task.** Open Command Prompt **as Administrator** and run (adjust the path to wherever you cloned the repo):

   ```
   schtasks /create /tn "FarmingReport Weather Update" /tr "\"C:\Users\Michael\Documents\farmingreport\scripts\run_fetch.bat\"" /sc hourly /mo 3 /rl highest
   ```

   This runs the script every 3 hours. Adjust `/mo 3` (hours) to taste — weather/turf data doesn't usually need updating more often than every few hours. You can also use `/sc daily /st 06:00` for a single fixed time per day instead.

   Prefer the GUI? Open **Task Scheduler** → *Create Task* → on the **Triggers** tab add a new trigger set to repeat every few hours → on the **Actions** tab set "Start a program" to `run_fetch.bat` with "Start in" set to the `scripts` folder.

6. **Confirm it's working**: after the next scheduled run, check `scripts\fetch.log`, then check the live dashboard URL — it should show the new "last updated" time.

## Adding more sites

Edit `config\sites.json`. Only `name` and `postcode` are required:

```json
{
  "sites": [
    { "name": "Wilberfoss", "postcode": "YO41 5YZ" },
    { "name": "Hougham", "postcode": "NG32 2JD", "gdd_base_temp_c": 6, "season_start": "03-01" }
  ]
}
```

Optional fields per site:

- `slug` — used in the data filename and the dashboard's URL; auto-generated from `name` if omitted.
- `gdd_base_temp_c` — Growing Degree Day base temperature in °C (default `6`, a common cool-season turf/grass baseline; use whatever's right for what you're growing).
- `season_start` — `MM-DD` when GDD accumulation resets each year (default `01-01`).
- `dollar_spot_threshold_pct` — the risk % at which the dashboard flags a spray-worthy risk (default `20`, per the Smith-Kerns model's usual guidance).
- `flood_search_radius_km` — how far to search for an Environment Agency rainfall station (default `15`). EA data only covers England & Wales; sites elsewhere in the UK will show no rainfall-station data (everything else still works).

Save the file, and the next scheduled run will pick up the change automatically — no need to touch the script itself.

## Notes on the disease/irrigation figures

- **Dollar spot risk** uses the published Smith-Kerns logistic regression model (5-day average relative humidity and temperature). It's a widely used turf-management model, not a guarantee — treat the 20% threshold as a prompt to go and inspect the turf, not an automatic spray trigger.
- **Water balance** is a simple 7-day running total of ET0 (reference evapotranspiration) minus rainfall. A "deficit" means more moisture has left than has fallen recently — a signal to consider irrigation, not a soil-moisture measurement in itself (the dashboard also shows Open-Meteo's modelled soil moisture separately).
- All figures are modelled from Open-Meteo's forecast/historical weather data, not on-site sensors. Cross-check against real sensors if the dashboard ever informs a spray or high-stakes irrigation decision.
- The full list of advisor thresholds and formulas is documented on the dashboard's `about.html` page, not just here — that's the version to point Michael (or anyone else) at.

## Turf advisor recommendations

Each run, `fetch_weather.py` turns the figures above into a short list of
plain-English recommendations (`build_recommendations()` in the script),
each tagged "On track", "Watch" or "Action". It's a fixed set of rules
against documented thresholds, not a machine-learning model — see
`about.html` for the exact numbers. These are written into each site's JSON
as `recommendations` and `top_severity`, and rendered as the "Turf advisor"
panel on the dashboard.

## Analysis history

Alongside each site's main JSON file, the script maintains
`data/sites/<slug>-history.json` — a compact daily snapshot (temperature,
GDD, dollar spot risk, water balance, soil moisture, leaf wetness and that
day's advisor severity) that grows by one entry per calendar day, however
often the schedule actually runs. Re-running on the same day updates that
day's entry rather than duplicating it; the very first run backfills
whatever history the weather API already returned so the trend charts
aren't a single dot on day one. This file — not just the current run's own
data — is what the dashboard's GDD, dollar spot and temperature charts read
from, so they show the site's history growing over time rather than a
fixed window. It's capped at 730 days per site to keep it a sane size.
