@echo off
title AutoRouting Sync — CE Connect to Supabase
cd /d "C:\Users\kimbe\Documents\autorouting_project\autorouting\sync"

:loop
echo.
echo [%date% %time%] Starting sync...
python sync.py
echo.
echo [%date% %time%] Sync stopped (exit code %errorlevel%). Restarting in 30 seconds...
echo Press Ctrl+C to cancel restart.
timeout /t 30 /nobreak >nul
goto loop
