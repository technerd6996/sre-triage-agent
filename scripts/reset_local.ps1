# Wipes ALL data in the local "triage" database (users, settings, runs, audit, agent state) so the next start
# shows the /setup page again. The Postgres install, the database itself and bootstrap.json are kept.
#   1. Stop the app (Ctrl+C in its window).
#   2. powershell -ExecutionPolicy Bypass -File scripts\reset_local.ps1
#   3. powershell -ExecutionPolicy Bypass -File scripts\start.ps1
param([switch]$Yes)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if (-not $Yes) {
    $a = Read-Host "This DELETES every user, setting, run and audit record in the local triage database. Type YES to continue"
    if ($a -ne "YES") { Write-Host "Cancelled."; exit 1 }
}
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "No .venv here. Run scripts\start.ps1 once first." }
$code = @'
import psycopg
from app import bootstrap_store, db
bootstrap_store.load()
with psycopg.connect(db.database_url(), connect_timeout=5, autocommit=True) as c:
    c.execute("drop schema public cascade")
    c.execute("create schema public")
print("database emptied")
'@
$code | & $py -
if ($LASTEXITCODE -ne 0) { throw "Reset failed. Is the app still running? Stop it and try again." }
Write-Host "Done. Now run scripts\start.ps1 - the console will print a setup code for /setup."
