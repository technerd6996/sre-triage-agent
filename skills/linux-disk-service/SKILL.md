---
name: linux-disk-service
description: Linux server service down, failing, or stopped (nginx, docker, postgres, sshd), or disk space issues.
systems: [linux]
risk: medium
keywords: [service, systemd, failed, stopped, inactive, nginx, docker, postgres, sshd, systemctl, disk, space, full, not running]
allowed_tools: [check_reachability, check_disk_usage, clear_temp_files, manage_service, add_work_note, resolve_incident, escalate_incident]
---

# Linux service and disk management

## Diagnose
1. Call claim_incident.
2. Call check_reachability with target "linux". If reachable is false, add_work_note and escalate_incident. Stop.
3. If a service is down or not running (such as nginx), call manage_service with target "linux", service_name "nginx", and action "status". Add the result to add_work_note.

## Fix (needs approval)
1. If the service status is inactive or stopped, call manage_service with target "linux", service_name "nginx", and action "start". The engineer must approve this action.
2. If the engineer rejects the approval, add_work_note and escalate_incident. Do not retry.

## Verify
1. Call manage_service with target "linux", service_name "nginx", and action "status" to confirm the service is active.
2. If active, call resolve_incident with close notes.
3. If still not running, add_work_note and escalate_incident.

## Close notes template
Service nginx on linux was inactive. Started service successfully after approval. Current status is active.