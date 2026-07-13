@echo off
REM Wrapper for Windows Task Scheduler. Runs fetch_weather.py using whichever
REM "python" is on PATH, from this script's own folder, so it works regardless
REM of what folder Task Scheduler starts in.
cd /d "%~dp0"
python fetch_weather.py
