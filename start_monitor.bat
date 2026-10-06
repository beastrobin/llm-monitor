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
rem A plain invocation, not a for /f loop: nesting this quoted command inside
rem a loop's command set makes cmd misparse the interpreter path, and every
rem launch printed 'C:\...\python.exe" -c "..."' is not recognized.  The exit
rem code already answers the only question being asked here.
set "PROBE_OK="
"%PY%" -c "import urllib.request; urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:8090/api/snapshot', timeout=3).read()" >nul 2>&1
if not errorlevel 1 set "PROBE_OK=1"

if defined PROBE_OK goto :already_running

rem --- adopt the api-key the router generated when it started ---
rem Hermes regenerates its key on every start, so a config.json left over from
rem an earlier run would answer 401 and the dashboard would show no models.
rem Best-effort by design: if the helper cannot find the router's log it
rem leaves the config alone, and the dashboard reports the 401 exactly as it
rem did before.
if exist "tools\sync_hermes_key.py" (
  "%PY%" "tools\sync_hermes_key.py" >nul 2>&1
)

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
