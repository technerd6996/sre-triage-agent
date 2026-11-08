# Active Directory account locked out

## When to use
A user reports "your account has been locked out", repeated sign-in failures, or cannot log in although they know their password.

## Likely causes
- The user mistyped the password several times (the lockout threshold was reached).
- An old password is saved somewhere and retries automatically: a phone mail app, a mapped drive, a scheduled task or a service running under the user's account.
- A password was changed recently and one device still uses the old one.

## Checks
1. Confirm the account is locked and enabled, not disabled or expired.
2. Ask the user which devices and apps they use (phone, laptop, remote desktop sessions). A lockout that returns soon after an unlock points to a device with an old saved password.

## Fix
1. Unlock the account. In the TRIAGE-1 chat: "unlock <username>". Use the sign-in name (sAMAccountName), for example jsmith.
2. Ask the user to sign in again.
3. If the account locks again within an hour, ask the user to sign out of other devices and remove saved passwords for the account. Check scheduled tasks and services that run under it.
4. A disabled account or an expired password is not a lockout: do not unlock it. Hand it to the identity team.

## Escalate when
- The account locks again after the user removed old saved passwords.
- There are lockouts for many users at once. That can be an attack: tell the security team at once.
