# Builds the Windows installer: TRIAGE-1-Setup-<version>.exe
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1 -Version 1.0.0
# Only TRIAGE-1's own code (the app package) is compiled. Python and the open-source libraries ship unchanged, which
# keeps the build short and leaves the LGPL libraries replaceable.
# Needs: the C++ tools of Visual Studio Build Tools, Inno Setup 6, PostgreSQL 17 on this machine, and $Out\venv with Nuitka.
param([string]$Version = "1.0.0", [string]$Out = "D:\triage-build", [switch]$SkipCompile, [switch]$SkipTest)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $Out "venv\Scripts\python.exe"
$module = Join-Path $Out "module"
$stage = Join-Path $Out "stage"
$app = Join-Path $stage "root"

if ((Get-Content app\version.py -Raw) -notmatch "VERSION = `"$([regex]::Escape($Version))`"") { throw "app\version.py does not say $Version" }

function Step($t) { Write-Host "`n=== $t" }
function Assert-Ok($what) { if ($LASTEXITCODE) { throw "$what failed (exit $LASTEXITCODE)" } }
function Copy-Tree($from, $to, [string[]]$skipDirs = @()) {
    $a = @($from, $to, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/XF", "*.pyc", "*.bak")
    if ($skipDirs) { $a += "/XD"; $a += $skipDirs }
    & robocopy @a | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Copying $from failed (robocopy $LASTEXITCODE)" }
    $global:LASTEXITCODE = 0
}

if (-not $SkipCompile) {
    Step "Compiling the app package"
    Remove-Item $module -Recurse -Force -ErrorAction SilentlyContinue
    $env:PYTHONPATH = $root
    & $py -m nuitka --mode=module --include-package=app --no-pyi-file --msvc=latest --assume-yes-for-downloads --jobs=3 `
        "--output-dir=$module" --remove-output app
    Assert-Ok "Nuitka"
    Remove-Item Env:\PYTHONPATH
}
$pyd = Get-ChildItem $module -Filter "app.*.pyd" | Select-Object -First 1
if (-not $pyd) { throw "Compiled app package not found in $module" }
Write-Host ("Compiled app: {0:N1} MB" -f ($pyd.Length / 1MB))

Step "Staging Python, libraries and data"
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $app, "$app\service", "$app\pgsql", "$app\db" | Out-Null
$base = (& $py -c "import sys; print(sys.base_prefix)").Trim()
Copy-Item "$base\python.exe" "$app\triage1.exe"
foreach ($f in "python313.dll", "python3.dll", "vcruntime140.dll", "vcruntime140_1.dll") { Copy-Item "$base\$f" "$app\" }
Copy-Item "$base\LICENSE.txt" "$app\PYTHON-LICENSE.txt"
Copy-Tree "$base\DLLs" "$app\DLLs"
Copy-Tree "$base\Lib" "$app\Lib" @("site-packages", "test", "tkinter", "idlelib", "turtledemo", "ensurepip", "__pycache__")
Set-Content "$app\triage1._pth" "Lib`r`nLib\site-packages`r`nDLLs`r`n.`r`nimport site" -Encoding ascii
& $py -m pip install -q --no-compile --target "$app\Lib\site-packages" -r requirements-prod.txt -c packaging\constraints.txt
Assert-Ok "Installing the libraries"

Copy-Item $pyd.FullName "$app\"
Copy-Item packaging\windows\launch.py "$app\"
Copy-Tree static "$app\static" @("v1")
Copy-Tree skills "$app\skills"
Copy-Item db\schema.sql "$app\db\"

$pgRoot = Get-ChildItem "C:\Program Files\PostgreSQL" -Directory | Where-Object { $_.Name -like "17*" } | Select-Object -First 1
if (-not $pgRoot) { throw "PostgreSQL 17 is needed on the build machine" }
foreach ($d in "bin", "lib", "share") { Copy-Tree (Join-Path $pgRoot.FullName $d) "$app\pgsql\$d" }
Remove-Item "$app\pgsql\bin\pgAdmin*", "$app\pgsql\bin\stackbuilder*", "$app\pgsql\share\doc" -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item (Join-Path $pgRoot.FullName "server_license.txt") "$app\POSTGRESQL-LICENSE.txt"
foreach ($dll in "vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll") { Copy-Item "$env:SystemRoot\System32\$dll" "$app\pgsql\bin\" -Force }

$winsw = Join-Path $Out "WinSW-x64.exe"
if (-not (Test-Path $winsw)) {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest "https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe" -OutFile $winsw -UseBasicParsing
}
Write-Host ("WinSW SHA256: " + (Get-FileHash $winsw -Algorithm SHA256).Hash)
Copy-Item $winsw "$app\service\TRIAGE1.exe"
Copy-Item packaging\windows\provision.ps1, packaging\windows\remove_services.ps1, packaging\windows\TRIAGE1.xml.template "$app\service\"

Step "Speeding up first start"
& "$app\triage1.exe" -W ignore -m compileall -q -j 0 "$app\Lib" | Out-Null
Assert-Ok "Precompiling the libraries"

Step "Checking what is in the build"
if (Get-ChildItem "$app\app" -ErrorAction SilentlyContinue) { throw "Readable app folder found in the build" }
foreach ($secret in "licence_tool*", "*licence-private*", "*.pem.bak", ".env") {
    $hit = Get-ChildItem $app -Recurse -File -Filter $secret | Where-Object { $_.FullName -notlike "*\Lib\site-packages\*" -and $_.FullName -notlike "*\pgsql\*" }
    if ($hit) { throw "Vendor or secret file in the build: $($hit[0].Name)" }
}
Write-Host "ok   no readable app source and no vendor files"

if (-not $SkipTest) {
    Step "Testing the build on a throw-away database"
    $smoke = Join-Path $Out "smoke"
    $port = 18001; $pgPort = 55432
    $pgBin = "$app\pgsql\bin"
    $ErrorActionPreference = "Continue"       # these tools write progress to stderr; every step's exit code is checked instead
    if (Test-Path "$smoke\pg\postmaster.pid") { & "$pgBin\pg_ctl.exe" -D "$smoke\pg" -m immediate stop | Out-Null }
    Remove-Item $smoke -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force "$smoke\home" | Out-Null
    & "$pgBin\initdb.exe" -D "$smoke\pg" -U postgres -A trust -E UTF8 --no-locale | Out-Null
    Assert-Ok "initdb"
    # Not piped: the server inherits the pipe from pg_ctl and keeps it open, so a pipeline would wait until the database stops.
    $pgStart = Start-Process "$pgBin\pg_ctl.exe" -ArgumentList "-D", "`"$smoke\pg`"", "-o", "`"-p $pgPort -c listen_addresses=127.0.0.1`"", "-l", "`"$smoke\pg.log`"", "-w", "start" -PassThru -WindowStyle Hidden
    $null = $pgStart.Handle
    $pgStart.WaitForExit()
    if ($pgStart.ExitCode) { throw "Starting the test database failed (exit $($pgStart.ExitCode))" }
    $proc = $null
    try {
        "create database triage;" | & "$pgBin\psql.exe" -h 127.0.0.1 -p $pgPort -U postgres -d postgres -q -v ON_ERROR_STOP=1 -f -
        Assert-Ok "Creating the test database"
        $env:TRIAGE_HOME = "$smoke\home"
        $env:DATABASE_URL = "postgresql://postgres@127.0.0.1:$pgPort/triage"
        $env:TRIAGE_PORT = "$port"
        $env:TRIAGE_SECRET_KEY = [Convert]::ToBase64String((1..32 | ForEach-Object { Get-Random -Maximum 256 } | ForEach-Object { [byte]$_ })).Replace("+", "-").Replace("/", "_")
        $proc = Start-Process "$app\triage1.exe" -ArgumentList "`"$app\launch.py`"", "serve" -WorkingDirectory $app -PassThru -WindowStyle Hidden `
            -RedirectStandardOutput "$smoke\out.log" -RedirectStandardError "$smoke\err.log"
        $up = $false
        foreach ($i in 1..120) {
            if ($proc.HasExited) { break }
            try { if ((Invoke-WebRequest "http://127.0.0.1:$port/api/health" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200) { $up = $true; break } } catch {}
            Start-Sleep 1
        }
        if (-not $up) { Get-Content "$smoke\err.log" -Tail 30 | Write-Host; throw "The compiled app did not start." }
        Write-Host "ok   the compiled app starts and answers its health check"
        foreach ($c in @(@("/setup", 200), @("/static/css/style.css", 200), @("/api/admin/licence", 503))) {   # 503: a new install refuses API calls until setup is finished
            $code = try { (Invoke-WebRequest "http://127.0.0.1:$port$($c[0])" -UseBasicParsing -TimeoutSec 30).StatusCode } catch { [int]$_.Exception.Response.StatusCode }
            if ($code -ne $c[1]) { throw "$($c[0]) answered $code, expected $($c[1])" }
            Write-Host "ok   $($c[0]) answers $code"
        }
        $bad = Select-String -Path "$smoke\out.log", "$smoke\err.log" -Pattern "not loaded|Traceback|ModuleNotFound|ImportError"
        if ($bad) { $bad | Select-Object -First 10 | ForEach-Object { Write-Host $_.Line }; throw "Part of the app failed to load." }
        Write-Host "ok   no part of the app failed to load"
        # Single quotes inside: Windows PowerShell 5.1 drops double quotes from arguments passed to a program.
        $check = "from app.ports import registry; from app.agent import skills; from app import licence; registry.discover(); print(sorted(m.provider for m in registry.available('ticketing'))); print(len(list(skills.SKILLS_DIR.glob('*/SKILL.md')))); print(sorted(licence.PLANS))"
        Push-Location $app
        $loaded = & "$app\triage1.exe" -c $check       # not "$out": PowerShell names ignore case, and $Out is the build folder
        Pop-Location
        Assert-Ok "Loading the adapters"
        $loaded | ForEach-Object { Write-Host "     $_" }
        $text = ($loaded -join " ")
        if ($text -notmatch "bmc.*freshservice.*jira.*manageengine.*servicenow") { throw "Not all ticketing adapters were found." }
        if ($text -notmatch "\]\s+([1-9]\d*)\s+\[") { throw "No playbooks were found." }
        if ($text -notmatch "Commander.*Starter.*Team") { throw "The plans were not found." }
        Write-Host "ok   all ticketing adapters, playbooks and plans load from the compiled build"

        # A real backup and restore, with the compiled tools and a real database.
        $launch = "$app\launch.py"
        $psql = { param($sql) $sql | & "$pgBin\psql.exe" -h 127.0.0.1 -p $pgPort -U postgres -d triage -q -t -A -v ON_ERROR_STOP=1 -f - }
        @{ DATABASE_URL = $env:DATABASE_URL; TRIAGE_SECRET_KEY = $env:TRIAGE_SECRET_KEY } | ConvertTo-Json | & "$app\triage1.exe" $launch bootstrap-merge | Out-Null
        Assert-Ok "Saving the test key file"
        $env:TRIAGE_BACKUP_KEY = "smoke-test-recovery-key"
        & $psql "create table restore_marker(v int); insert into restore_marker values (42);" | Out-Null
        Assert-Ok "Creating test data"
        & "$app\triage1.exe" $launch backup-now --reason smoke
        Assert-Ok "backup-now"
        $enc = Get-ChildItem "$smoke\home\backups" -Filter "triage-*.dump.enc" | Select-Object -First 1
        if (-not $enc -or -not (Get-ChildItem "$smoke\home\backups" -Filter "bootstrap-*.json.enc")) { throw "Encrypted backup files were not written." }
        if ((Get-ChildItem "$smoke\home\backups" -Filter "*.dump") -or (Get-ChildItem "$smoke\home\backups" -Filter "*.json")) { throw "An unencrypted backup file was left behind." }
        Write-Host "ok   backup-now writes encrypted files only"
        Stop-Process -Id $proc.Id -Force; $proc.WaitForExit(); Start-Sleep 2
        & $psql "drop table restore_marker;" | Out-Null
        $env:TRIAGE_BACKUP_KEY = "the-wrong-key"
        & "$app\triage1.exe" $launch restore $enc.FullName --yes
        if ($LASTEXITCODE -ne 1) { throw "A wrong recovery key was not refused (exit $LASTEXITCODE)." }
        Write-Host "ok   a wrong recovery key is refused"
        $env:TRIAGE_BACKUP_KEY = "smoke-test-recovery-key"
        & "$app\triage1.exe" $launch restore $enc.FullName --yes
        Assert-Ok "restore"
        $v = (& $psql "select v from restore_marker;" | Out-String).Trim()
        if ($v -ne "42") { throw "The restored data is wrong (got '$v')." }
        if (Get-ChildItem "$smoke\home" -Directory -Filter "restore-*") { throw "Decrypted restore files were left behind." }
        Write-Host "ok   restore puts the data back and leaves no decrypted files"
    } finally {
        if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }
        foreach ($n in "TRIAGE_HOME", "DATABASE_URL", "TRIAGE_PORT", "TRIAGE_SECRET_KEY", "TRIAGE_BACKUP_KEY") { Remove-Item "Env:\$n" -ErrorAction SilentlyContinue }
        & "$pgBin\pg_ctl.exe" -D "$smoke\pg" -m fast -w stop | Out-Null
        $global:LASTEXITCODE = 0
        $ErrorActionPreference = "Stop"
    }
}

Step "Building the installer"
$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 is not installed" }
& $iscc /Q "/DAppVersion=$Version" "/DDist=$app" "/O$Out\installer" packaging\windows\triage1.iss
Assert-Ok "Inno Setup"
if (-not (Test-Path "$Out\installer\TRIAGE-1-Setup-$Version.exe")) { throw "The installer was not written to $Out\installer" }
Get-ChildItem "$Out\installer\*.exe" | ForEach-Object { "{0}  {1:N0} MB  sha256 {2}" -f $_.FullName, ($_.Length / 1MB), (Get-FileHash $_.FullName -Algorithm SHA256).Hash }
Write-Host "`n=== DONE"
