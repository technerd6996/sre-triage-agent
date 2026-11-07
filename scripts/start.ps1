# One command to run the app from a fresh clone:  powershell -ExecutionPolicy Bypass -File scripts\start.ps1
# No .env file. On the first run it
#   1. installs PostgreSQL 17 locally if it is missing (needs an elevated PowerShell),
#   2. creates the "triage" database and a role with a random password,
#   3. writes DATABASE_URL and a random TRIAGE_SECRET_KEY to C:\ProgramData\triage\bootstrap.json (owner + Administrators only),
#   4. applies db\schema.sql (safe to repeat).
# Every integration setting (ServiceNow, WinRM, AWS, LDAP ...) is then entered on the setup web page.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function New-Secret([int]$n) { -join ((48..57) + (65..90) + (97..122) | Get-Random -Count $n | ForEach-Object { [char]$_ }) }

# --- Python environment ---
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "Creating .venv and installing requirements..."
    python -m venv .venv
    & $py -m pip install -q -r requirements.txt
}

# --- PostgreSQL ---
function Get-PgService { Get-Service | Where-Object { $_.Name -like "postgresql*" } | Select-Object -First 1 }
$svc = Get-PgService
$firstRun = $false
& $py -m app.bootstrap_store has 2>$null
if ($LASTEXITCODE -ne 0) { $firstRun = $true }

if (-not $svc) {
    Write-Host "Installing PostgreSQL 17 (one time)..."
    $tmp = New-Secret 32            # throw-away superuser password, never stored
    winget install --id PostgreSQL.PostgreSQL.17 --source winget --accept-package-agreements --accept-source-agreements --silent `
        --custom "--mode unattended --unattendedmodeui none --superpassword $tmp --serverport 5432 --disable-components stackbuilder"
    $svc = Get-PgService
    if (-not $svc) { throw "PostgreSQL did not install. Run this script from an elevated PowerShell." }
}
if ($svc.Status -ne "Running") { Start-Service $svc.Name }

if ($firstRun) {
    $pgRoot = Get-ChildItem "C:\Program Files\PostgreSQL" -Directory | Sort-Object Name -Descending | Select-Object -First 1
    $data = Join-Path $pgRoot.FullName "data"; $psql = Join-Path $pgRoot.FullName "bin\psql.exe"
    $appPw = New-Secret 32
    # Temporarily allow passwordless localhost login for the postgres account, only to create our own role.
    Copy-Item "$data\pg_hba.conf" "$data\pg_hba.conf.orig" -Force
    try {
        Set-Content "$data\pg_hba.conf" (@("host all postgres 127.0.0.1/32 trust", "host all postgres ::1/128 trust") + (Get-Content "$data\pg_hba.conf.orig")) -Encoding ascii
        Restart-Service $svc.Name; Start-Sleep 4
        $env:PGHOST = "127.0.0.1"
        & $psql -U postgres -d postgres -v ON_ERROR_STOP=1 -c "create role triage login password '$appPw'" -c "create database triage owner triage"
        if ($LASTEXITCODE -ne 0) { throw "Could not create the triage role/database. If they already exist from an earlier run, delete them or restore bootstrap.json." }
    } finally {
        Copy-Item "$data\pg_hba.conf.orig" "$data\pg_hba.conf" -Force; Remove-Item "$data\pg_hba.conf.orig" -ErrorAction SilentlyContinue
        Restart-Service $svc.Name; Start-Sleep 3
    }
    # listen on localhost only
    (Get-Content "$data\postgresql.conf") -replace "^\s*#?\s*listen_addresses\s*=.*", "listen_addresses = 'localhost'" | Set-Content "$data\postgresql.conf" -Encoding ascii
    Restart-Service $svc.Name; Start-Sleep 3
    $key = & $py -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    (@{ DATABASE_URL = "postgresql://triage:$appPw@127.0.0.1:5432/triage"; TRIAGE_SECRET_KEY = $key } | ConvertTo-Json) | & $py -m app.bootstrap_store merge
    Write-Host "First-run database provisioning done."
}

# --- schema (idempotent) and start ---
$apply = @'
import time, psycopg
from app import bootstrap_store, db
bootstrap_store.load()
for _ in range(30):
    try:
        with psycopg.connect(db.database_url(), connect_timeout=3, autocommit=True) as c:
            c.execute(open("db/schema.sql", encoding="utf-8").read())
        print("database ready"); raise SystemExit(0)
    except psycopg.OperationalError:
        time.sleep(1)
raise SystemExit("database did not become ready in 30s")
'@
$apply | & $py -
if ($LASTEXITCODE -ne 0) { throw "database check failed" }
& $py -m uvicorn app.main:app --host 127.0.0.1 --port 8000
