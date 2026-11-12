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

**Within TRIAGE-1**
- Your PostgreSQL database, on your server.
- Your encrypted backups and backup copies.
- Each user's own browser, for approver chat conversations.

**To systems your organisation connects**
- Your ticketing tool, for work notes and incidents.
- Your mail service, for approval and result emails.
- Your directory, for sign-in and account checks.
- Your credential store.

If any of these is a cloud service, data goes to that provider under your organisation's agreement with it, and international transfer rules may apply.

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

Requests go to **your organisation**, as the controller. Your administrators handle them using the procedures below. Some records can lawfully be kept despite an erasure request, for example where your organisation needs an audit trail to meet legal or security obligations. In those cases, replacing the person's identity with a pseudonym in the audit trail is usually the right balance.

## 6. Handling an access request

A database administrator can list everything that names the person. Take care to give the person only their own data, not other people's.

```sql
\set who '''jane.doe@example.com'''
select * from public.users          where user_id = :who;
select ts, action, ticket, detail from public.audit where actor = :who order by ts;
select * from public.runs           where requester = :who;
select * from public.approvals      where decided_by = :who;
select * from public.owner_requests where requester = :who or approved_by = :who or owners_json::text ilike '%' || :who || '%';
select * from public.notices        where approver = :who or recipients_json::text ilike '%' || :who || '%';
```

Chat conversations are only in the person's own browser, and they can export or delete them there. Ticket records are in your ticketing tool.

## 7. Handling an erasure request

1. **Verify** the request and decide what your organisation must keep. Audit records are often kept, but under a pseudonym.
2. **Stop access.** In **Admin > Users**, set the account to *Disabled* and revoke its sessions.
3. **Replace or remove** the person's details in the database. Take a backup and test on a copy first. The statements below replace the person's identity with a fixed pseudonym, which keeps the audit trail intact.

```sql
begin;
\set who '''jane.doe@example.com'''
\set alias '''erased-user-0001'''
delete from public.sessions       where user_id = :who;
update public.audit          set actor = :alias        where actor = :who;
update public.runs           set requester = :alias    where requester = :who;
update public.approvals      set decided_by = :alias   where decided_by = :who;
update public.owner_requests set requester = :alias    where requester = :who;
update public.owner_requests set approved_by = :alias, approval_text = '[removed on request]' where approved_by = :who;
update public.notices        set approver = :alias     where approver = :who;
delete from public.users          where user_id = :who;
commit;
```

4. **Free-text fields** such as audit details, run steps, notification bodies and agent working memory can mention the person by name. Search for the name and edit or delete those rows as your decision in step 1 requires.
5. **Backups:** older backups still contain the data until they expire under your retention policy. Record that the request has been applied in production and that backups will age out by a known date. If a backup is ever restored, re-apply the erasure.
6. **Outside TRIAGE-1:** carry out the request in your ticketing tool, mail system and directory as well, under their own procedures.
7. **The person's browser:** chat history is cleared when the person deletes their chats or clears their browser data.

## 8. Changes and contact

This notice describes TRIAGE-1 version 1.0.0 and is updated with new versions.

- Questions about how your organisation uses TRIAGE-1 go to your organisation's data protection contact.
- Questions about the software go to **[Vendor legal name], [privacy contact email]**.
