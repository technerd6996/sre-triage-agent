# Backup of the TRIAGE-1 database and its root secrets file. Run by hand or by the scheduled task TRIAGE1-Backup (see -Install).
#   powershell -ExecutionPolicy Bypass -File scripts\backup.ps1            makes one backup now
#   powershell -ExecutionPolicy Bypass -File scripts\backup.ps1 -Install   also schedules it every night at 02:30
# Writes to <TRIAGE_HOME>\backups (default C:\ProgramData\triage\backups), readable only by you and Administrators, keeps 14 days.
# The backup holds the encryption key and the encrypted credentials: copy it OFF this machine to somewhere equally protected.
param([switch]$Install, [int]$KeepDays = 14)
$ErrorActionPreference = "Stop"
$triage = if ($env:TRIAGE_HOME) { $env:TRIAGE_HOME } else { "C:\ProgramData\triage" }
$boot = Join-Path $triage "bootstrap.json"
if (-not (Test-Path $boot)) { throw "bootstrap.json not found in $triage" }

$dest = Join-Path $triage "backups"
New-Item -ItemType Directory -Force -Path $dest | Out-Null
& icacls $dest /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F" "Administrators:(OI)(CI)F" | Out-Null

$cfg = Get-Content $boot -Raw | ConvertFrom-Json
$u = [uri]$cfg.DATABASE_URL
$ui = $u.UserInfo.Split(":", 2)
$pg = Get-ChildItem "C:\Program Files\PostgreSQL\*\bin\pg_dump.exe" | Sort-Object FullName | Select-Object -Last 1
if (-not $pg) { throw "pg_dump.exe was not found under C:\Program Files\PostgreSQL" }

$stamp = Get-Date -Format "yyyyMMdd-HHmm"
$dump = Join-Path $dest "triage-$stamp.dump"
$env:PGPASSWORD = [uri]::UnescapeDataString($ui[1])
try {
    & $pg.FullName -h $u.Host -p $u.Port -U $ui[0] -d $u.AbsolutePath.TrimStart("/") -Fc -f $dump
    if ($LASTEXITCODE -ne 0) { throw "pg_dump failed with exit code $LASTEXITCODE" }
} finally { Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue }
Copy-Item $boot (Join-Path $dest "bootstrap-$stamp.json")

Get-ChildItem $dest -File | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$KeepDays) } | Remove-Item -Force
"{0}: backup written: {1} ({2:N1} MB)" -f (Get-Date -Format s), $dump, ((Get-Item $dump).Length / 1MB)

if ($Install) {
    $self = $MyInvocation.MyCommand.Path
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$self`""
    $trigger = New-ScheduledTaskTrigger -Daily -At "02:30"
    $me = "$($env:USERDOMAIN)\$($env:USERNAME)"
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1)
    try {
        # S4U: runs whether or not you are signed in, without storing your password (local resources only, which is all this needs).
        # It needs the "log on as a batch job" right, so it can be refused in a non-elevated session.
        $principal = New-ScheduledTaskPrincipal -UserId $me -LogonType S4U -RunLevel Limited
        Register-ScheduledTask -TaskName "TRIAGE1-Backup" -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force -ErrorAction Stop | Out-Null
        "Scheduled task TRIAGE1-Backup registered: every night at 02:30 as $me, signed in or not"
    } catch {
        # Fallback: runs only while you are signed in; if you were signed out at 02:30 it runs when you next sign in (StartWhenAvailable).
        $principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName "TRIAGE1-Backup" -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
        "Scheduled task TRIAGE1-Backup registered: every night at 02:30 as $me, ONLY WHILE SIGNED IN (S4U was refused: $($_.Exception.Message.Trim())). Run this script elevated with -Install to make it run signed out."
    }
}
