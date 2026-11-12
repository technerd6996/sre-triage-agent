# TRIAGE-1 Privacy and Erasure Notice

**TRIAGE-1 version 1.0.0**

This notice explains:
- what personal data TRIAGE-1 processes and why;
- who is responsible for it;
- the rights of the people concerned; and
- how to carry out a request to access or erase a person's data.

## 1. Who is responsible

TRIAGE-1 is software that your organisation installs and runs on its own infrastructure.

- **Your organisation is the controller** of the personal data that TRIAGE-1 processes. It decides why and how that data is used.
- **[Vendor legal name] does not receive this data.** The software does not report to the vendor, and the licence check runs offline. The vendor sees personal data only if your organisation shares it, for example during a support session. In that case the vendor acts as your processor, under your instructions and a data processing agreement.

```
Person (engineer, server owner, ticket caller)
   -> data used by TRIAGE-1 on YOUR server
   -> your organisation decides how it is used (controller)
   -> the vendor receives nothing, unless you share data for support (then the vendor is your processor)
```

Your organisation's own privacy notice to its staff should cover the use of TRIAGE-1. This document gives you the details to include.

## 2. Whose data, what data, and why

| People | Data | Purpose | Usual legal basis |
|---|---|---|---|
| **TRIAGE-1 users** (engineers, approvers, admins, auditors) | Email or user ID, name, role, sign-in times, actions taken, approvals given, password hash (local accounts only) | Access control, carrying out and recording operational work, security and accountability | Legitimate interests in running IT operations securely; contract of employment or service |
| **Server owners and managers** (from your CMDB) | Name, email address, the approval replies they send | Asking for and recording approval of high-risk changes | Legitimate interests; compliance with change-management obligations |
| **People named in incidents** (callers, affected users, account holders) | Whatever your tickets contain, such as names, user IDs and locked-out account names | Triage and resolution of the incident | Legitimate interests in restoring IT services |
| **Directory users** | Account name and lock status, read from Active Directory when an unlock is requested | Unlocking an account on request | Legitimate interests |

TRIAGE-1 does not:
- use personal data for marketing or profiling;
- make decisions about people that have legal or similarly significant effects;
- sell or share personal data.

The language model processes ticket text to suggest checks and write findings. It runs on a model service **hosted by your organisation**, not by the vendor or a public AI provider. Any change the model suggests is applied only through fixed tools, under your risk rules and human confirmation.

## 3. Where the data goes

```
TRIAGE-1 server -> PostgreSQL database on your server
                -> encrypted backups -> your second disk or share
                -> your ticketing tool   (work notes, incidents)
                -> your mail service     (approval and result emails)
                -> your directory        (sign-in, account checks)
                -> your credential store (short-lived server passwords)
Engineer's browser -> approver chat conversations (stay in that browser)
```

If any connected system is a cloud service, data goes to that provider under your organisation's agreement with it, and international transfer rules may apply.

**Security**
- Credentials and secret settings are encrypted with a key unique to your installation.
- Backups are encrypted with AES-256-GCM using a recovery key that your organisation holds.
- Access is controlled by role, and every action is recorded in the audit log.

## 4. How long data is kept

See the **TRIAGE-1 Data Retention Notice**.

- Sign-in sessions expire after 7 days.
- Telemetry is deleted after 72 hours by default.
- Backups follow your retention policy.
- Operational and audit records are kept until your organisation deletes them under its own schedule.

## 5. People's rights

Depending on the law that applies, people may have the right to:
- access their data;
- correct it;
- erase it;
- restrict or object to its processing;
- receive it in a portable form; and
- complain to a data protection authority.

Examples of such laws include the EU and UK GDPR and the Nigeria Data Protection Act 2023.

Requests go to **your organisation**, as the controller. Some records can lawfully be kept despite an erasure request, for example where your organisation needs an audit trail to meet legal or security obligations. In those cases, replacing the person's identity with a pseudonym in the audit trail is usually the right balance.

## 6. Handling an access request

```
Request received by your data protection contact
   -> confirm the person's identity
   -> database administrator runs the "Find a person's data" procedure
      (TRIAGE-1 Administrator Procedures guide), searching by the person's email or user ID
   -> results cover: account, audit entries, runs they asked for,
      approvals they gave, owner requests, emails sent to or about them
   -> remove other people's details from the results
   -> add their chat history (the person exports it from their own browser)
      and their ticket records (from your ticketing tool)
   -> reply to the person within the legal deadline
```

## 7. Handling an erasure request

```
Request received
   -> 1. Verify and decide
         Confirm identity. Decide what must be kept
         (audit records are usually kept, under a pseudonym).
   -> 2. Stop access
         Admin > Users -> set the account to Disabled -> revoke its sessions.
   -> 3. Replace the identity
         Take a backup -> the database administrator runs the "Erase a person" procedure
         (TRIAGE-1 Administrator Procedures guide)
         -> the person's name is replaced by a pseudonym such as "erased-user-0001"
            in the audit log, runs, approvals, owner requests and notifications
         -> their sessions and their account are deleted
         -> any approval text they wrote is replaced with "[removed on request]"
   -> 4. Check free text
         Search audit details, run steps, email text and agent working memory
         for the person's name -> edit or delete those entries as decided in step 1.
   -> 5. Backups
         Older backups keep the data until they expire under your retention policy
         -> record the date they will have aged out
         -> if a backup is ever restored, repeat step 3.
   -> 6. Outside TRIAGE-1
         Carry out the request in your ticketing tool, mail system and directory too.
   -> 7. Browser
         The person deletes their chats, or clears their browser data.
   -> Confirm completion to the person.
```

## 8. Changes and contact

This notice describes TRIAGE-1 version 1.0.0 and is updated with new versions.

- Questions about how your organisation uses TRIAGE-1 go to your organisation's data protection contact.
- Questions about the software go to **[Vendor legal name], [privacy contact email]**.
