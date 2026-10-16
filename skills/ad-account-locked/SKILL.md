---
name: ad-account-locked
description: Active Directory user account locked out, cannot login, authentication or sign-in failure.
systems: [windows]
risk: medium
keywords: [locked, lockout, lock out, active directory, ad account, unlock, account locked, login, sign in, password expired, sAMAccountName]
allowed_tools: [check_reachability, lookup_ad_user, unlock_ad_account, add_work_note, resolve_incident, escalate_incident]
---

# Active Directory account locked

## Steps
1. Find the username (sAMAccountName) in the ticket. If there is none, call add_work_note and escalate_incident. Stop.
2. check_reachability with target "windows". If reachable is false, follow its next_step. Stop.
3. lookup_ad_user with the username. Call add_work_note with Enabled, LockedOut and PasswordExpired.
4. Follow the next_step of lookup_ad_user exactly. If it says to call unlock_ad_account, call it now. The engineer must approve this call. Do not resolve the ticket before the unlock.
5. If the engineer rejects the approval, call add_work_note and escalate_incident. Do not retry.
6. After unlock_ad_account, follow its next_step.

## Close notes template
AD user <username>: Enabled=<true/false>, LockedOut before=<true/false>. Unlocked after approval. LockedOut after=<false>.