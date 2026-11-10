# Builds the Windows installer: compiled app + PostgreSQL + service wrapper -> TRIAGE-1-Setup-<version>.exe
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1 -Version 1.0.0
# Needs: the C++ tools of Visual Studio Build Tools, Inno Setup 6, PostgreSQL 17 on this machine, and $Out\venv with Nuitka.
param([string]$Version = "1.0.0", [string]$Out = "D:\triage-build", [switch]$SkipCompile)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $Out "venv\Scripts\python.exe"
$dist = Join-Path $Out "nuitka\triage1.dist"
$stage = Join-Path $Out "stage"

function Step($t) { Write-Host "`n=== $t" }

if (-not $SkipCompile) {
    Step "Python packages (tested versions)"
    & $py -m pip install -q -r requirements-prod.txt -c packaging\constraints.txt
    if ($LASTEXITCODE) { throw "pip failed" }

    Step "Compiling (this takes a long time)"
    $env:PYTHONPATH = $root
    & $py -m nuitka --standalone --msvc=latest --assume-yes-for-downloads `
        "--output-dir=$Out\nuitka" --output-filename=triage1.exe `
        --include-package=app --include-package=uvicorn --include-package=langgraph --include-package=langchain_openai `
        --include-package=psycopg --include-package=psycopg_pool --include-package=psycopg_binary `
        --nofollow-import-to=torch --nofollow-import-to=sentence_transformers --nofollow-import-to=transformers `
        --nofollow-import-to=pytest --nofollow-import-to=tests `
        --include-data-dir=static=static --include-data-dir=skills=skills `
        --include-data-files=db/schema.sql=db/schema.sql `
        "--noinclude-data-files=static/v1/*" `
        --windows-console-mode=force --product-name=TRIAGE-1 --company-name=TRIAGE-1 `
        "--product-version=$Version" "--file-version=$Version" --file-description="TRIAGE-1 server" `
        packaging\triage1.py
    if ($LASTEXITCODE) { throw "Nuitka failed" }
}
if (-not (Test-Path "$dist\triage1.exe")) { throw "Compiled program not found in $dist" }
foreach ($secret in "licence_tool", "licence-private") {
    if (Get-ChildItem $dist -Recurse -File | Where-Object { $_.Name -like "*$secret*" }) { throw "Vendor file $secret found in the build" }
}

Step "Staging"
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force "$stage\service", "$stage\pgsql" | Out-Null

$pgRoot = Get-ChildItem "C:\Program Files\PostgreSQL" -Directory | Where-Object { $_.Name -like "17*" } | Select-Object -First 1
if (-not $pgRoot) { throw "PostgreSQL 17 is needed on the build machine" }
foreach ($d in "bin", "lib", "share") { Copy-Item (Join-Path $pgRoot.FullName $d) "$stage\pgsql\$d" -Recurse }
Remove-Item "$stage\pgsql\bin\pgAdmin*", "$stage\pgsql\bin\stackbuilder*", "$stage\pgsql\share\doc" -Recurse -Force -ErrorAction SilentlyContinue
foreach ($dll in "vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll") { Copy-Item "$env:SystemRoot\System32\$dll" "$stage\pgsql\bin\" -Force }

$winsw = Join-Path $Out "WinSW-x64.exe"
if (-not (Test-Path $winsw)) {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest "https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe" -OutFile $winsw -UseBasicParsing
}
Write-Host ("WinSW SHA256: " + (Get-FileHash $winsw -Algorithm SHA256).Hash)
Copy-Item $winsw "$stage\service\TRIAGE1.exe"
Copy-Item packaging\windows\provision.ps1, packaging\windows\remove_services.ps1, packaging\windows\TRIAGE1.xml.template "$stage\service\"

Step "Building the installer"
$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 is not installed" }
& $iscc "/DAppVersion=$Version" "/DDist=$dist" "/DStage=$stage" "/O$Out\installer" packaging\windows\triage1.iss
if ($LASTEXITCODE) { throw "Inno Setup failed" }
Get-ChildItem "$Out\installer\*.exe" | ForEach-Object { "{0}  {1:N0} MB" -f $_.FullName, ($_.Length / 1MB) }
