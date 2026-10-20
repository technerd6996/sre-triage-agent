---
name: windows-service-down
description: Windows service stopped, not running or failing (IIS, Print Spooler, DNS, Windows Update, RDP, W32Time).
systems: [windows]
risk: medium
keywords: [windows service, service, stopped, not running, failed, iis, w3svc, spooler, print spooler, dns, dhcp, wuauserv, windows update, termservice, w32time]
allowed_tools: [check_reachability, check_service_status, start_service, add_work_note, resolve_incident, escalate_incident]
verify: check_service_status(service) x2 soak=VERIFY_SOAK_SECONDS expect=active
---

# Windows service down

## Steps
1. Find the service in the ticket and convert it to its Windows service name using this table:
   IIS or web server = W3SVC; Print Spooler = Spooler; DNS Server = DNS; DHCP Server = DHCPServer;
   Windows Update = wuauserv; Remote Desktop = TermService; Windows Time = W32Time.
   If the ticket names no service, call add_work_note and escalate_incident. Stop.
2. check_reachability with target "windows". If reachable is false, follow its next_step. Stop.
3. check_service_status with target "windows" and the service name. Follow its next_step.
4. If next_step says to call start_service, call it. The engineer must approve this call.
5. If the engineer rejects it, call add_work_note and escalate_incident. Do not retry.
6. After start_service, follow its next_step.

## Close notes template
Service <name> on windows was <status before>. Started after approval. Status now: <status after>.