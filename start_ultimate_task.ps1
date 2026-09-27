# Starts FMP Ultimate at Windows logon (used by the "FMPUltimate" scheduled task).
# Written 2026-09-27: the task's old one-line command nested double quotes inside -Command "...", so PowerShell
# failed to parse it (task result -1) and Ultimate never started after a reboot.
$ErrorActionPreference = 'Stop'
$root   = 'C:\FMP_Ultimate'
$logDir = 'C:\FMP_Broadcaster\logs'
$note   = Join-Path $logDir 'ultimate_task.log'
function Note($msg) { Add-Content -Path $note -Value ("{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg) }

# Already running? (someone started it by hand) - leave it alone.
if (Get-NetTCPConnection -LocalPort 58000 -State Listen -ErrorAction SilentlyContinue) {
    Note 'Ultimate already listening on 58000 - nothing to do.'
    exit 0
}

# Ultimate reads and writes the music library on Google Drive (G:), which mounts some time after logon.
$deadline = (Get-Date).AddMinutes(10)
while (-not (Test-Path 'G:\My Drive\FMP MUSIC')) {
    if ((Get-Date) -gt $deadline) { Note 'G: drive not mounted after 10 minutes - starting anyway.'; break }
    Start-Sleep -Seconds 15
}

Start-Process -FilePath 'py' -ArgumentList '-u', 'app.py' -WorkingDirectory $root -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logDir 'ultimate_out.log') -RedirectStandardError (Join-Path $logDir 'ultimate_err.log')
Note 'Started Ultimate (py -u app.py).'
