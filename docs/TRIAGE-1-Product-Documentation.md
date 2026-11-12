# TRIAGE-1 Product Documentation

An L1 SRE assistant for ServiceNow incidents. Document date 2026-11-10. Describes the build that passes 542 automated tests.

## Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Installation and first run](#3-installation-and-first-run)
4. [Roles and sign-in](#4-roles-and-sign-in)
5. [Chat for users](#5-chat-for-users)
6. [Approver chat](#6-approver-chat)
7. [Agent console and automatic mode](#7-agent-console-and-automatic-mode)
8. [Knowledge and runbooks](#8-knowledge-and-runbooks)
9. [Admin panel and settings](#9-admin-panel-and-settings)
10. [Safety, audit and security](#10-safety-audit-and-security)
11. [Operations and troubleshooting](#11-operations-and-troubleshooting)
12. [Known limits and roadmap](#12-known-limits-and-roadmap)

Reading the flows: `->` means "goes to", `|` and `v` show the next step down, and text between dashes (`--message-->`) says what travels.

---

## 1. Overview

TRIAGE-1 helps a support team handle infrastructure incidents while ServiceNow stays the system of record. It does three jobs:

- **Answers questions.** A chat page where anyone signed in can paste a log or ask an SRE question. Answers come from runbooks in a ServiceNow knowledge base and cite the article.
- **Works tickets automatically.** An agent picks up new incidents from an L1 queue, runs read-only checks on the affected server, and asks a human to approve any change before it makes it.
- **Acts for an engineer.** In the approver chat an engineer can check CPU, memory, disk and processes, and ask for a small set of fixes (restart a service, clear temp files, clean logs, unlock an account). Each fix needs the engineer's Confirm, is recorded on a ServiceNow ticket they own, and is audited.

Everything runs on your own machine: the app, its PostgreSQL database and the model endpoint you point it at. ServiceNow, Active Directory and the servers being managed are the only systems it talks to.

> **What it will not do.** It never changes a server without a recorded approval or Confirm. It never runs kill-process, or anything rated high or critical risk, from the chat. It does not create or publish knowledge articles on its own, and it does not keep a copy of your runbooks.

---

## 2. Architecture

One FastAPI process serves the web pages and the API. State lives in a local PostgreSQL database. Everything else is reached through adapters, so a system can be swapped without touching the code that uses it (ticketing, hosts, identity, credentials, notifications and context are each a port with one adapter today).

```
                                               |--> ServiceNow          tickets, knowledge base, CMDB
Browsers --HTTP--> TRIAGE-1 app ---------------|--> Model endpoint      prompts, through the model gate
 - chat (users)    (FastAPI, one process,      |--> Active Directory    LDAPS: sign-in and groups
 - approver chat    127.0.0.1:8000)            |--> Target servers      WinRM / SSH (Windows, Linux)
   and console          |                      |--> Credential store    one secret per host, checked out per use
 - admin panel          |
                        v
                  PostgreSQL (local): runs, audit, users, settings
```

The app is the only thing that talks to the outside systems. Browsers never reach ServiceNow, the model, the servers or the credential store directly.

### What is stored where

| Place | What it holds |
|---|---|
| PostgreSQL 17 (local, localhost only) | Users and sessions, runs and their events, approvals, the audit log, the job queue, settings (secrets are encrypted), model call statistics, chat statistics, and the app's own telemetry (traces, metrics, logs; kept 72 hours by default). Agent checkpoints use the same database. |
| `C:\ProgramData\triage\bootstrap.json` | The two root secrets: `DATABASE_URL` and `TRIAGE_SECRET_KEY`. Created automatically, readable only by its owner and Administrators. There is no `.env` file. |
| ServiceNow | Incidents, the runbook knowledge base, the playbook knowledge base, CMDB servers and relationships, users. |
| Project files | `runbooks/*.md` (source for the runbook articles), `skills/*/SKILL.md` (agent playbooks, can also come from ServiceNow), `db/schema.sql` (applied by the start script). |

Two design rules run through the whole product. **Everything tunable is a setting** in the admin panel, with the old hard-coded value as its default. **Settings apply at once**; only new code needs a restart.

---

## 3. Installation and first run

### Prerequisites

- Windows Server with PowerShell, and Python 3.13 (a `.venv` folder is used).
- An elevated PowerShell for the very first start, because it installs PostgreSQL 17 as a Windows service.
- A reachable model endpoint that speaks the OpenAI chat format, a ServiceNow instance with an integration user, and (optional) Active Directory over LDAPS.

### Start it

```
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

The script starts local PostgreSQL if needed, applies `db/schema.sql`, then serves on `127.0.0.1:8000`. On a brand-new database the app shows a **setup page** (`/setup`) where you enter the ServiceNow, model, identity and notification details. Each section has a Test button, and any section can be filled in later from the admin panel. After setup, `/setup` is closed.

### First sign-in

The setup page creates the first super admin account, `superadmin@triage.local`, with a password **you choose on that page** (typed twice, checked against the minimum length). There is no shipped default password. Installs made before this change may still have the old default; the login page warns while it is unchanged, and that old password is refused as a new password.

> **Back up `bootstrap.json`.** Without `TRIAGE_SECRET_KEY` the saved credentials in the database cannot be decrypted.

### Resetting and copying

- `scripts\reset_local.ps1` empties the database so `/setup` shows again. It asks you to type YES and keeps the PostgreSQL install.
- A copy of the project on the same machine shares that machine's database and `bootstrap.json`, so it is not a fresh install. When copying changes, copy whole `app\`, `static\` and `tests\` folders.
- Keep `bcrypt==4.0.1` with `passlib==1.7.4`. A newer bcrypt breaks sign-in with a 401 error.

---

## 4. Roles and sign-in

Access is deny-by-default: every route has an explicit rule, and a route without one is refused. A person can hold several roles; their permissions add up.

| Role | Can do | Cannot do |
|---|---|---|
| User | Use the chat page, raise a ticket from chat. | Anything on the console, the approver chat or the admin pages. |
| Approver | Chat, agent console (view and act), approver chat, open the admin page, monitoring. | Change settings, manage users. |
| Auditor | Open the admin page, monitoring, audit log (read only). | Chat, console, settings. |
| Admin | Admin page, monitoring, audit log, change any setting (including safety settings). | Chat, console, the Users page. |
| Super admin | Everything, including users and the AD group mapping. | |

### Active Directory

- Sign-in is over LDAPS. TRIAGE-1 first tries `user@domain`; if that fails it retries as `NETBIOS\username`, which covers accounts without a UserPrincipalName.
- The person is looked up across the whole domain, but only groups under the configured **Search Base** grant a role.
- A super admin maps AD groups to roles on the Users page. A person in no mapped group is told so, and cannot enter.
- When a bind fails, the log records the directory's own reason (for example "wrong password" or "account locked out").

---

## 5. Chat for users

The chat page answers SRE questions and reads pasted logs. It is deliberately narrow: off-topic messages get a fixed reply that points back to what it can do.

### How a runbook answer is built

```
Question (+ short history for follow-ups)
  | text query
  v
Keywords (error codes and names first, filler dropped)
  |
  v
ServiceNow search: SRE Runbooks KB, published articles only
  | 10 articles
  v
Model rerank: keep the best 3
  | best 3, trimmed to the matching passages
  v
Prompt = articles  +  "Topology:" lines  <--  CMDB relationships (only when a server is named)
  | streamed
  v
Answer, citing KB numbers (each one a link to the article)

Failure route:  ServiceNow or the model fails or times out
                -> that step is skipped | the chat still answers, without that content
```

A slow or unavailable ServiceNow never blocks the chat. It only removes the runbook content from that one answer.

- **Source badge.** Under each answer: "From your knowledge base: KB0010049" with the article number as a link to ServiceNow; or a warning that the answer is not from the knowledge base; or a warning that the answer mentions an article that was not retrieved.
- **Follow-ups.** Short follow-up questions ("how do I fix it?") are searched together with the previous question.
- **Raise a ticket.** When a message describes a live problem, a **Create incident** button appears. Chat itself never creates a ticket.
- **Logs.** A pasted log is trimmed to its error lines and answered with a severity, a hypothesis and a checklist.
- Chat replies are generated with the model's reasoning mode off, so internal reasoning text never appears in answers.
- **While you wait.** The chat says what it is really doing, with a seconds counter after three seconds: "Searching runbooks…", "Checking the CMDB…", "Waiting for the model…", "Writing the answer…". A step shorter than a third of a second is skipped so the words do not flicker. For a server check in the approver chat the label names the check ("Checking Windows updates on the server…") and changes to "Still waiting for the server to answer…" after six seconds.
- **While it writes.** The answer appears at a steady pace with a blinking cursor, instead of in bursts. It never lags far behind the model, shows everything at once when the browser tab is in the background or the system asks for reduced motion, and does not pull the page down if you scrolled up to read. A table or code block can look unfinished until its last line arrives.

---

## 6. Approver chat

Approvers use the same chat page with extra abilities. The page recognises what an approver asks and routes it:

```
Approver's message
  |
  |-- question about a server (CPU, memory, disk, processes, CMDB) -> live read-only check, answered at once
  |-- request to change something (restart, free up space, unlock, close INC...) -> a card that needs Confirm
  |-- anything else -> the normal chat
```

The page remembers the last server you asked about, so "What about memory?" needs no server name.

### What the chat remembers

Follow-up questions work because the page keeps two things for the length of the conversation:

- **The server you are talking about.** It is set by any question or card that names a server, including the "Email the owners" card, so "is it up yet?" right after a reboot request means that server. A server named in a new message always wins over the remembered one.
- **A short list of what the chat has done** (up to the last 20 entries: owner requests, confirmed actions, closed tickets, each with its ticket number, request code and result). A question such as "what was the ticket number?", "which request was that?" or "show me the incident number" is answered from this list at once, without the model. It is kept in the browser, so it survives a page reload but belongs to that browser and does not carry over to another machine or a cleared browser.

The notes that go to the model as conversation history are plain sentences ("Restart of victim-test is scheduled. Ticket INC0010714."), and the note on a card is rewritten when the outcome is known, so later general questions see what really happened.

### Read-only checks (run immediately)

| Ask | You get |
|---|---|
| "CPU and memory on term-win-srv" | One line per reading, with the threshold flag. Several readings in one question run together. |
| "What is the C drive utilization?" | The named drive (or every disk) with percent used, free and total GB. |
| "Which process is consuming the CPU?" | The top five processes with memory, CPU and service name. |
| "Who owns term-win-srv?" (CMDB, owner, support group, criticality) | The CMDB context for that server. |
| "What is the uptime?" / "boot up time" / "is term-win-srv back?" / "is it up yet?" / "was victim-test rebooted?" | Last start and how long the server has been up. Good for checking a restart you asked for. |
| "Does zabbix-mon need a reboot?" | Whether a restart is pending, and why (Windows servicing keys, Linux reboot-required). |
| "Can term-win-srv reach db01 on port 1433?" / "ping 8.8.8.8 from victim-test" | Name lookup, ping and TCP port test, run from that server. The first server named is the source. |
| "Recent errors in the event log" / "in the journal" | Error and critical entries from the last hour (or the period you give), capped. |
| "Who is logged in on term-win-srv?" | Sessions with their IDs and users. |
| "Which services are stopped?" | Automatic services that are not running (Windows) or failed units (Linux). |
| "List scheduled tasks" / "scheduled jobs" | Non-system tasks (Windows) or systemd timers and cron files (Linux). |
| "Pending Windows updates" / "patch status" / "when was the last patch installed?" | Updates waiting, the latest installed updates, the date of the last install and the Windows Update service state (Windows). |
| "Is the spooler service running?" / "are there any sccm services?" | Looks the service up on the server by name or display name and shows its state, or says plainly that no such service exists. Words like sccm, defender or sql expand to the real service names through the setting `chat.service_search_aliases`. |
| "Patch job status" / "did the update finish?" (the word "job" or "finish" is what selects this one) | Progress or result of the last Windows update install started from the chat, including whether a restart is needed. |
| "Which packages are upgradable?" / "security updates" / "is nginx installed?" | Package lists and pending updates (Linux; apt, dnf and yum are detected). |

Read-only checks fetch credentials through a separate inquiry path with its own hourly limits (60 per hour overall and 20 per person by default). They open no ticket and start no run.

### Changes: ask, review the card, confirm

| You say | Action | Risk |
|---|---|---|
| "restart the print spooler service" | Restart a service (plain names such as "print spooler" map to the real service name through an admin table) | L1 |
| "start nginx" | Start a stopped service | L1 |
| "free up space" / "clean temp files" | Delete temp files older than one day | L1 |
| "clean old logs in C:\inetpub\logs\LogFiles older than 30 days" | Delete old logs; only folders on the administrator's allowed list are accepted; default 14 days | L1 |
| "unlock jsmith" | Unlock an Active Directory account (runs through the one running Windows server) | L1 |
| "stop the print spooler service" | Stop a service (protected services such as WinRM, sshd and TermService are refused) | L1 |
| "disable the print spooler service at boot" / "enable ..." | Change whether a service starts at boot; its current state is not touched | L1 |
| "log off session 3 on term-win-srv" | Log off one session by its ID (the card shows whose session it is; unsaved work is lost) | L1 |
| "run the task Backup on term-win-srv" | Start one listed, non-system scheduled task or timer service now; the chat does not wait for it | L1 |
| "install htop and curl on victim-test" / "update packages nginx" | Linux only. Several named packages in one request. The card shows a dry run first. Owners are mailed after it finishes | L1 |
| "reboot victim-test because kernel patching" / "restart the server victim-test" | Restart the whole server after a warning delay (default 60 s). **Owner approval by email first** | L2 |
| "shut down victim-test" | Power the server off. **Owner approval by email first** | L2 |
| "remove telnet from victim-test" | Linux only: remove named packages (not purge). The dry run must show nothing else being removed. **Owner approval by email first** | L2 |
| "install update KB5007651 on term-win-srv" | Windows only: install the named updates in the background through a one-time scheduled task. The card checks that each number is really pending. No automatic restart. **Owner approval by email first** | L2 |
| "close INC0010689" | Resolve a ticket you opened from the chat, with drafted close notes | n/a |

Polite requests count: "Can we restart the spooler service?" is treated as a request, while "how does restart work?" is a question. If a detail is missing the chat asks for it ("Which folder?", "Which account?"). Kill process and anything rated L2 or L3 is refused from the chat for now.

### How a change happens

```
You (browser)                                        TRIAGE-1 server
-------------                                        ---------------
1  You ask: "Can we restart the spooler service?"
   | message
   v
                                                     2  PROPOSE (changes nothing):
                                                        one action from the verb table + the host
   | card + one-time token
   v
3  You read the card: Confirm or Cancel
   | token
   v
                                                     4  REDEEM the token: once, same person, not expired
                                                        | open YOUR ticket (outside the L1 queue)
                                                        | no ticket = no change
                                                        v
                                                     5  Run the tool, credential checked out in your name
                                                        |
                                                        v
                                                     6  Record it: work note on the ticket + audit rows
   | result + ticket number
   v
7  You see the result
8  Close INC... (optional)
   | second token
   v
                                                     9  Resolve the ticket, only after your second Confirm
```

Steps 2 and 4 are the safety design. Step 2 changes nothing, so a card can be read, ignored or cancelled at no cost. Step 4 only starts from a token that works once, for the same person, before it expires, so the browser cannot alter what runs between the card and the click. Closing the ticket is a separate card with its own token.

### The ticket

- On Confirm, ServiceNow gets a new incident with **you** as caller and assignee and **no assignment group**, so it never lands in the L1 queue that the agent works.
- **You are matched to your ServiceNow user by the email on that user's profile first, then by user name** (the part of your TRIAGE-1 login before the `@`). So a TRIAGE-1 login such as `vedavidyadaran@lab.local` still finds the ServiceNow user `vedavidyadaran` after the profile email was changed to a real address.
- If neither matches, the ticket is still created, unassigned, and the work note names you.
- If the ticket cannot be created, nothing is changed on the server.
- The ticket stays **In Progress**. After a successful action the result card offers a **Close INC...** button.
- **Closing** works only for tickets you opened from the chat and that are not in the agent queue. The drafted notes list what was done; the ticket is resolved only after your second Confirm.

### Linux package changes: dry run first, owners told afterwards

- The card is prepared with a read-only dry run (`apt-get -s`, or `dnf --assumeno`). It shows what would be installed or upgraded, and it is **refused** when the plan would also remove something else, when the package manager reports an error (for example a conflict with an installed package), or when there is nothing to do.
- Package names are validated and passed after `--`, so a name can never become an option or a command. Packages that grant or control privileges (sudo, doas, polkit, pkexec, openssh-server, systemd, libc, kernel images and similar) are on a deny list that an admin can edit. Commands are fixed templates run with `sudo -n`; the chat has no command that raises privileges.
- After a successful install or update, the server's **owners are mailed** what was done, and the same text is written to the ticket. If no owner address is found, or the mail fails, the chat and the ticket say so plainly. The change itself is not undone.

### Owner approval by email (reboot, shutdown, package removal, Windows update install)

These actions can interrupt many people, so the engineer's own Confirm is not enough. The owners of the server approve first, by email, and the engineer pastes their reply back into the chat:

```
You: "reboot victim-test because kernel patching"
  |
  v
Card: who would be disconnected, which owners will be emailed   (nothing sent yet)
  | you press "Email the owners"
  v
TRIAGE-1: opens YOUR ticket  ->  stores request REQ-K7M2QX  ->  mails the owners from the CMDB
  |
  v
Owner replies by email:  "Approved, go ahead tonight."
  |
  v
You paste the WHOLE reply into the chat (with its From line)
  |
  v
TRIAGE-1 checks the reply:
  | - the request code REQ-K7M2QX is in it and the request is yours, open and not expired
  | - only the top of the reply is read; quoted text and signatures below it are ignored
  | - the From address is one of the owners the CMDB lists
  | - an approval word is there ("approved", "go ahead" ...) and NO refusal, hold or condition word
  v
Card: "Owner approval recorded from ann@lab.local ... Go ahead?"
  | you press Confirm (the last step)
  v
Reboot is scheduled -> the result and the reply text are written to the ticket -> owners are told it was done
  |
  v
A background check watches for the server to answer again and writes "back after N min" on the ticket
```

What the check does with a reply:

| Reply | Result |
|---|---|
| "Approved, go ahead tonight." / "No objection, proceed." / "Yes - agreed." | Accepted |
| "Not approved, hold off." / "Don't reboot it today." / "Wait for my call, then go ahead." | **Refused**: the request stays refused and nothing runs |
| "Approved if the backup finishes first." / "Go ahead after 22:00." / "Approved, but tell the team." | **Sent back to you**: a condition needs a person to read it |
| "ok" / "Thanks." | Sent back: no clear approval word |
| From an address that is not an owner, or no From line pasted | Sent back: the sender cannot be confirmed |

The approval, refusal and condition words are admin settings, and the check is stricter than a human would be on purpose: when in doubt it refuses.

> **A pasted email is evidence, not proof.** An engineer could paste a made-up reply. The protections are that the sender must match the CMDB owners, the pasted text is stored on the ticket and in the audit log, the owners are mailed again after the action so a false approval would be noticed, and the request code, expiry (24 hours) and single use stop replays. A stronger design, where owners reply to a mailbox that TRIAGE-1 reads itself, is possible later.

Safeguards specific to these actions:
- **TRIAGE-1 never reboots or shuts down the server it runs on.** On the reference install that is `term-win-srv`: its CMDB name differs from its operating system host name, so the check also compares IP addresses. More servers can be protected with the setting `chat.never_power_hosts`.
- A server that was already restarted or shut down from the chat within the minimum gap (default 60 minutes) is refused.
- The card for a reboot shows who is logged on and would be disconnected.
- Nothing is sent and no ticket is opened until you press "Email the owners". If the mail cannot be sent, the ticket says so and nothing is changed.
- Mail must be able to reach the owners, so owner mail needs SMTP or SES. Set **Mail transport for owner approval** (`owners.mail_transport`) to `smtp` or `ses`: it is separate from the main Notice transport, so P1 alerts can stay on SNS. With SNS only, the chat refuses these actions and says why.
- The reboot check runs in the background; if TRIAGE-1 itself restarts during that time, the check is lost and you ask "is victim-test back?" instead.

### How a Windows update install runs

Windows refuses to install updates from a remote session, so TRIAGE-1 uses a **one-time scheduled task running as SYSTEM**:

```
Confirm
  |
  v
Write a short script on the server  ->  register task TRIAGE1-WU-<job> (SYSTEM, run once)  ->  start it
  |                                                    the chat returns at once
  v
The task uses the built-in Windows Update API for ONLY the KB numbers you named
  | writes progress to C:\ProgramData\triage\wu_<job>.json
  v
It removes its own task and script when done
  |
  v
TRIAGE-1 checks the result file every 2 minutes (up to 40 minutes) -> writes the result on the ticket -> mails the owners
```

Nothing restarts automatically. If an update needs a restart, the result says so and you can ask for a reboot (which needs its own owner approval). KB numbers are validated before they go into the script, and the script holds nothing else from the chat.

### Owner contacts and mail

Owner addresses come from the ServiceNow CMDB record of the server (the server table, `cmdb_ci_server` by default) and are found **through each person's ServiceNow profile**: the server record points at a user, and the **Email on that user's profile** is the address. No custom field is needed. Which fields are read is an admin setting (Notifications section):

| Field on the server CI | Type | Status | What is read |
|---|---|---|---|
| `owned_by` | Reference to User | standard | The user's profile **Email** |
| `managed_by` | Reference to User | standard | The user's profile **Email** |
| `assigned_to` | Reference to User | standard | The user's profile **Email** |
| `support_group` | Reference to Group | standard | The group's **Email** (the group record needs one) |
| `managed_by_group` | Reference to Group | standard | The group's **Email** |
| `u_owner_email` | String (255) | optional, not required | One or more extra addresses (a shared mailbox), separated by comma, semicolon or space. Creating it needs ServiceNow admin rights |

Whoever is named in any of these fields is mailed; duplicates are removed. If none of the fields holds an address, TRIAGE-1 finds nobody to mail and says so on the card instead of sending anything.

Set the owner in ServiceNow by opening the server's CMDB record and filling **Owned by** with a person whose profile has a **deliverable** email address. A lab-only address (for example one ending in `.local`) cannot receive mail: change it on the profile, or choose another person. TRIAGE-1 sign-in names and profile emails are independent, because the engineer's ticket matching falls back to the user name.

**Copy to the engineer.** Owner mail also copies the engineer who asked, using the email on their **ServiceNow profile** (never their sign-in name, which may be a lab-only address that mail systems reject), and never twice on the same message.

> **The mail transport must be able to address people.** SMTP and SES can. **SNS cannot**: it publishes to one fixed topic and ignores the To and Cc lists, so owners would never receive the message. TRIAGE-1 checks this and reports "Owner mail will NOT be sent" on the card and on the ticket instead of pretending. Switch Notifications to SMTP or SES to mail owners.

**SES sandbox.** A new Amazon SES account is in sandbox: it can mail **only verified addresses**. The sender must be verified, and so must every owner mailbox until AWS grants production access (SES console, Account dashboard, Request production access). An unverified recipient is rejected with `MessageRejected: Email address is not verified`; the chat reports it and nothing has changed on the server.

**Same-domain senders.** If the sender and the owner are both `@outlook.com` (or the same mail provider), the provider may treat the message as spoofed and drop it into Junk even though SES reports it as accepted. Look in Junk and mark the sender as safe. The lasting fixes are an SMTP login on the provider itself (an app password; the provider then really sends the mail) or an SES sender on a domain you own with DKIM records.

### Limits you may meet

- Confirmed chat actions are limited per person per hour (default 10 distinct tickets). One restart counts once even though it fetches credentials three times.
- A service can be restarted from chat only a limited number of times per hour (the setting `infra.restart_limit_l1`). On the reference install it is 1, so a second restart of the same service within an hour is refused on purpose, to stop restart loops.
- Cards expire (default 5 minutes) and work once.

---

## 7. Agent console and automatic mode

The agent console lists the L1 queue (incidents in state New, unassigned, in the L1 assignment group), live runs, pending approvals and the audit trail. A run can be started with the Run button, from the queue by the automatic poller, or by an approver. Each run follows a playbook, calls read-only tools, and pauses for approval before any change at or above the approval level.

### Run states

```
                        change needs approval
awaiting approval  <-------------------------------  running ----|--> done          problem fixed, ticket resolved
                   -------------------------------->             |--> escalated    handed to a human (not an error)
                        approved or rejected                     |--> error        failed (counts toward the breaker)
                                                                 |--> interrupted  app restarted mid-run
```

Only **error** counts toward the circuit breaker. An escalation is the system working as designed: the agent decided a human should take the ticket (for example after a loop was detected), and the run ends as escalated, not as a failure.

### Automatic mode

- A poller checks the L1 queue every 15 seconds (setting) and queues tickets it has not worked yet. A ticket that failed is retried the configured number of times (default 1), then left alone.
- The **circuit breaker** switches automatic mode off after a set number of failed runs in a row (default 3) and shows a banner on the console.
- **Re-run all failed** (admin runs list) queues every failed ticket again.
- Queued jobs are ordered by priority and age. A job older than the maximum age expires.

### Run slots: keeping room for people

```
reserved = 0 (default)   [auto] [auto] [auto] [auto] [auto]
                         -> every place is taken by automatic runs: an approver's request waits

reserved = 1             [auto] [auto] [auto] [auto] [ kept for people ]
                         -> automatic runs stop at four | the fifth place is always free for
                            chat tickets, the console Run button and ad-hoc runs
```

Two settings control this: **Tickets running at once** (`runs.max_concurrent`) and **Run slots kept for chat and ad-hoc work** (`runs.reserved_slots`). Automatic work always keeps at least one place, whatever value is entered. Changes apply within seconds.

### The model gate

Every model call passes one priority gate with a configurable number of concurrent slots (`model.slots`). Waiting calls are served by priority: resuming a run, checks, approver runs, chat titles and reranking come before automatic triage, then chat, then ingest. A second setting, `model.reserved_slots`, keeps slots free for the interactive kinds so a title or an approver step never waits behind the automatic backlog. It only helps when the model server really handles requests in parallel.

---

## 8. Knowledge and runbooks

Knowledge lives in ServiceNow. There is no upload page: authoring, approval and ownership happen where the rest of your documentation lives.

```
BEFORE (removed): a local copy you maintained
  Knowledge page (upload a file) -> chunk and embed (local model, about 1 GB)
    -> Postgres table sre_runbooks -> hybrid search (vector + full text) -> Chat

AFTER: ServiceNow is the source of truth
  Author an article (SRE Runbooks KB)
    | publish
    v
  Review and publish (a person decides)
    |
    v
  ServiceNow text index
    | 10 articles
    v
  Model rerank (10 -> 3)
    | best 3
    v
  Chat answer: cites KB numbers + links   <--live--  CMDB relationships (what depends on the server)
```

What changed: the copy you used to upload and maintain is gone, along with the model that embedded it. The one new person-in-the-loop step is publishing in ServiceNow, which is now the review gate for what the chat is allowed to quote.

### Two knowledge bases, two purposes

| Knowledge base | Used by | Setting |
|---|---|---|
| **SRE Runbooks** | Chat answers (human procedures). | `knowledge.kb_name` |
| **TRIAGE-1 Playbooks** | Agent runs only (which tools to call, in what order). Optional: local `skills/` files are used when the source is "file". | `knowledge.skills_source`, `knowledge.skills_kb_name` |

Playbooks do not feed the chat, and adding playbooks does not add chat actions. Chat actions come from the verb table plus code that knows how to read each tool's inputs (section 6).

### How a search works

1. The question (plus the previous question for short follow-ups) is reduced to keywords: error codes and names first, filler words dropped, and nothing that could act as a query operator.
2. ServiceNow's text search runs on the chosen knowledge base, published and active articles only, fetching up to 10 candidates. If nothing matches, it retries with the three longest keywords.
3. The model reorders the candidates and keeps the best three (one short extra call, thinking off). If it is busy, slow or answers badly, ServiceNow's own order is used.
4. For each kept article, the passages that best match the question are taken (up to 1,500 characters by default) and put in the prompt as quoted data.
5. The reranked result is cached, so the same question within two minutes costs no ServiceNow call and no model call.

When a server is named in the question, live **CMDB relationships** are added as "Topology:" lines (the services that depend on it, their criticality, owner and support group). If the server has no linked services in the CMDB there is simply nothing to add.

### Adding runbooks

- Write an article in the **SRE Runbooks** knowledge base and publish it. Use the same sections as the starter set: When to use, Likely causes, Checks, Fix, Escalate when.
- **Draft articles are never used.** The integration user cannot publish unless it is a manager of the knowledge base; the admin **Test** button says when articles exist but none are published.
- The starter set is kept as files in `runbooks/` (nginx 502, print spooler, low disk, high CPU, locked AD account, service down). `python -m scripts.push_runbooks_to_kb` creates or updates the articles (add `--dry` to preview). They are drafts of general practice, so review them before publishing.
- Write articles with the words people actually type. The search is keyword based with a model reorder, so a title and first paragraph that use the real terms ("bad gateway", "502") find the article.

### Switching it off or back

`knowledge.source` accepts `servicenow` (default), `off` (answer without runbook knowledge) and `local` (the old Postgres search, read only, kept for one release as a way back).

---

## 9. Admin panel and settings

The admin page has a **Configuration** area with one section per topic. Sections with a **Test** button check the connection and say exactly what is wrong. Saving applies immediately: secrets are re-synced and caches that depend on the setting are rebuilt, with no restart. Secrets are stored encrypted and are never shown again after saving.

| Panel | Sections |
|---|---|
| Connections | Ticketing (ServiceNow), Identity (AD), Knowledge source, Notifications, Credentials (PAM) |
| Behaviour | Model runtime, Agent behaviour, Limits and timings, Security and access, Safety policy (approvals), Prompts and messages, Telemetry (app monitoring), Backups, Licence and plan |

The registry holds about 115 settings. The ones you are most likely to touch:

| Setting | What it does | Default |
|---|---|---|
| `model.colab_api_url`, `model.name` | Where prompts go and which model name is sent. | set in setup |
| `model.slots` / `model.reserved_slots` | Concurrent model calls, and how many stay free for interactive kinds. | 1 / 0 |
| `runs.max_concurrent` / `runs.reserved_slots` | Tickets worked at once, and how many places are kept for people. | 2 / 0 |
| `agent.auto_max_fails` | Failed automatic runs in a row before automatic mode switches off. | 3 |
| `agent.poll_seconds` | How often the L1 queue is checked. | 15 |
| `agent.checker_mode` | Independent check after a fix: off, shadow (note only) or on (can reopen or close). | shadow |
| `policy.risk`, `policy.approval_from_level` | Risk level per tool, and the level from which a human must approve. | table, 1 |
| `knowledge.source`, `knowledge.kb_name` | Where chat knowledge comes from and which knowledge base. | servicenow, SRE Runbooks |
| `knowledge.max_articles`, `max_chars`, `rerank`, `cache_seconds` | How much knowledge reaches the model, whether to rerank, how long to cache. | 3, 1500, on, 120 |
| `approver_chat.verbs`, `service_aliases`, `action_token_minutes` | Phrase-to-tool table, "print spooler" to "Spooler" names, how long a card stays valid. | see panel, see panel, 5 |
| `credentials.action_max_per_user_per_hour`, `inquiry_*` | Hourly limits for chat changes and chat read-only checks. | 10; 60 and 20 |
| `chat.service_protected` | Services the chat will never stop or disable. | winrm, sshd, ssh, dbus, systemd-logind, rpcss, eventlog, termservice, lsass, networkmanager, network |
| `chat.package_denylist` | Packages the chat will never install or update (a trailing `*` matches a prefix). | sudo, doas, polkit*, pkexec, openssh-server, systemd, glibc, libc6, kernel*, linux-image*, bash ... |
| `chat.max_packages`, `chat.install_timeout_seconds` | Most packages per request, and the longest an install may run. | 10, 300 |
| `owners.email_field`, `owners.user_fields`, `owners.group_fields` | Which CMDB fields supply owner addresses. | u_owner_email, owned_by / managed_by / assigned_to, support_group / managed_by_group |
| `owners.approve_words`, `owners.refuse_words`, `owners.condition_words` | What counts as approval, refusal or a condition in an owner's reply. | see panel |
| `owners.require_sender_match`, `owners.approval_ttl_hours`, `owners.reply_scan_lines` | The reply's From must be an owner; how long a request stays open; how many lines of the reply are read. | on, 24, 3 |
| `chat.patch_poll_seconds`, `chat.patch_wait_minutes` | How often a running Windows update install is checked, and how long to wait for it. | 120, 40 |
| `chat.reboot_delay_seconds`, `chat.reboot_min_interval_minutes`, `chat.reboot_verify_minutes` | Warning time before a reboot, least gap between two on one server, how long to wait for it to return. | 60, 60, 15 |
| `chat.never_power_hosts` | Servers that are never rebooted or shut down from the chat (the TRIAGE-1 server is always protected). | empty |
| Prompts (eight of them) | The wording the model works from. Editing one rebuilds the agent at once. | built-in text |

Anyone with the `config.manage` permission (Admin and Super admin) may change the safety settings. The admin page also shows run statistics, model usage, chat statistics (including how often answers came from the knowledge base), the audit log and the jobs queue.

---

### Licence and plans

A licence key sets the plan: which features are on and how many servers, approver seats and automatic tickets are included.

| | Starter | Team | Commander |
|---|---|---|---|
| Servers | 25 | 100 | unlimited |
| Approver seats | 2 | 10 | 50 |
| Automatic tickets per month | 200 | 1,500 | 10,000 |
| Chat read-only checks | yes | yes | yes |
| Changes from the approver chat | no | yes | yes |
| Owner approval by email (reboot, shutdown, package removal, Windows updates) | no | no | yes |
| Telemetry export and alerts (not built yet) | no | no | yes |

A key carries its own numbers, so a custom deal needs no code change. Without a key the app runs as **Unlicensed**: the Starter limits and no paid features.

```
Vendor (private signing key, never shipped)
  | scripts\licence_tool.py issue --customer ... --plan Team --days 365
  v
Licence key  TRIAGE1-<plan and limits>.<signature>
  | pasted into Admin > Behaviour > Licence and plan
  v
TRIAGE-1 checks the signature with the built-in public key (offline, no internet)
  |-- signature wrong or key edited -> refused when saved
  |-- feature not in the plan       -> that action is refused at once, with a plain message
  |-- over a limit, or expired      -> warning + 14 days of grace -> then only NEW AUTOMATIC tickets stop
  v
People can always sign in, chat and approve.
```

What is counted: **servers** = the servers in the host list; **approver seats** = active people whose role lets them act (approvers and super admins); **automatic tickets** = tickets the automatic mode queues in the calendar month. A warning appears at 80% of a limit. Being back within the limits clears the grace period. The admin Overview shows the plan and the counts; **Check licence** in the section shows the detail. Grace starting and ending are written to the audit log.

Honest limit: the key cannot be forged or edited without the vendor's private key, but someone who can change the program's code can remove the check. A compiled build (planned with the installers) raises that effort; only a hosted service can enforce it fully.

For the vendor: `scripts\licence_tool.py keygen` makes the signing key once (the public half is in `app/licence.py`); `issue` prints a key; `show` reads one. Keep the private key off customer machines and out of the repository: whoever holds it can issue licences.

### Ticketing tools and CyberArk

Each licence key is issued for **one ticketing tool**, and only that tool can be used by that install: `licence_tool.py issue ... --itsm servicenow|jira|bmc|manageengine|freshservice`. The admin panel shows the connection section of that tool only.

| Tool | Sign-in | Ticket id |
|---|---|---|
| ServiceNow | user and password | INC number |
| Jira Service Management (Cloud) | account email and API token | issue key (OPS-123) |
| BMC Helix / Remedy | user and password (JWT) | Incident Number |
| ManageEngine ServiceDesk Plus | technician key | request id |
| Freshservice | API key | ticket id |

Only ServiceNow is proven on a real system. The other four were written from their published APIs and tested against simulated replies: expect field or workflow names to need adjusting on a first real connection.

With a tool other than ServiceNow these are **off**, and say so: the runbook search, CMDB relationships, and server owners. Because owners come from the CMDB, owner approval by email cannot find anyone to mail, so reboot, shutdown, package removal and Windows update install are not available. SLA details are not read. Closing a ticket from chat understands INC numbers and Jira keys, not plain numeric ids.

**CyberArk.** Connections > Credentials > "Where server logins are stored" = `cyberark`, then the CCP address, Application ID and safe (optional client certificate and CA bundle). TRIAGE-1 calls the Central Credential Provider over HTTPS and looks up one account per server, named by the pattern `{key}` (host id first, then address). A value that looks like a private key is used as an SSH key, otherwise as a password. All checkout limits and audit rows work as before. Not yet tested against a real CyberArk.

### Performance page: traces, metrics and logs of the web app

The **Performance** page measures the web app itself with OpenTelemetry (traces, metrics and logs), and nothing else. It answers "is TRIAGE-1 fast and working?", not "how did the agent do?". Agent runs, the run console and the audit log are left out on purpose.

| What is measured | Examples |
|---|---|
| Web requests | Every page and API call: route, status, how long. Grouped into areas: chat, approver chat checks, approver chat actions, tickets from chat, sign-in, admin, pages. |
| Calls to outside systems | ServiceNow (per table and verb), Active Directory, mail (SES or SMTP), the credential store, the knowledge search, and chat/title/rerank calls to the model server. |
| Chat and approver chat | The request behind each chat message, check, card and Confirm: how long it took and whether it failed. Never the message text. |
| App health | Database query time, connections in use and waiting, how long chat waits for a model slot, time to first token, and the app's own CPU, memory and thread count. |

Left out: anything that runs inside an agent run, the poller, dispatcher and watchdog threads, the run and audit tables, the run console's polling (`/api/agent/*`), the admin self-check, and the Performance page's own requests. These are settings, not code (`telemetry.exclude_paths`, `telemetry.exclude_tables`).

```
Web request -> TelemetryMiddleware starts a trace (one root span per request)
  |-- ServiceNow / AD / mail / model / credential store calls -> child spans + a timing metric
  |-- database queries -> a timing metric (a slow query also gets its own span)
  |-- failures -> a log line, tied to the same trace id
  v
Spans, metrics and logs are batched -> written to this app's own Postgres
  (otel_spans | otel_metrics | otel_logs) -> shown on the Performance page
  Nothing is sent outside the machine.
```

The page has four tabs and a time window (1 hour, 6 hours, 24 hours, 3 days):

| Tab | Shows |
|---|---|
| Overview | Requests, failed requests, latency p50/p95/p99, outside-call failures, database p95, model queue wait, app CPU and memory; requests over time with failures in red; tables by area, slowest routes, outside systems, slowest outside operations and the model queue. |
| Traces | Recent requests, filterable by failed only, slower than, and area. Click one to see a waterfall of its steps (for example the ServiceNow call inside a confirm) and the log lines from that request. |
| Logs | The app's own events: failed or slow outside calls and server errors. Click a trace id to open that trace. |
| Model (AI) | The earlier model statistics: calls, tokens, time to first token, latency, cost by skill. These count chat and runs together. |

Which traces are kept: **every failed or slow request is kept**; other requests are kept by chance (`telemetry.trace_sample_percent`, 100 by default). The metrics always count every request, so the numbers do not depend on the sampling. Data older than `telemetry.retention_hours` (72) is deleted every ten minutes, and the stored spans and log lines are capped (`telemetry.max_rows`, 50000).

| Setting (Behaviour > Telemetry) | What it does | Default |
|---|---|---|
| `telemetry.enabled` | Turns collection on or off at once. | on |
| `telemetry.retention_hours`, `telemetry.max_rows` | How long and how much is kept. | 72, 50000 |
| `telemetry.trace_sample_percent` | Share of normal requests whose trace is kept. Failed and slow ones are always kept. | 100 |
| `telemetry.slow_ms`, `telemetry.slow_db_ms` | What counts as a slow request or outside call, and a slow database query (gets its own span). | 1000, 250 |
| `telemetry.log_level` | Lowest level kept: INFO, WARNING or ERROR. | WARNING |
| `telemetry.exclude_paths`, `telemetry.exclude_tables` | Web paths and database tables that are not measured. | run screens and polling; run and audit tables |
| `telemetry.metric_interval_seconds` | How often metrics are written (needs a restart). | 60 |

Who can see it: the same people who see monitoring (`monitor.view`: approvers, auditors, admins). The page shows request routes, system names and error text, never message text, tickets' contents or credentials.

---

## 10. Safety, audit and security

### Risk levels and approval

| Level | Meaning | Tools (default table) | Rule |
|---|---|---|---|
| L0 | Read-only and ticket bookkeeping | checks, status, disk, CPU, memory, processes, logs, lookups, notes, resolve, escalate | Runs automatically |
| L1 | Standard change | restart or start service, clear temp files, clean old logs, unlock account | Needs a human. In chat, the engineer's Confirm; in agent runs, an approval on the console |
| L2 | High risk | reboot, shutdown, package removal and Windows update install (chat only), kill process, and any tool missing from the table | Needs a human. In chat, reboot, shutdown, package removal and Windows update install need the owners' email approval first, then your Confirm. Kill process is not available from chat |
| L3 | Critical | (none by default) | No autonomous action, never from chat |

The levels are enforced in code, not in prompts. A tool that is not in the table counts as L2.

### Credentials

- Server credentials are fetched per use from the credential store, never kept in the app, and never shown to the model.
- There are three separate paths with separate limits and audit rows: an **agent run** (needs a live run), a **read-only inquiry** (approver checks), and a **chat action** (audited under the engineer and the ticket). The agent path refuses to work without a run.
- Root secrets live in an access-restricted file; all other secrets are encrypted in the database.

### Guards around every change

- One host lock per server: two actors cannot change the same server at once.
- Service allow and deny lists, and a never-auto-restore list, are honoured by the tools.
- A restart limit per service per hour stops restart loops.
- Ticket text and article text are treated as data. Intent comes only from the person's own message, and the chat never builds a shell command from message text: arguments are validated and the tools only accept names from allowed patterns and folders.

### Audit

Every approval, run and chat action writes an audit row with who, what and which ticket. Chat actions leave this trail:

| Row | Written when |
|---|---|
| `chat_action_proposed` | A card was shown (nothing changed) |
| `chat_action_confirmed` | The engineer confirmed and the ticket exists |
| `pam_action_checkout` | Each credential checkout for that action |
| `chat_action_done` / `chat_action_failed` | The result |
| `chat_close_proposed`, `chat_ticket_resolved`, `chat_close_failed` | Closing a ticket from chat |

---

## 11. Operations and troubleshooting

### Routine

- **Code changes need a restart; settings do not.** After copying new files, stop and start the app, then hard-refresh the browser (Ctrl+F5).
- **Backups are automatic.** Every day at the hour set in Behaviour > Backups (02:00 by default) the app writes a `pg_dump` of the `triage` database and a copy of `bootstrap.json` to the backup folder (`backups` next to `bootstrap.json` unless you set another), keeps 14 days and records the result in the audit log (`backup_done` / `backup_failed`). If the app was off at that hour it backs up when it is next running and the last good backup is over 36 hours old. **Back up now** in that section makes one at once. No scheduled task and no elevated session are needed. Backups are **encrypted by default** with a recovery key the app creates. A super admin must open Behaviour > Backups, press **Show recovery key**, store it outside the server and press **I have saved it**; the Overview warns until then. Without the key, backups cannot be restored after losing the server. Retention is yours to set: keep every backup for N days, one a week for N weeks, one a month for N months; the newest is never deleted. **Copy each backup to** a second disk or share (give the server's computer account write access). The Overview warns, and `backup.alert_email` is mailed at most once a day, when backups are old, failing or not being copied. The Windows installer makes a backup before an upgrade and stops if it fails. To restore, run as an administrator on the server `triage1.exe launch.py restore <path to triage-date.dump.enc>` from the install folder: it asks for the recovery key and the word RESTORE, stops the service, backs up the current data, restores in one transaction, and starts the service again. In Docker the steps are at the top of `docker-compose.yml`.
- Run the tests: `.venv\Scripts\python.exe -m pytest tests -q` (542 pass at the time of writing).
- After editing the page scripts, check them with `node --check`. The scripts in `static/` are not type checked.

### Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| "Not Found" when a card should appear | The page is new but the server is old. Compare the server's start time with the file's modified time and restart. |
| An approver's question gets the generic "I'm a triage assistant" reply | The page script is cached (hard-refresh), or the signed-in account is not an approver. The live-check and action cards only work with the `console.act` permission. |
| "TRIAGE-1 has a long queue right now" | The waiting list is full of requests people are waiting on. Automatic backlog no longer counts toward this limit; if you still see it on an old build, restart. |
| An ad-hoc request or card result is slow to start | All run places are busy. Set `runs.reserved_slots` to keep a place for people. |
| Second restart of the same service is refused | The per-service restart limit per hour (`infra.restart_limit_l1`). Wait, or raise it knowingly. |
| Knowledge Test says the KB was not found | No knowledge base with that exact title. Check `knowledge.kb_name` or create it. |
| Test says articles exist but none are published | Publish them in ServiceNow. The integration user cannot publish unless it manages the KB. |
| Answers cite no article or say "not from your knowledge base" | No article matched. Check the article is published and uses the words people type. Check the server log for a "Knowledge search" warning. |
| Chat answers show internal reasoning text | The model server ignores the thinking-off switch. Strip it server side or use a model setup that honours it. |
| Sign-in returns 401 on a fresh install | bcrypt 5 with passlib. Keep `bcrypt==4.0.1`, `passlib==1.7.4`. |
| AD sign-in: invalid credentials or "not in any mapped group" | Check the user is under the domain root, the group is under the Search Base and is mapped on the Users page; the log names the directory's reason. |
| Everything fails with a `NoneType` error after a code edit | A function argument named `config` shadows the settings module. In the agent module use `app_config`. |
| Owner approval mail is not in the inbox | Check Junk first. If SES says `Email address is not verified`, the account is in sandbox and that mailbox must be verified. Sender and owner on the same provider can be dropped as spoofing (see "Owner contacts and mail"). |
| The card says no owner address was found | The server's CMDB record has no `owned_by`, `managed_by`, `assigned_to` or group with an email, or the person's profile Email is empty. Fill the owner on the CI and the profile Email, or set `u_owner_email`. |
| "Is it up yet?" or "what was the ticket number?" gets the generic "I'm a triage assistant" reply | An old page script is cached: hard-refresh (Ctrl+F5). Ticket numbers are remembered per browser; ask in the same browser where you ran the action. |
| The Performance page says "Nothing recorded" | Metrics are written once a minute: use the app, wait a minute, press Refresh. Check the badge on the page says Collecting and `telemetry.enabled` is on. After a code update the app must be restarted once. |
| Performance numbers look too low or traces are missing | Run screens and polling are excluded on purpose (`telemetry.exclude_paths`). Normal fast requests are kept by chance (`telemetry.trace_sample_percent`); the counts and percentiles still include every request. |
| Jobs stay queued | The model health check is failing (the app calls `/health` on the model server) or all run places are busy. |

---

## 12. Known limits and roadmap

### Exercised live, and not yet

On 2026-11-10 the owner approval path was run for real once: a reboot of `victim-test` (ticket INC0010714). The request mail was sent through SES, the reply was pasted and accepted, the reboot ran, the background check wrote "back after about 1 min" on the ticket, and a second reboot request two minutes later was refused by the 60 minute guard. Everything below was tested with mocks and read-only live checks only:

- package install and removal on Linux, and Windows update install by KB number
- the "done" mail to owners reaching a different person than the requester
- a patch watcher or reboot verifier surviving a restart of TRIAGE-1 (it does not; see the limits)

### Limits today

- Telemetry stays inside TRIAGE-1: there is no OTLP export to Grafana, Jaeger or a collector yet, and no alerting on the numbers.
- Whether an approver action succeeded is judged by the HTTP status of its request. A confirm that returns 200 with a failed action is not counted as a failed request (the ticket and audit log show it).
- Percentiles are estimated from histogram buckets (accurate to the bucket), not computed from every sample.
- Calls made by timer threads (the chat ticket undo timer, the reboot and patch watchers) are traced on their own, without a web request above them.

- Owner mail works only for recipients SES can deliver to: in sandbox each owner mailbox must be verified, and a mail provider may filter same-domain senders to Junk.
- The list of what the chat did (used for "what was the ticket number?") lives in the browser, not on the server.
- From chat you can close only tickets you opened there. There is no "pick up an existing ticket" yet.
- Kill process and other L2 actions are not available from chat until the owner-approval step exists.
- Keyword search with a model reorder can miss an article whose wording shares nothing with the question. Good titles and first paragraphs matter.
- The first runbook search takes about three seconds (ServiceNow plus one model call). Repeats within the cache time are instant.
- The model health check expects a `/health` address. Servers without one (some local runtimes) look offline until a fallback is added.
- The reserved model slots only help when the model server serves requests in parallel.

### Planned

1. **Pick, assign and work an existing ticket** from chat, with engineer-to-ServiceNow-user matching by email and a takeover note.
2. **Kill process from the chat** using the same owner approval, and owners replying to a mailbox that TRIAGE-1 reads itself instead of the engineer pasting the reply (stronger evidence).
3. **Deployment profiles at first run** (single machine, app plus GPU box, hosted model as an explicit opt-in, custom).
4. **Rotation of credentials** that were once stored in the project history: the checklist is `docs/CREDENTIAL-ROTATION.md` (built from `scripts/scan_history.py`, which lists where a secret is and what kind, never the value). The rotation itself is done by you at each system.
5. **Removing the old local runbook store** (table, search function, embedding library and the `local` option) one release after the ServiceNow knowledge base proves itself.
