# TRIAGE-1 Administrator Procedures

**TRIAGE-1 version 1.0.0 · For database administrators**

These procedures support the Data Retention Notice and the Privacy and Erasure Notice. Run them with `psql`, or any PostgreSQL client, connected to the TRIAGE-1 database.

**Always:**

```
take a backup -> test on a copy -> run in production -> record what was done
```

## 1. Retention clean-up

This removes finished records older than the chosen period. The example uses 365 days. Work that is running or waiting for approval is never removed.

```sql
\set cutoff 'extract(epoch from now() - interval ''365 days'')'
delete from public.events        where run_id in (select id from public.runs where ended is not null and ended < :cutoff);
delete from public.runs          where ended is not null and ended < :cutoff;
delete from public.approvals     where coalesce(decided_at, requested_at) < :cutoff and decision is not null;
delete from public.owner_requests where created < :cutoff and status <> 'waiting';
delete from public.notices       where sent_at < :cutoff;
delete from public.verifications where ts < :cutoff;
delete from public.rag_events    where ts < :cutoff;
delete from public.llm_calls     where ts < :cutoff;
delete from public.audit         where ts < :cutoff;   -- only if your policy allows deleting audit records
```

Agent working memory is kept in the tables `checkpoints`, `checkpoint_writes` and `checkpoint_blobs`. Ask support for the statement that matches your version before you delete from them.

## 2. Find a person's data

This is used for access requests. Replace the email address with the person's user ID or email.

```sql
\set who '''jane.doe@example.com'''
select * from public.users          where user_id = :who;
select ts, action, ticket, detail from public.audit where actor = :who order by ts;
select * from public.runs           where requester = :who;
select * from public.approvals      where decided_by = :who;
select * from public.owner_requests where requester = :who or approved_by = :who or owners_json::text ilike '%' || :who || '%';
select * from public.notices        where approver = :who or recipients_json::text ilike '%' || :who || '%';
```

## 3. Erase a person

Disable the account in Admin > Users first. This procedure keeps the audit trail but replaces the person's identity with a pseudonym. Use a new pseudonym for each person.

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

Then search the free-text columns for the person's name: `audit.detail`, `events.body`, `notices.body` and the agent working memory. Edit or delete the matching entries as your decision requires.
