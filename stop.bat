@echo off
rem Stop the study site: background autostart (pythonw) or a start.bat window.
rem Finds processes started from this project's .venv and their child processes
rem (the venv launcher runs the real interpreter as a child).
rem Only ASCII here: cmd.exe misreads batch files with UTF-8 Cyrillic text.
powershell -NoProfile -Command "$all = Get-CimInstance Win32_Process; $own = @($all | Where-Object { $_.ExecutablePath -like '*\study-site\.venv\*' -or $_.CommandLine -like '*study-site*app.py*' }); $ids = @($own | ForEach-Object { $_.ProcessId }); $kids = @($all | Where-Object { $ids -contains $_.ParentProcessId } | ForEach-Object { $_.ProcessId }); $targets = @($ids + $kids | Select-Object -Unique); if ($targets.Count) { $targets | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }; Write-Host 'Study site stopped.' } else { Write-Host 'Study site is not running.' }"
pause
