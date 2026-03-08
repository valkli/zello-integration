$skillDir = "C:\Users\Val\.openclaw\skills\zello"
$scriptName = "zello_skill.py"

$running = Get-Process python* -ErrorAction SilentlyContinue | Where-Object {
    (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)" -ErrorAction SilentlyContinue).CommandLine -like "*zello_skill*"
}

if (-not $running) {
    Write-Host "[Watchdog] Ne zapuschen - zapuskaem..."
    Start-Process python -ArgumentList "-X utf8 $scriptName" -WorkingDirectory $skillDir -RedirectStandardOutput "$skillDir\zello_out.log" -RedirectStandardError "$skillDir\zello_err.log" -WindowStyle Hidden
    Write-Host "[Watchdog] Zapuschen"
} else {
    Write-Host "[Watchdog] Rabotaet (PID $($running.Id))"
}
