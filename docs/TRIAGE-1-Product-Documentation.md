# TRIAGE-1 Product Documentation

An L1 SRE assistant for ServiceNow incidents. Document date 2026-11-09. Describes the build that passes 242 automated tests.

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
| PostgreSQL 17 (local, localhost only) | Users and sessions, runs and their events, approvals, the audit log, the job queue, settings (secrets are encrypted), model call statistics, chat statistics. Agent checkpoints use the same database. |
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

The first run creates a super admin account, `superadmin@triage.local`, with a default password defined in `app/bootstrap.py`. The login page shows a warning while that password is unchanged. Change it at first sign-in. A password chosen during setup is on the roadmap.

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

### Read-only checks (run immediately)

| Ask | You get |
|---|---|
| "CPU and memory on term-win-srv" | One line per reading, with the threshold flag. Several readings in one question run together. |
| "What is the C drive utilization?" | The named drive (or every disk) with percent used, free and total GB. |
| "Which process is consuming the CPU?" | The top five processes with memory, CPU and service name. |
| "Who owns term-win-srv?" (CMDB, owner, support group, criticality) | The CMDB context for that server. |

Read-only checks fetch credentials through a separate inquiry path with its own hourly limits (60 per hour overall and 20 per person by default). They open no ticket and start no run.

### Changes: ask, review the card, confirm

| You say | Action | Risk |
|---|---|---|
| "restart the print spooler service" | Restart a service (plain names such as "print spooler" map to the real service name through an admin table) | L1 |
| "start nginx" | Start a stopped service | L1 |
| "free up space" / "clean temp files" | Delete temp files older than one day | L1 |
| "clean old logs in C:\inetpub\logs\LogFiles older than 30 days" | Delete old logs; only folders on the administrator's allowed list are accepted; default 14 days | L1 |
| "unlock jsmith" | Unlock an Active Directory account (runs through the one running Windows server) | L1 |
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
- If no ServiceNow user has your email, the ticket is still created, unassigned, and the work note names you.
- If the ticket cannot be created, nothing is changed on the server.
- The ticket stays **In Progress**. After a successful action the result card offers a **Close INC...** button.
- **Closing** works only for tickets you opened from the chat and that are not in the agent queue. The drafted notes list what was done; the ticket is resolved only after your second Confirm.

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
| Behaviour | Model runtime, Agent behaviour, Limits and timings, Security and access, Safety policy (approvals), Prompts and messages |

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
| Prompts (eight of them) | The wording the model works from. Editing one rebuilds the agent at once. | built-in text |

Anyone with the `config.manage` permission (Admin and Super admin) may change the safety settings. The admin page also shows run statistics, model usage, chat statistics (including how often answers came from the knowledge base), the audit log and the jobs queue.

---

## 10. Safety, audit and security

### Risk levels and approval

| Level | Meaning | Tools (default table) | Rule |
|---|---|---|---|
| L0 | Read-only and ticket bookkeeping | checks, status, disk, CPU, memory, processes, logs, lookups, notes, resolve, escalate | Runs automatically |
| L1 | Standard change | restart or start service, clear temp files, clean old logs, unlock account | Needs a human. In chat, the engineer's Confirm; in agent runs, an approval on the console |
| L2 | High risk | kill process, and any tool missing from the table | Needs a human. Not available from chat yet |
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
- Back up nightly: a `pg_dump` of the `triage` database and a copy of `bootstrap.json`. Scheduling this is on the roadmap.
- Run the tests: `.venv\Scripts\python.exe -m pytest tests -q` (242 pass at the time of writing).
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
| Jobs stay queued | The model health check is failing (the app calls `/health` on the model server) or all run places are busy. |

---

## 12. Known limits and roadmap

### Limits today

- From chat you can close only tickets you opened there. There is no "pick up an existing ticket" yet.
- Kill process and other L2 actions are not available from chat until the owner-approval step exists.
- Keyword search with a model reorder can miss an article whose wording shares nothing with the question. Good titles and first paragraphs matter.
- The first runbook search takes about three seconds (ServiceNow plus one model call). Repeats within the cache time are instant.
- The model health check expects a `/health` address. Servers without one (some local runtimes) look offline until a fallback is added.
- The reserved model slots only help when the model server serves requests in parallel.

### Planned

1. **Pick, assign and work an existing ticket** from chat, with engineer-to-ServiceNow-user matching by email and a takeover note.
2. **Owner approval for high-risk actions** (kill process): a popup with "request approval", which emails the server owners and waits for an approver, or "no approval needed" with a typed reason that is audited and mailed afterwards.
3. **Deployment profiles at first run** (single machine, app plus GPU box, hosted model as an explicit opt-in, custom) and a password chosen during setup.
4. **Scheduled backups**, and rotation of credentials that were once stored in the project history.
5. **Removing the old local runbook store** (table, search function, embedding library and the `local` option) one release after the ServiceNow knowledge base proves itself.
