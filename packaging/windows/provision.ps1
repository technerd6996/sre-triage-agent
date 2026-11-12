# Run by the installer, as administrator. Safe to repeat: an existing bootstrap.json keeps its database.
param([Parameter(Mandatory)][string]$AppDir)
$ErrorActionPreference = "Stop"
$data = Join-Path $env:ProgramData "triage"
New-Item -ItemType Directory -Force $data | Out-Null
Start-Transcript -Path (Join-Path $data "install.log") -Force | Out-Null

$exe = Join-Path $AppDir "triage1.exe"
$launch = Join-Path $AppDir "launch.py"
$bin = Join-Path $AppDir "pgsql\bin"
$svcDir = Join-Path $AppDir "service"
$account = "NT SERVICE\TRIAGE1"
$pgService = "triage1-postgres"

function New-Hex([int]$bytes) {
    $b = New-Object byte[] $bytes
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
    -join ($b | ForEach-Object { $_.ToString("x2") })
}
function New-FernetKey {
    $b = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
    [Convert]::ToBase64String($b).Replace("+", "-").Replace("/", "_")
}
function Assert-Ok($what) { if ($LASTEXITCODE) { throw "$what failed (exit $LASTEXITCODE)" } }
function Test-PortFree([int]$p) {
    $l = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, $p)
    try { $l.Start(); $true } catch { $false } finally { try { $l.Stop() } catch {} }
}

try {
    $port = 8000
    if (-not (Get-Service TRIAGE1 -ErrorAction SilentlyContinue | Where-Object Status -eq Running) -and -not (Test-PortFree $port)) {
        throw "Port $port is already in use. Stop whatever is listening on it, then run the installer again."
    }

    # An existing install keeps its database. Otherwise the bundled PostgreSQL is set up below.
    & $exe $launch bootstrap-has
    $hasDb = ($LASTEXITCODE -eq 0)
    if ($hasDb) {
        $found = Get-Service "postgresql*", $pgService -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $found) { throw "bootstrap.json points to a database, but no PostgreSQL service was found on this machine." }
        $depend = $found.Name
    } else {
        $depend = $pgService
    }

    # The service has to exist before its account can be given rights on anything.
    $xml = (Get-Content "$svcDir\TRIAGE1.xml.template" -Raw).Replace("@APP@", $AppDir).Replace("@HOME@", $data).Replace("@DEPEND@", $depend)
    Set-Content "$svcDir\TRIAGE1.xml" $xml -Encoding UTF8
    if (-not (Get-Service TRIAGE1 -ErrorAction SilentlyContinue)) {
        if (-not $hasDb) {
            # The dependency must exist when the service is registered, so PostgreSQL comes first on a new install.
            $pgdata = Join-Path $data "pgdata"
            if ((Test-Path $pgdata) -and (Get-ChildItem $pgdata -Force | Select-Object -First 1)) {
                throw "$pgdata already holds data but bootstrap.json is missing. Restore bootstrap.json from a backup, or move that folder away."
            }
            $pgPort = 5432..5450 | Where-Object { Test-PortFree $_ } | Select-Object -First 1
            if (-not $pgPort) { throw "No free port for PostgreSQL between 5432 and 5450." }
            New-Item -ItemType Directory -Force $pgdata | Out-Null
            & icacls $pgdata /inheritance:r /grant:r "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" "*S-1-5-20:(OI)(CI)F" | Out-Null
            Assert-Ok "Setting folder permissions"

            $super = New-Hex 24
            $pw = Join-Path $data "pw.tmp"
            try {
                [IO.File]::WriteAllText($pw, $super)
                & icacls $pw /inheritance:r /grant:r "*S-1-5-32-544:F" "*S-1-5-18:F" | Out-Null
                & "$bin\initdb.exe" -D $pgdata -U postgres -A scram-sha-256 "--pwfile=$pw" -E UTF8 --no-locale
                Assert-Ok "initdb"
            } finally { Remove-Item $pw -Force -ErrorAction SilentlyContinue }
            Add-Content "$pgdata\postgresql.conf" "`nlisten_addresses = 'localhost'`nport = $pgPort" -Encoding ascii
            & icacls $pgdata /grant "*S-1-5-20:(OI)(CI)F" /T | Out-Null
            & "$bin\pg_ctl.exe" register -N $pgService -D $pgdata -U "NT AUTHORITY\NetworkService" -S auto
            Assert-Ok "Registering PostgreSQL"
            Start-Service $pgService

            $env:PGPASSWORD = $super
            $ready = $false
            foreach ($i in 1..30) {
                & "$bin\pg_isready.exe" -h 127.0.0.1 -p $pgPort -q
                if ($LASTEXITCODE -eq 0) { $ready = $true; break }
                Start-Sleep 1
            }
            if (-not $ready) { throw "PostgreSQL did not start." }
            $appPw = New-Hex 24
            "create role triage login password '$appPw';" | & "$bin\psql.exe" -h 127.0.0.1 -p $pgPort -U postgres -d postgres -q -v ON_ERROR_STOP=1 -f -
            Assert-Ok "Creating the database role"
            "create database triage owner triage;" | & "$bin\psql.exe" -h 127.0.0.1 -p $pgPort -U postgres -d postgres -q -v ON_ERROR_STOP=1 -f -
            Assert-Ok "Creating the database"
            Remove-Item Env:\PGPASSWORD
            $newBootstrap = @{ DATABASE_URL = "postgresql://triage:$appPw@127.0.0.1:$pgPort/triage"; TRIAGE_SECRET_KEY = (New-FernetKey) } | ConvertTo-Json
        }
        & "$svcDir\TRIAGE1.exe" install
        Assert-Ok "Registering the TRIAGE-1 service"
    } else {
        & "$svcDir\TRIAGE1.exe" refresh
        Assert-Ok "Updating the TRIAGE-1 service"
    }
    & sc.exe config TRIAGE1 obj= $account | Out-Null
    Assert-Ok "Setting the service account"
    & sc.exe failure TRIAGE1 reset= 3600 actions= restart/10000/restart/30000/restart/60000 | Out-Null

    # The service account writes logs, backups and telemetry state here. The key file is granted separately.
    & icacls $data /grant "${account}:(RX,W)" | Out-Null
    Assert-Ok "Granting the data folder"
    foreach ($sub in "logs", "backups") {
        $p = Join-Path $data $sub
        New-Item -ItemType Directory -Force $p | Out-Null
        & icacls $p /grant "${account}:(OI)(CI)M" | Out-Null
    }

    $env:TRIAGE_SERVICE_ACCOUNT = $account
    if ($newBootstrap) {
        $newBootstrap | & $exe $launch bootstrap-merge
        Assert-Ok "Saving the database address"
    }
    $bootstrap = Join-Path $data "bootstrap.json"
    & icacls $bootstrap /grant "${account}:F" | Out-Null
    Assert-Ok "Granting the key file"

    Start-Service TRIAGE1
    $up = $false
    foreach ($i in 1..120) {
        try {
            if ((Invoke-WebRequest "http://127.0.0.1:$port/api/health" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200) { $up = $true; break }
        } catch {}
        Start-Sleep 1
    }
    if (-not $up) { throw "The service did not answer within two minutes. See $data\logs." }
    Write-Host "TRIAGE-1 is running at http://127.0.0.1:$port"
} catch {
    Write-Host "INSTALL FAILED: $($_.Exception.Message)"
    Stop-Transcript | Out-Null
    exit 1
}
Stop-Transcript | Out-Null
