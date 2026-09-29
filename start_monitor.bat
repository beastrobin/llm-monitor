@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PY="
if exist "%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe" (
  set "PY=%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe"
)
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Python was not found. Set the PYTHON environment variable to an
  echo interpreter path, or add python to PATH.
  pause
  exit /b 1
)

rem --- probe: is the dashboard already listening? ---
set "PROBE_OK="
for /f "tokens=*" %%H in (
  "%PY%" -c "import urllib.request; urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:8090/api/snapshot').read()" 2^>nul
) do ( set "PROBE_OK=1" & goto :probe_done )
:probe_done

if defined PROBE_OK goto :already_running

rem --- first launch: start monitor.py HIDDEN via WSH (zero console flicker) ---
>"%TEMP%\_llm_monitor_launch.vbs" echo Set WshShell = CreateObject("WScript.Shell")
>>"%TEMP%\_llm_monitor_launch.vbs" echo WshShell.Run """%PY%"" monitor.py %*", 0, false
cscript //nologo "%TEMP%\_llm_monitor_launch.vbs" >nul 2>&1
del "%TEMP%\_llm_monitor_launch.vbs" >nul 2>&1

rem Give it a heartbeat to bind the port.
"%SystemRoot%\System32\ping.exe" -n 3 127.0.0.1 >nul

rem Open the browser and exit -- no pause, no window left behind.
start "" "http://127.0.0.1:8090"
exit /b 0

:already_running
start "" "http://127.0.0.1:8090"
exit /b 0
