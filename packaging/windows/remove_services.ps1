# Run by the uninstaller. Removes the services and keeps all data in C:\ProgramData\triage.
param([Parameter(Mandatory)][string]$AppDir)
$ErrorActionPreference = "Continue"
$svcDir = Join-Path $AppDir "service"
if (Get-Service TRIAGE1 -ErrorAction SilentlyContinue) {
    Stop-Service TRIAGE1 -Force -ErrorAction SilentlyContinue
    & "$svcDir\TRIAGE1.exe" uninstall
}
if (Get-Service triage1-postgres -ErrorAction SilentlyContinue) {
    Stop-Service triage1-postgres -Force -ErrorAction SilentlyContinue
    & (Join-Path $AppDir "pgsql\bin\pg_ctl.exe") unregister -N triage1-postgres
}
