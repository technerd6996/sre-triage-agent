# Plan: approver chat flow, model lane, favicons

Written 2026-11-08. Read CLAUDE.md first. This file holds three pieces of work: (1) approvers working incidents through the chat panel, (2) keeping the model free for titles and approver runs, (3) favicons. Nothing here is committed. Where a section says "decision needed", do not guess: ask the user.

## Status at a glance

| Piece | State |
|---|---|
| Approver chat: working-ticket flow | Designed. Four decisions still open (below). No code yet. |
| Model lane, layer 1 (title kind, token cap, approver kind) | Code was drafted in chat. Unknown whether applied: check (see "Verify" in section 2). |
| Model lane, layer 2 (reserved slot) | Designed. Blocked on two answers: what serves the model, and the value of `model.slots`. |
| Favicons | Final icons made (flat, theme colours). Needs the `<link>` tags added to 8 pages. |

## 1. Approver chat: work existing incidents from chat

### What exists today (`app/api/approver_chat.py`)
- `POST /api/approver-chat/run` makes a NEW ticket from the instruction and runs the normal pipeline (`start_run(source="approver_adhoc")`, or `jobs.enqueue(..., priority 1)` when busy). It never works an existing ticket.
- `POST /api/approver-chat/inquire` is read-only: top processes, CPU, memory, or CMDB context for a named server. No ticket, no run.
- `POST /api/approver-chat/cmdb` is a read-only CMDB lookup.
- RBAC: the whole prefix needs `console.act` (approvers). Users must never inherit it.
- Gaps: no memory between messages; `claim()` on the ticketing port assigns to TRIAGE-1's own ServiceNow user, never to the engineer; the port has no assign.

### Target behaviour
The engineer says "pick INC0010123". That ticket becomes the session's working ticket. After that, "restart the service", "check disk", "what is eating CPU" act on that ticket's host without naming it again. The page shows a chip with the working ticket and a Release button.

### Command vocabulary (closed, not free-form)

| Engineer says | What happens |
|---|---|
| pick INC… / take INC… | Validate, assign to the engineer, set In Progress, work note, bind session to ticket and host |
| release / drop it | Clear the session binding; optionally unassign and note |
| status | Show ticket state, host, last actions |
| read-only asks (cpu, memory, processes, disk, service status, CMDB) | Run the existing inquire path against the working ticket's host. L0, immediate. |
| change asks (restart/start service, clear temp files, clean old logs, unlock AD account, kill process) | Map to a tool in the risk table, then gate by level (below) |
| resolve / escalate | Draft the notes from the chat, show them, send on the engineer's Confirm |
| triage it | Hand the ticket to the normal agent run, started under the engineer's name |

Parsing: deterministic first. Ticket numbers by regex (`\bINC\d{5,}\b`). Verbs and phrase-to-tool mapping come from a settings table (admin-editable, no hardcoding). The model is used only to extract slots (service name, process) in JSON mode, and the chat asks back when a slot is missing. Never build a shell command from chat text. Ticket text is data: intent comes only from the engineer's own message.

### Pick rules
- Ticket must exist and not be Resolved/Closed/Canceled.
- Assigned to someone else: ask "take over?" and record the takeover in a work note.
- Resolve the host from the ticket CI via `resolve_server` (`app/agent/hosts.py`).
- Assign to the engineer (needs the engineer to ServiceNow user mapping, decision 2).
- Work note: "Picked up by <name> via TRIAGE-1 chat."

### Approval model for change asks
Same tools, same `policy.risk` table (admin setting), same host lock, PAM and audit path as the agent. Call the tool directly; do NOT spawn an agent run per command.

- L0: runs immediately.
- L1: chat shows a confirm card ("Restart nginx on HOST? Confirm / Cancel"). The Confirm click is the approval, recorded in the audit log under the engineer's name. (Recommended; see decision 1.)
- L2 (`kill_process`): confirm card plus a stronger gate: typed confirmation or a second approver.
- L3: refused.

Policy knobs that already exist and must still apply: `policy.approval_from_level`, never-auto services, flap limit, host lock.

Before coding, read `app/tools/infra.py`, `app/tools/host_lock.py`, `app/pam_service.py` and how `/inquire` sets the audience (`set_audience("l2")`) to confirm tools can be called outside an agent run with the right audit identity.

### Session state
New table (add to `db/schema.sql`, which `start.ps1` applies): approver id, working ticket, host reference, picked_at, last_active. Persisted so it survives restarts. Expires when idle (setting, e.g. `approver_chat.idle_minutes`). One working ticket per approver at a time. Add a lock so two engineers cannot both drive the same ticket.

### Phases
1. Ticketing port `assign` (+ ServiceNow adapter, reuse `_user_id`/`_incident_id` helpers in `app/adapters/servicenow/client.py`), engineer mapping, session table, pick/release/status endpoint, UI chip.
2. Bind read-only asks to the working ticket; post an auto work note for each.
3. Change asks with confirm cards through the policy table, audited.
4. Resolve/escalate with drafted notes; "triage it" handoff.

Each phase ends with tests (`tests/`), a live check against the real ServiceNow, and an honest note of what was not exercised. The approval path has had no recorded approval requests so far, so test L1 confirm end to end.

### Decisions needed from the user
1. Approval model: is the engineer's own Confirm the approval for L1? For L2: typed confirmation or second approver?
2. Engineer to ServiceNow user: match on email, or an editable `snow_user` field on the user row?
3. Picking a ticket TRIAGE-1 is currently running: pause that run, or refuse until it finishes?
4. Pick scope: only unassigned L1-queue tickets, or any open ticket?

## 2. Keeping the model free for titles and approver runs

### Facts from the code
- `app/llm/gate.py`: one priority gate, `model.slots` slots (default 1). Priority only orders waiters; it does not reserve capacity, so one long step still blocks everyone behind it.
- PRIORITY = resume 0, verify 1, triage 2, chat 3, ingest 4 (before layer 1).
- `POST /api/title` (`main.py`) used kind `chat`, waited 15 s, then returned the constant "Incident Triage". It also inherited `model.max_tokens` (1024) because `llm_post` had no per-call cap.
- Approver runs went through `start_run` as kind `triage`, so they queued behind auto-mode runs.
- `runs.max_concurrent` is 12 on this machine, so runs do not wait for a run slot. The bottleneck is the model gate.
- Unknown: what serves the model (vLLM, Ollama/LM Studio, Colab /generate) and the current `model.slots`.

### Layer 1: no GPU change needed (code drafted in chat)
- `gate.py`: `PRIORITY = {"resume": 0, "verify": 1, "approver": 1, "title": 1, "triage": 2, "chat": 3, "ingest": 4}`
- `app/llm/client.py`: `llm_post(messages, timeout, max_tokens=None, thinking=None)`; `thinking=False` adds `chat_template_kwargs.enable_thinking=false`.
- `main.py` `/api/title`: kind `title`, `max_tokens=48`, `thinking=False`, `log_stream("title", ...)`, and a local fallback title (INCnnnnn follow-up or the first six words) instead of the constant.
- `app/api/agent.py` `start_run`: pass `kwargs={"kind": "approver" if source == "approver_adhoc" else "triage"}` to `_consume`.

Verify it is applied: `grep -n approver app/llm/gate.py`, and check `llm_post` has `max_tokens`. Then run pytest. With layer 1 alone, a title or approver step waits only for the call in flight, not the whole queue.

### Layer 2: reserved lane (needs `model.slots` of 2 or more)
- New setting `model.reserved_slots` (default 0 so behaviour is unchanged until set).
- Background kinds (triage, ingest) may hold at most `slots - reserved` slots. Interactive kinds (title, approver, resume) may use all.
- The gate must pick the first ELIGIBLE waiter, not just the head of the heap, or a capped background entry would block an interactive one.
- Only works if the server handles concurrent requests. vLLM does (extra cost is KV cache, not another copy of the weights; a title is about 15 tokens). Ollama needs `OLLAMA_NUM_PARALLEL` of 2 or more. A strictly one-at-a-time endpoint cannot have a real lane; there only layer 1 and shorter jobs help.
- Add tests in `tests/test_llm_gate.py` for: interactive proceeds while background is capped; background cannot take the reserved slot; reserved 0 behaves as today.

### Layer 3 (reserve a run slot): dropped
`runs.max_concurrent` is 12, so it is not needed. Revisit only if the poller floods the gate; then cap auto runs at `max_concurrent - 1`.

### Check after changes
With two or three auto runs in flight: open a new chat (title appears fast or uses the local fallback), raise an approver ad-hoc run, and read the admin LLM stats. New title and approver kinds should show short wait times.

## 3. Favicons

### Decision
Flat vector icons in the app theme (`static/css/theme.css`: gradient `#7c3aed` to `#3b82f6`, `--grad`), white glyphs, one stroke weight, no gold, no shading. The earlier navy-and-gold AI image set was dropped because it did not match the theme and blurred at tab size.

### Files (project root, folder `static/icons/`)
`NAME.svg`, `NAME-32.png`, `NAME-48.png`, `NAME-180.png` for each of: brand, user, approver, auditor, admin, superadmin, chat; plus `favicon.ico` (the brand icon, 16/32/48). `/static` is exempt from auth, so the icons load on every page including login and setup. The SVGs are the source of truth; the PNGs were rendered from them with `rsvg-convert`.

### Page mapping

| Page | Icon |
|---|---|
| `static/index.html` (user chat) | user |
| `static/agent.html` (approver console) | approver |
| `static/admin.html` | admin (swapped by role, below) |
| `static/chat_off.html` | chat |
| `static/knowledge.html`, `login.html`, `setup.html`, `change-password.html` | brand |

This mapping was a guess by Claude. If the new approver chat gets its own page, give it `chat`.

### Link tags
Replace `<link rel="icon" href="data:,">` where present; add after `<title>` elsewhere.

```html
<link rel="icon" type="image/svg+xml" sizes="any" href="/static/icons/NAME.svg">
<link rel="icon" type="image/png" sizes="32x32" href="/static/icons/NAME-32.png">
<link rel="apple-touch-icon" href="/static/icons/NAME-180.png">
```

### Optional: role icon on the admin page
`admin.html` already has `const ME = fetch('/api/auth/me')...`. `/api/auth/me` returns `role` (comma list of super_admin, admin, auditors, approvers, users). Add a script after that line:

```js
ME.then(m => {
  const roles = String(m.role || '').split(',').map(s => s.trim());
  const pick = roles.includes('super_admin') ? 'superadmin'
             : (!roles.includes('admin') && roles.includes('auditors')) ? 'auditor' : null;
  if (!pick) return;
  document.querySelectorAll('link[rel="icon"]').forEach(l => {
    l.href = l.href.replace('/icons/admin', '/icons/' + pick);
  });
});
```

### Optional: `/favicon.ico` route
Only for tools that probe it. Add to `main.py`:

```python
from fastapi.responses import FileResponse

@app.get("/favicon.ico", include_in_schema=False)
def favicon_ico():
    return FileResponse(BASE_DIR / "static" / "icons" / "favicon.ico", media_type="image/x-icon")
```

and add `"/favicon.ico"` to the allowlist tuple at the top of `AuthMiddleware.dispatch` (`app/auth.py`) and to `_SKIP` in `app/rbac.py`, or the route redirects to login and RBAC warns about an unruled route.

### Notes
- Browsers cache favicons hard: hard-refresh (Ctrl+F5) or use a private window to check.
- The icons use fixed gradient values. If the accent in `theme.css` changes, update the two colours in the SVGs.
- Several `<title>` tags in the code dump show `Â·` where `·` belongs. Check a real tab; if it is real, `scripts/fix_mojibake.py` exists for that.
- Not yet checked in a real browser.

## 4. Noticed but not fixed
- `app/api/agent.py` has a doubled assignment `cap = cap = int(config.get("runs.max_concurrent") or 1)` in both `start_run` and the job worker; harmless, tidy it. (The setting's built-in default is 2; this machine runs 12.)
- `usage._active` is a module-level dict updated by `bind_run`; with 12 concurrent runs it can mis-attribute model calls when the context variable is missing. The context variable is the real source.
- `/inquire` resets the audience with `set_audience.__globals__["_AUDIENCE"].reset(token)`; expose a proper reset helper.
- `main.py` is large and mixes RAG/chat with app setup. (The lazy Neo4j client mentioned in the original note has since been removed with the rest of Neo4j.)
- `get_agent` builds the model with `os.getenv("COLAB_API_URL")` while the source of truth is the `model.colab_api_url` setting; confirm the env bridge is always in sync after a hot apply.
