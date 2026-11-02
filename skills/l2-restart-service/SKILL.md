---
name: l2-restart-service
description: L2 engineer chat. Restart a named service (stop and start) and confirm it came back, for example restart nginx or restart tomcat.
audience: l2
systems: [linux, windows]
risk: high
keywords: [restart, restart service, bounce, recycle, restart nginx, restart tomcat, restart iis, restart w3svc]
allowed_tools: [get_service_logs, restart_service, check_service_status, add_work_note]
---

# L2 restart service

## Steps
1. Find the service name in the engineer's message. If there is none, ask for it and stop. On windows, convert the common names: IIS = W3SVC, Print Spooler = Spooler, DNS Server = DNS, Windows Update = wuauserv, Remote Desktop = TermService.
2. get_service_logs for that service, so the engineer sees recent warnings and errors before the restart. Show the key lines in two or three sentences.
3. Call restart_service with the target and service name. The engineer must approve this call.
4. Read the status in the result. If it is active, tell the engineer the service is back.
5. If it is not active, call get_service_logs again, show what it says, and stop. Do not retry.
6. If the engineer gave an incident number, call add_work_note with the before and after status.

## Rules
- Restart one service per request. If the engineer asks for several, do them one at a time and wait for each approval.
