---
name: snow-incident-lifecycle
description: Handle a ServiceNow incident end to end - intake, move to In Progress, diagnose, fix, verify, resolve.
systems: [servicenow]
risk: low
allowed_tools: [create_incident, get_incident, set_wip, add_work_note, resolve_incident]
---

# ServiceNow incident lifecycle

1. Confirm the incident number and read its short description and affected host.
2. Set the state to In Progress (WIP) before touching any server.
3. Pick the matching technical skill (e.g. linux-disk-full) and run its Diagnose steps.
4. Add a work note summarising findings.
5. Apply the fix (approval required for medium/high risk).
6. Verify the fix. If it fails, roll back and escalate instead of resolving.
7. Resolve the incident with a close code and close notes describing root cause and fix.