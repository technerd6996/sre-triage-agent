# TRIAGE-1 Data Retention Notice

**TRIAGE-1 version 1.0.0**

This notice lists what TRIAGE-1 stores and for how long. It helps your organisation set its retention schedule and answer audit and data protection questions.

TRIAGE-1 runs on your own infrastructure:
- All data below is stored in your PostgreSQL database, on your server or in your backups, except where stated otherwise.
- None of it is sent to the vendor.
- Your organisation decides how long to keep it.

## 1. Where data lives

```
Engineer's browser ---------> chat conversations (kept in that browser only)
        |
        v
TRIAGE-1 server ------------> PostgreSQL database (accounts, audit, runs, approvals, settings)
        |                             |
        |                             v
        |                     Daily encrypted backup ---> second disk or network share
        v
Your ticketing tool, mail service, directory ---> kept under those systems' own retention rules
```

## 2. What is stored and for how long

| Data | Contents | Kept for | How to change it |
|---|---|---|---|
| **User accounts** | Email or user ID, name, role, status, ticketing-tool user reference, password hash (local accounts only), created and last sign-in times | Until the account is removed. Disabling an account stops access but keeps the record | Admin > Users. See the Privacy and Erasure Notice |
| **Sign-in sessions** | A coded form of the session token, the user and the expiry time | 7 days, then deleted automatically | Sign out, or revoke sessions in Admin > Users |
| **Audit log** | Time, actor, action, ticket and details of every check, change, approval, setting change and sign-in event | Until you delete it. This is your accountability record | Administrator procedure |
| **Triage runs and steps** | Ticket number, server, playbook, steps and findings, outcome, requester, token counts | Until you delete it | Administrator procedure |
| **Agent working memory** | The working state of each run, which can include ticket text | Until you delete it | Administrator procedure |
| **Approvals** | Requested action, risk level, who decided, decision, comment | Until you delete it | Administrator procedure |
| **Owner approval requests** | Ticket, requester, action, server, owner addresses, the pasted approval reply, who approved and when | Until you delete it | Administrator procedure |
| **Notifications** | Recipients and text of approval and result emails | Until you delete it | Administrator procedure |
| **Verifications** | Results of the checks run after a fix | Until you delete it | Administrator procedure |
| **Knowledge search records** | Question text (shortened), a pseudonymous user identifier, articles found, timings | Until you delete it | Administrator procedure |
| **Model usage records** | Type of call, token counts, timings. No prompt text | Until you delete it | Administrator procedure |
| **Application telemetry** | Request timings, errors and metrics for TRIAGE-1 itself. No chat text | **72 hours** by default, then deleted automatically | Admin > Telemetry, from 1 to 720 hours |
| **Settings and stored credentials** | Configuration, with credentials and other secrets encrypted | Until changed or removed | Admin panel |
| **Approver chat conversations** | The conversation text | In each user's **own browser** only, until that user deletes the chat or clears browser data. Not on the server | The user's browser |
| **Backups** | Encrypted copies of the whole database and the installation's key file | According to **your retention policy**. The newest backup is always kept | Admin > Behaviour > Backups |
| **Backup copies off the machine** | The same encrypted files on your second disk or share | Removed by the same policy | As above |

## 3. How each kind of data leaves the system

```
Sign-in session   -> expires after 7 days     -> deleted automatically
Telemetry         -> older than 72 hours      -> deleted automatically
Backup            -> outside your retention   -> deleted automatically (the newest is always kept)
Audit, runs,      -> reach your chosen period -> your administrator deletes them
approvals, emails                                using the Administrator Procedures guide
Chat conversation -> user deletes the chat    -> gone from that browser
```

## 4. How backup retention works

You choose three numbers in Admin > Behaviour > Backups:

```
Keep every backup  -> for N days    (for example 14)
then keep one a week  -> for N weeks   (for example 8)
then keep one a month -> for N months  (for example 12)
Anything older        -> deleted, on the server and on the backup copy
```

## 5. Deleting older operational records

This version deletes sessions, telemetry and backups automatically. Audit, run, approval, notification and search records are kept until your administrator deletes them. This lets your organisation set its own period for accountability records.

```
Decide the period (for example 365 days)
   -> take a backup
   -> your database administrator runs the "Retention clean-up" procedure
      from the TRIAGE-1 Administrator Procedures guide
   -> only finished work older than the period is removed
      (anything running or waiting for approval is never touched)
   -> repeat on a schedule, for example monthly
```

Records deleted from the database remain in older backups until those backups expire under your backup retention policy.

## 6. Data held outside TRIAGE-1

TRIAGE-1 writes work notes, incident updates and new incidents into **your ticketing tool**, and sends email through **your mail service**. Records there are kept under that system's own retention settings, not TRIAGE-1's.

## 7. Recommended starting points

| Record | Common choice |
|---|---|
| Audit log | 1 to 7 years, following your change-management and audit policy |
| Runs, approvals, owner requests, notifications | Match your incident-record retention |
| Knowledge search records | 90 days |
| Telemetry | 72 hours (default) |
| Backups | 14 days, 8 weeks, 12 months |

These are suggestions only. Your organisation sets the periods that match its legal and contractual obligations.
