---
name: ad-account-locked
description: Active Directory user account locked out, cannot login, authentication or sign-in failure.
systems: [windows]
risk: medium
keywords: [locked, lockout, lock out, active directory, ad account, unlock, account locked, login, sign in, password expired, sAMAccountName]
allowed_tools: [check_reachability, lookup_ad_user, unlock_ad_account, add_work_note, resolve_incident, escalate_incident]
---

# Active Directory account locked

## Diagnose (read-only)
1. Extract the affected username (sAMAccountName) from the ticket description. If no username is mentioned in the ticket, call add_work_note and escalate_incident stating no username was provided. Stop immediately.
2. check_reachability with target "windows". If reachable is false, call add_work_note and escalate_incident saying the Active Directory server is unreachable. Stop immediately.
3. lookup_ad_user with the username. Record Enabled, LockedOut, and PasswordExpired in add_work_note.
4. If the user is not found or lookup_ad_user returns an error, call add_work_note and escalate_incident. Stop immediately.
5. If Enabled is false, call add_work_note and escalate_incident saying the account is disabled and requires L2 review. Stop immediately.
6. If PasswordExpired is true and LockedOut is false, call add_work_note and escalate_incident saying the password is expired. Stop immediately.
7. If LockedOut is false and Enabled is true, call add_work_note "Account is enabled and not locked out" and call resolve_incident. Stop immediately.

## Fix (needs approval)
1. If LockedOut is true, call unlock_ad_account with the username. The engineer must approve this call.
2. If the engineer rejects the approval, call add_work_note and escalate_incident. Do not retry.

## Verify
1. Call lookup_ad_user again with the username to confirm LockedOut is now false.
2. If LockedOut is false, call add_work_note and resolve_incident.
3. If LockedOut is still true, call add_work_note and escalate_incident.

## Close notes template
AD user <username>: Enabled=<true/false>, LockedOut before=<true/false>. Unlocked account after approval. LockedOut after=<false>.