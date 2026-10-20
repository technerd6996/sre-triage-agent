---
name: linux-service-down
description: Linux service stopped, inactive or failing (nginx, docker, postgres, sshd, apache, redis).
systems: [linux]
risk: medium
keywords: [service, systemd, systemctl, nginx, docker, postgres, postgresql, sshd, apache, httpd, mysql, redis, inactive, stopped, not running, failed, down]
allowed_tools: [check_reachability, check_service_status, start_service, add_work_note, resolve_incident, escalate_incident]
verify: check_service_status(service) x2 soak=VERIFY_SOAK_SECONDS expect=active
---

# Linux service down

## Steps
1. Find the service name in the ticket (for example nginx, docker, postgresql). If the ticket names no service, call add_work_note and escalate_incident. Stop.
2. check_reachability with target "linux". If reachable is false, follow its next_step. Stop.
3. check_service_status with target "linux" and the service name. Follow its next_step.
4. If next_step says to call start_service, call it. The engineer must approve this call.
5. If the engineer rejects it, call add_work_note and escalate_incident. Do not retry.
6. After start_service, follow its next_step.

## Close notes template
Service <name> on linux was <status before>. Started after approval. Status now: <status after>.