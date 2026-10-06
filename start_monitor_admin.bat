@echo off
setlocal
cd /d "%~dp0"

set "PY="
if exist "%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe" (
  set "PY=%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe"
)
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)

rem --- probe: is the dashboard already listening? ---
rem A plain invocation, not a for /f loop: nesting this quoted command inside
rem a loop's command set makes cmd misparse the interpreter path, and every
rem launch printed 'C:\...\python.exe" -c "..."' is not recognized.  The exit
rem code already answers the only question being asked here.
set "PROBE_OK="
if defined PY (
  "%PY%" -c "import urllib.request; urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:8090/api/snapshot', timeout=3).read()" >nul 2>&1
  if not errorlevel 1 set "PROBE_OK=1"
)

if defined PROBE_OK goto :already_running

rem --- adopt the api-key the router generated when it started ---
rem Hermes regenerates its key on every start, so a config.json left over from
rem an earlier run would answer 401 and the dashboard would show no models.
rem Best-effort by design: if the helper cannot find the router's log it
rem leaves the config alone, and the dashboard reports the 401 exactly as it
rem did before.
if defined PY (
  if exist "tools\sync_hermes_key.py" "%PY%" "tools\sync_hermes_key.py" >nul 2>&1
)

rem --- elevated restart if needed ---
net session >nul 2>&1
if %errorlevel% neq 0 goto :request_elevate

:run
>"%TEMP%\_llm_monitor_launch.vbs" echo Set WshShell = CreateObject("WScript.Shell")
>>"%TEMP%\_llm_monitor_launch.vbs" echo WshShell.Run """%PY%"" monitor.py %*", 0, false
cscript //nologo "%TEMP%\_llm_monitor_launch.vbs" >nul 2>&1
del "%TEMP%\_llm_monitor_launch.vbs" >nul 2>&1
"%SystemRoot%\System32\ping.exe" -n 3 127.0.0.1 >nul
start "" "http://127.0.0.1:8090"
exit /b 0

:request_elevate
>"%TEMP%\_llm_monitor_elevate.vbs" echo Set objShell = CreateObject("Shell.Application")
>>"%TEMP%\_llm_monitor_elevate.vbs" echo objShell.ShellExecute """%CD%\start_monitor_admin.bat""", """%*""", """", """runas""", 0
cscript //nologo "%TEMP%\_llm_monitor_elevate.vbs" >nul 2>&1
del "%TEMP%\_llm_monitor_elevate.vbs" >nul 2>&1
exit /b

:already_running
start "" "http://127.0.0.1:8090"
exit /b
