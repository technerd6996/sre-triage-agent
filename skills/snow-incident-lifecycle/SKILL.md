---
name: snow-incident-lifecycle
description: Handle a ServiceNow incident end to end - claim, diagnose, fix, verify, resolve or escalate.
systems: [servicenow]
risk: low
allowed_tools: [get_incident, claim_incident, add_work_note, resolve_incident, escalate_incident]
---

# ServiceNow incident lifecycle

1. Call claim_incident with the ticket number. This assigns the ticket to you and moves it to In Progress.
2. Call get_incident and read the short description and description to find the affected server.
3. Follow the technical skill below. Use only the tools it lists.
4. After each important check or action, call add_work_note with what you found (numbers, not guesses).
5. If the fix is verified, call resolve_incident with close notes: root cause, action taken, result.
6. If you cannot fix it, the fix did not verify, or the ticket is outside L1, call escalate_incident with a clear reason. Never resolve a ticket whose fix was not verified.