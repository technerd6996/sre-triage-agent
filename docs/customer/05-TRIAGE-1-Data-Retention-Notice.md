# TRIAGE-1 Data Retention Notice

**TRIAGE-1 version 1.0.0**

This notice lists what TRIAGE-1 stores and for how long. It helps your organisation set its retention schedule and answer audit and data protection questions.

TRIAGE-1 runs on your own infrastructure:
- All data below is stored in your PostgreSQL database, on your server or in your backups, except where stated otherwise.
- None of it is sent to the vendor.
- Your organisation decides how long to keep it.

## 1. What is stored and for how long

| Data | Contents | Kept for | How to change it |
|---|---|---|---|
| **User accounts** | Email or user ID, name, role, status, ticketing-tool user reference, password hash (local accounts only), created and last sign-in times | Until the account is removed. Disabling an account stops access but keeps the record | Admin > Users. See the Privacy and Erasure Notice |
| **Sign-in sessions** | A hash of the session token, the user and the expiry time | 7 days. Expired sessions are deleted automatically | Sign out, or revoke sessions in Admin > Users |
| **Audit log** | Time, actor, action, ticket and details of every check, change, approval, setting change and sign-in event | Until you delete it. The audit log is your accountability record | Database procedure (see Section 3) |
| **Triage runs and steps** | Ticket number, server, playbook, the steps and findings of each run, outcome, requester, token counts | Until you delete it | Database procedure |
| **Agent working memory** | The working state of each run, which can include ticket text | Until you delete it | Database procedure |
| **Approvals** | Requested action, risk level, who decided, decision, comment | Until you delete it | Database procedure |
| **Owner approval requests** | Ticket, requester, action, server, owner addresses, the pasted approval reply, who approved and when | Until you delete it | Database procedure |
| **Notifications** | Recipients, message ID and body of approval and result emails | Until you delete it | Database procedure |
| **Verifications** | Results of the checks run after a fix, reopen count | Until you delete it | Database procedure |
| **Knowledge search records** | Question text (shortened), a pseudonymous user identifier, retrieved articles, timings | Until you delete it | Database procedure |
| **Model usage records** | Type of call, token counts, timings. No prompt text | Until you delete it | Database procedure |
| **Application telemetry** | Request timings, errors and metrics for TRIAGE-1 itself. No chat text | **72 hours** by default, deleted automatically | Admin > Telemetry, from 1 to 720 hours |
| **Settings and stored credentials** | Configuration, with credentials and other secrets encrypted | Until changed or removed | Admin panel |
| **Approver chat conversations** | The conversation text | In each user's **own browser** only, until that user deletes the chat or clears browser data. Not on the server | The user's browser |
| **Backups** | Encrypted copies of the whole database and the installation's key file | According to **your retention policy**: every backup for N days, one a week for N weeks, one a month for N months. The newest is always kept | Admin > Behaviour > Backups |
| **Backup copies off the machine** | The same encrypted files on your second disk or share | Pruned by the same policy | As above |

## 2. Data held outside TRIAGE-1

TRIAGE-1 writes work notes, incident updates and new incidents into **your ticketing tool**, and sends email through **your mail service**. Records there are kept under that system's own retention settings, not TRIAGE-1's.

## 3. Deleting older operational records

This version deletes sessions, telemetry and backups automatically. Audit, run, approval, notification and search records are kept until your administrator deletes them, so that your organisation can set its own period for accountability records.

To apply a retention period, a database administrator can run the statements below on a schedule. Take a backup and test on a copy first. The example uses a period of 365 days.

```sql
-- seconds since 1970 for 365 days ago
\set cutoff 'extract(epoch from now() - interval ''365 days'')'
delete from public.events        where run_id in (select id from public.runs where ended is not null and ended < :cutoff);
delete from public.runs          where ended is not null and ended < :cutoff;   -- finished runs only
delete from public.approvals     where coalesce(decided_at, requested_at) < :cutoff and decision is not null;
delete from public.owner_requests where created < :cutoff and status <> 'waiting';
delete from public.notices       where sent_at < :cutoff;
delete from public.verifications where ts < :cutoff;
delete from public.rag_events    where ts < :cutoff;
delete from public.llm_calls     where ts < :cutoff;
delete from public.audit         where ts < :cutoff;   -- only if your policy allows deleting audit records
```

Agent working memory is kept in the checkpoint tables created by the agent engine (`checkpoints`, `checkpoint_writes`, `checkpoint_blobs`). It is linked to runs by thread ID. Ask support for the statement that matches your version before you delete it.

Records deleted from the database stay in older backups until those backups expire under your backup retention policy.

## 4. Recommended starting points

| Record | Common choice |
|---|---|
| Audit log | 1 to 7 years, following your change-management and audit policy |
| Runs, approvals, owner requests, notifications | Match your incident-record retention |
| Knowledge search records | 90 days |
| Telemetry | 72 hours (default) |
| Backups | 14 days, 8 weeks, 12 months |

These are suggestions only. Your organisation sets the periods that match its legal and contractual obligations.
