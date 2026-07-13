# Wrapper for Windows Task Scheduler. Runs fetch_weather.py using whichever
# "python" is on PATH, from this script's own folder, so it works regardless
# of what folder Task Scheduler starts in.
Set-Location -Path $PSScriptRoot
python fetch_weather.py
