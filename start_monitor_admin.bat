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
set "PROBE_OK="
if defined PY (
  for /f "tokens=*" %%H in (
    "%PY%" -c "import urllib.request; urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:8090/api/snapshot').read()" 2^>nul
  ) do ( set "PROBE_OK=1" & goto :probe_done )
)
:probe_done

if defined PROBE_OK goto :already_running

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
