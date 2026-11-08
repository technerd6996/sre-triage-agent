# TRIAGE-1 (sre-triage-agent): project memory

## What it is
An L1 SRE agent that works ServiceNow incidents. FastAPI app (`app/main.py`), LangGraph agent
(`app/agent/`), prompt-and-JSON tool calling to an OpenAI-compatible model endpoint, human approval for
risky actions, an admin panel, and a chat UI. Windows Server VM, PowerShell, Python 3.13 in `.venv`.

## Run it
- `powershell -ExecutionPolicy Bypass -File scripts\start.ps1` starts local Postgres if needed, applies
  `db/schema.sql`, then serves on `127.0.0.1:8000`. The first run installs Postgres 17 (needs elevated PowerShell).
- `scripts\reset_local.ps1` empties the database so `/setup` shows again (asks for YES; keeps the install).
- Tests: `.venv\Scripts\python.exe -m pytest tests -q` (242 pass at last run).
- Chat knowledge comes from ServiceNow (`app/knowledge.py`): the KB named in `knowledge.kb_name` (default "SRE Runbooks";
  playbooks stay in "TRIAGE-1 Playbooks" for agent runs). ServiceNow text search (`123TEXTQUERY321` on `kb_knowledge`,
  published + active) fetches candidates, the chat model reranks them (`knowledge.rerank`, gate kind `rerank`, falls back to
  ServiceNow's order), the best passages go in the prompt; the answer cites KB numbers and the badge links to the article.
  When a server is named, live CMDB relationships (`cmdb.service_context`) are added as "Topology:" lines. Results are cached
  (`knowledge.cache_seconds`). `knowledge.source` = servicenow | local | off: `local` is the OLD Postgres runbook search (way
  back for one release; no upload page, embedder loads lazily); `off` answers without knowledge. The Knowledge Manager page,
  `/api/rag/*`, and the `kb.view`/`kb.manage` permissions are gone; authoring happens only in ServiceNow. Neo4j is gone too.
  The admin Test button on "Knowledge source" says whether the KB exists and has readable published articles.
- There is NO `.env`. Root secrets (`DATABASE_URL`, `TRIAGE_SECRET_KEY`) live in
  `C:\ProgramData\triage\bootstrap.json` (owner + Administrators only; code in `app/bootstrap_store.py`).
  Back that file up: without the key, saved credentials cannot be read. Everything else is set on the setup
  page / admin panel and stored in the database.

## Plans in progress
Read `docs/PLAN-approver-chat-gpu-lane-icons.md` before touching any of these three. It has the design, the
open decisions and what was drafted but may not be applied yet:
- Approver chat: work existing incidents (pick, assign to the engineer, act on the host) instead of only creating new tickets.
- Model lane: keep model capacity free for titles and approver runs.
- Favicons: flat theme-coloured icons in `static/icons/`, one per page or role.

## Architecture notes
- Database: local PostgreSQL via `app/db.py`, a small layer that mimics the old supabase-py calls
  (`store.supabase.table(...).select().eq()...execute()`). No cloud Supabase, no SQLite. Agent state uses
  LangGraph `PostgresSaver`. Embeddings are `double precision[]` with a SQL `cosine_sim` (no pgvector).
- Settings: `app/config.py` registry (`SETTINGS`, `register`, `_add`). About 106 settings in 11 groups. Saving
  applies immediately (hot apply): bridged env vars are re-synced and `config.on_change` listeners rebuild
  caches. Read settings at use time with `config.get("group.key")`, never at import time.
- Prompts (8) and the tool risk table (`policy.risk`) are settings with the old text as the default.
  The agent graph is cached in `get_agent`; it is rebuilt when a prompt, policy or model URL setting changes.
- RBAC: `app/rbac.py`. Roles: auditors, users, approvers, admin, super_admin. Admin has no console or
  Users-page access; only super_admin manages users. AD group mapping needs `users.manage`. Every route needs a
  rule (deny by default); `/api/approver-chat/*` needs `console.act` and must never be given to users.
- Identity: Active Directory over LDAPS (`app/adapters/ad/identity.py`). Users are found across the whole
  domain; only groups under the Search Base grant roles. Failed UPN bind retries as `NETBIOS\sam`.
- Runs: status `done`, `error`, `escalated` (hand-over to a human; not an error and not a breaker failure),
  `interrupted`. Auto mode (poller) skips tickets already worked; failed tickets retry
  `limits.auto_retry_failed` times (default 1). A circuit breaker turns auto mode off after
  `agent.auto_max_fails` failures in a row and shows a banner. `runs.max_concurrent` is 12 on this machine.
- Model: URL in `model.colab_api_url`. `_model_online` calls `GET {base}/health`; jobs stay queued while it fails.
  Ollama/LM Studio lack `/health` (planned fallback to `/v1/models`).
- Model gate (`app/llm/gate.py`): one priority gate in front of the model, `model.slots` slots (default 1).
  Priority orders waiters but reserves nothing. Every model call takes `llm_slot(kind)`; the kind sets the
  priority (`PRIORITY` dict). Do not nest `llm_slot()` in one thread. Titles are `POST /api/title` in `main.py`.
- Approver chat (`app/api/approver_chat.py`, logic in `app/chat_actions.py`): `/inquire` and `/cmdb` are read-only
  checks (CPU, memory, processes, CMDB; "CPU & memory" runs both). Changes go in two steps: `/act/propose` reads the
  message with an admin verb table (`approver_chat.verbs`) and returns a card, `/act/confirm` redeems its single-use,
  per-user, expiring token. On Confirm: a ServiceNow ticket owned by the engineer is created OUTSIDE the L1 queue
  (`create_incident_for`), the tool runs directly (no agent run, no queue) under `tools/actor.acting_as` using
  `pam.checkout_for_action` (audited under the engineer, limited per hour by distinct ticket), result goes to the ticket.
  Actions today: restart/start service, clear temp files, clean old logs, unlock AD account (all L1; L2/L3 refused).
  "close INC..." resolves a ticket the engineer opened from the chat. Adding an action = an entry in `ACTIONS` plus a verb
  line. Playbooks (skills) do NOT feed the chat; they only steer agent runs. `/run` still exists but the UI no longer uses it.
- Ticketing port (`app/ports/ticketing.py`): `claim()` assigns to TRIAGE-1's own ServiceNow user; `create_for_user()` makes
  a ticket owned by a person. No general "assign to engineer" / pick-a-ticket yet.
- Run slots: `runs.max_concurrent` and `runs.reserved_slots` (automatic runs may use max minus reserved; people's runs
  may use all), `_can_start(source)` in `app/api/agent.py`. Model-call lane: `model.slots`, `model.reserved_slots`.

## Gotchas that already bit us
- A function parameter named `config` shadows the settings module (broke every run once). In
  `app/agent/agent.py` use `app_config`.
- `bcrypt` must stay `4.0.1` with `passlib==1.7.4`; bcrypt 5 breaks login with 401.
- Copies such as `D:\Triage-One` share this machine's database and `bootstrap.json`, so a copy is not a fresh
  install. Copy the whole `app\`, `static\`, `tests\` folders, not single files.
- JS in `static/*.html` is not type-checked: run `node --check` on extracted scripts after editing.
- Do not run `git stash` mid-task (it once hid uncommitted work).
- `/static` is exempt from auth, so anything every page needs before login (icons, css) must live under `static/`.
- Favicons use fixed gradient values copied from `static/css/theme.css`; if the theme accent changes, update the
  SVGs. Browsers cache favicons hard: test with Ctrl+F5 or a private window.

## Open items
- Rotate everything that was in the old `Creds/` folder; it is still in git history.
- Nothing from the recent work is committed. Run `git add -A`, check `git status`, then commit.
- The approval path has never been exercised (0 approval requests recorded).
- Knowledge move to ServiceNow: needs the "SRE Runbooks" KB created in ServiceNow with published articles the integration
  user can read (it did not exist at build time; the Test button says so). Existing local runbooks are NOT migrated: export
  them from `sre_runbooks` and create articles (see `scripts/push_skills_to_kb.py` for how articles are written).
  Not yet seen live: a real chat answer from a runbook article, and the rerank on a real model (first search ~3.5 s with
  rerank, repeat 0 s). One release later remove: `sre_runbooks`, `hybrid_search`, lazy embedder, sentence-transformers,
  the `local` option. `static/v1/*.bak` still hold old copies of the removed page.
- `prompt.system` still says "Source: graph" and calls topology lines "from the graph"; the CMDB lines use the same
  "Topology:" wording on purpose so Mode 2T keeps working.
- Planned: deployment profiles at first-run setup (Single machine, App + GPU box, Hosted LLM opt-in, Custom),
  choice hidden after setup but re-openable with the setup code; admin password chosen in the wizard
  (the hardcoded default still exists in `app/bootstrap.py`); `_model_online` fallback to `/v1/models`.
- Schedule backups: nightly `pg_dump` of the `triage` database and a copy of `bootstrap.json`.
- Not yet clicked through in a browser: textarea/on-off controls in the admin config pane, Re-run all failed,
  the auto-mode banner, and the Escalated badge.
- Approver chat: built; tested with mocks plus live read-only proposals. The first real restart and close were done by the
  user (INC0010689). Still to build: pick/assign an existing ticket, the L2 owner-approval popup (kill process),
  resolve/escalate with drafted notes for other tickets. Model lane layers 1 and 2 are applied; raise `model.slots` and
  set `model.reserved_slots` only if the model server handles parallel requests (not checked which server it is).
- Restart is needed after Python changes (settings are hot-applied, code is not). Compare the server's start time with
  the file's modified time before assuming a fix did not work.
- Favicons: linked on all 8 pages (icons in `static/icons/`). Not yet checked in a real browser.

## Editing gotchas (this environment)
- Inline `python - <<EOF` / `python -c` scripts mangle backslashes: `\b` in a regex becomes a backspace byte and `\n` becomes
  a real newline. Write helper scripts with the Write tool, or use Edit. After JS/Python edits run
  `grep -rlP '\x08' app static tests` (it must print nothing).
- The code lives in `C:\Users\VedaVidyadaran\sre-triage-agent`; the app runs from `D:\sre-triage-agent`. After every change copy
  the files to D and run `diff -rq` on app, static, tests, db. D has no `.venv` of its own (the C venv is used).

## How the user likes to work
- Ask when something is genuinely unclear; do not assume. Keep answers short and plain.
- Prefers everything configurable in the admin panel, no hardcoded values, nothing in the cloud.
- Wants changes verified (tests, live checks) and an honest report of what was not tested.
