
# Simulate-ADLockout.ps1
# Run on an authorized, domain-joined lab client.
# Replace the placeholder with a dedicated lab account.

param(
    [Parameter(Mandatory = $true)]
    [string]$TestSamAccountName
)

$ErrorActionPreference = "Stop"

try {
    Import-Module ActiveDirectory

    # Verify domain connectivity
    $domain = Get-ADDomain
    Write-Host "Domain: $($domain.DNSRoot)"
    Write-Host "PDC Emulator: $($domain.PDCEmulator)"

    # Verify the exact target account
    $user = Get-ADUser -Identity $TestSamAccountName `
        -Properties LockedOut, Enabled, PasswordLastSet

    if (-not $user.Enabled) {
        throw "Target account is disabled. Aborting."
    }

    Write-Host "`nTarget account: $($user.SamAccountName)"
    Write-Host "Enabled: $($user.Enabled)"
    Write-Host "Currently locked: $($user.LockedOut)"

    if ($user.LockedOut) {
        throw "Target is already locked. Unlock it before testing."
    }

    # Display the domain's default lockout policy
    $policy = Get-ADDefaultDomainPasswordPolicy

    Write-Host "`nDefault domain lockout policy:"
    $policy | Select-Object LockoutThreshold,
        LockoutDuration,
        LockoutObservationWindow | Format-List

    if ($policy.LockoutThreshold -eq 0) {
        Write-Warning "Default domain policy has no lockout threshold."
        Write-Warning "Check whether a fine-grained password policy applies."
    }

    Write-Host "`nPre-check complete."
    Write-Host "Next: use the lab client to trigger a lockout"
    Write-Host "for this test account, then validate Event ID 4740."
}
catch {
    Write-Error "Pre-check failed: $($_.Exception.Message)"
    exit 1
}
