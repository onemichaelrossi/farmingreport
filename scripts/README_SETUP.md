# Setting up the data-fetch script on your PC (Windows)

This folder (`scripts/`) contains the program that fetches weather data and
publishes it to the dashboard. It's designed to run from a normal clone of
this repository on your own machine, on a schedule, so the public dashboard
always shows current data without you having to do anything by hand.

## How it fits together

- **`config/sites.json`** (one level up) — the list of sites you want tracked. Edit this to add, rename or remove sites.
- **`scripts/fetch_weather.py`** — pulls weather, soil, and rainfall data for every site in `sites.json`, works out Growing Degree Days, dollar spot disease risk and a simple irrigation water balance, turns those into turf advisor recommendations, updates each site's analysis history, and writes the results into `data/`. It also commits and pushes those `data/` changes to your git remote itself — you don't need to run `git add`/`git commit`/`git push` by hand.
- **`scripts/run_fetch.bat`** / **`scripts/run_fetch.ps1`** — one-line wrappers so Windows Task Scheduler can run the Python script; use whichever matches how you'd rather set up the scheduled task (Command Prompt or PowerShell). They do the same thing.
- **`index.html`** — the public dashboard. It just reads the JSON files in `data/`, so as soon as `fetch_weather.py` commits and pushes new data, the live site updates too.
- **`about.html`** — the glossary and methodology page, linked from the dashboard header/footer.

The script only uses Python's standard library — nothing needs to be
`pip install`-ed.

## One-off setup

These steps are shown for **PowerShell** (the default terminal in modern Windows — right-click Start → "Windows PowerShell" or "Terminal"). Command Prompt works identically for steps 1, 2 and 4; only the scheduled-task registration (step 5) differs, and both a Command Prompt and a PowerShell version are given.

1. **Install Python**, if you don't already have it: [python.org/downloads](https://www.python.org/downloads/). During install, tick "Add python.exe to PATH". Restart your PowerShell window afterwards so it picks up the updated PATH.

2. **Install Git**, if you don't already have it: [git-scm.com/downloads](https://git-scm.com/downloads) (or `winget install --id Git.Git -e` in PowerShell). This also gives you Git Credential Manager, which handles step 3 below automatically.

3. **Clone the repository** to a folder you're happy to leave in place long-term, e.g.:

   ```powershell
   cd $HOME\Documents
   git clone https://github.com/onemichaelrossi/farmingreport.git
   ```

   This folder *is* the "working files" folder — `fetch_weather.py` commits and pushes from inside it, so don't move it once it's set up. The first `git push` (or the first run of the script, since it pushes for you) may prompt you to sign in to GitHub in a browser window — that's Git Credential Manager, and it only asks once.

4. **Test it once by hand:**

   ```powershell
   cd $HOME\Documents\farmingreport\scripts
   python fetch_weather.py
   ```

   You should see log lines for each site, `data\index.json` / `data\sites\*.json` should be updated, and — because the script commits and pushes automatically — you should see `pushed to remote.` near the end of the log output. Check `scripts\fetch.log` if anything looks wrong — every run appends a timestamped line there.

5. **Register the scheduled task.** Open PowerShell **as Administrator** (right-click PowerShell → "Run as administrator") and run (adjust the path if you cloned somewhere other than `Documents`):

   ```powershell
   $repoPath = "$HOME\Documents\farmingreport"
   $action   = New-ScheduledTaskAction -Execute "powershell.exe" `
                 -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$repoPath\scripts\run_fetch.ps1`""
   $trigger  = New-ScheduledTaskTrigger -Once -At (Get-Date) `
                 -RepetitionInterval (New-TimeSpan -Hours 3) -RepetitionDuration ([TimeSpan]::MaxValue)
   Register-ScheduledTask -TaskName "FarmingReport Weather Update" -Action $action -Trigger $trigger -RunLevel Highest
   ```

   This runs the script every 3 hours, starting now. Adjust `-Hours 3` to taste — weather/turf data doesn't usually need updating more often than every few hours. For a single fixed time per day instead, swap the trigger for:

   ```powershell
   $trigger = New-ScheduledTaskTrigger -Daily -At "6:00am"
   ```

   Prefer Command Prompt instead? The equivalent one-liner (also run as Administrator) is:

   ```
   schtasks /create /tn "FarmingReport Weather Update" /tr "\"C:\Users\Michael\Documents\farmingreport\scripts\run_fetch.bat\"" /sc hourly /mo 3 /rl highest
   ```

   Prefer the GUI? Open **Task Scheduler** → *Create Task* → on the **Triggers** tab add a new trigger set to repeat every few hours → on the **Actions** tab set "Start a program" to `run_fetch.bat` (or `powershell.exe` with argument `-File run_fetch.ps1`) with "Start in" set to the `scripts` folder.

6. **Confirm it's working**: after the next scheduled run, check `scripts\fetch.log`, then check the live dashboard URL — it should show the new "last updated" time. You can also run it on demand any time from PowerShell with `Start-ScheduledTask -TaskName "FarmingReport Weather Update"`.

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
