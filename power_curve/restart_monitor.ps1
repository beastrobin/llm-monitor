# Restart llm-monitor (elevated): kill the 127.0.0.1:8090 holder, relaunch elevated.
$log = 'D:\AI\llm-monitor\power_curve\restart_log.txt'
"restart started $((Get-Date).ToString('o'))" | Set-Content $log
$ErrorActionPreference = 'Continue'
$py = 'D:\AI\hermes\hermes-agent\venv\Scripts\python.exe'
$conns = Get-NetTCPConnection -LocalPort 8090 -State Listen -ErrorAction SilentlyContinue
$holder = $null
if ($conns) { $holder = $conns[0].OwningProcess }
if ($holder) {
    Stop-Process -Id $holder -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    "killed old monitor pid $holder" | Add-Content $log
}
Start-Process -FilePath $py -ArgumentList 'monitor.py' -WorkingDirectory 'D:\AI\llm-monitor'
"relaunched $((Get-Date).ToString('o'))" | Add-Content $log
