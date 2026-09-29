# Power-curve sweep: set GPU cap per tier, run decode bench, log JSONL.
# Runs elevated (single UAC). Restores 300W at the end.
$log = 'D:\AI\llm-monitor\power_curve\sweep_log.txt'
"elevated sweep started $((Get-Date).ToString('o'))" | Set-Content $log
$ErrorActionPreference = 'Continue'
$tiers = 150,200,250,300,350,400,450,500,550,600
$py = 'D:\AI\hermes\hermes-agent\venv\Scripts\python.exe'
$smi = 'C:\Windows\System32\nvidia-smi.exe'
$bench = 'D:\AI\llm-monitor\power_curve\bench_power.py'
$out = 'D:\AI\llm-monitor\power_curve\results_150_600.jsonl'
$progress = 'D:\AI\llm-monitor\power_curve\progress.txt'
Remove-Item $out -ErrorAction SilentlyContinue
"log start $((Get-Date).ToString('o'))" | Set-Content $progress
try {
    if (-not (Test-Path $py)) { throw "python not found: $py" }
    $smiVer = & $smi --version 2>&1 | Out-String
    "smi: $($smiVer.Trim().Substring(0, [Math]::Min(60, $smiVer.Trim().Length)))" | Add-Content $log
    "smi rc=$LASTEXITCODE" | Add-Content $log
} catch {
    "SETUP FAILED: $_" | Add-Content $log
    throw
}

function Set-Cap([int]$w) {
    & $smi -i 0 -pl $w *> $null
    $tries = 0
    while ($tries -lt 20) {
        $tries = $tries + 1
        $cur = (& $smi -i 0 --query-gpu=power.limit --format=csv,noheader,nounits).Trim()
        $ok = $false
        try { $ok = [math]::Abs([double]$cur - $w) -lt 0.5 } catch {}
        if ($ok) { return [double]$cur }
        Start-Sleep -Milliseconds 400
        & $smi -i 0 -pl $w *> $null
    }
    return [double]$cur
}

try {
    foreach ($w in $tiers) {
        "tier $w start $((Get-Date).ToString('HH:mm:ss'))" | Add-Content $progress
        "tier $w start" | Add-Content $log
        $applied = Set-Cap $w
        "tier $w applied=$applied" | Add-Content $log
        Start-Sleep -Seconds 5   # let clocks/power settle under the new cap
        & $py $bench --watts $w --applied $applied 2>&1 | Add-Content $out
        "bench rc=$LASTEXITCODE tier $w" | Add-Content $log
        "tier $w done $((Get-Date).ToString('HH:mm:ss'))" | Add-Content $progress
        Start-Sleep -Seconds 2
    }
    # restore the user's working setting
    Set-Cap 300
    "restored 300W $((Get-Date).ToString('o'))" | Add-Content $progress
    "sweep complete $((Get-Date).ToString('o'))" | Add-Content $log
} catch {
    "TIER LOOP FAILED: $_" | Add-Content $log
    try { Set-Cap 300; "restored 300W after failure" | Add-Content $log } catch {}
    throw
}
