@echo off
rem Start the study site in the background with a tray icon (no console window).
rem Stop it from the tray icon menu or with stop.bat. Log: instance\server.log
rem For debugging with a console window run:  .venv\Scripts\python.exe app.py
rem Only ASCII here: cmd.exe misreads batch files with UTF-8 Cyrillic text.
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
    echo Virtual environment not found. Do the setup steps from README.md first.
    pause
    exit /b 1
)
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0tray.py"
