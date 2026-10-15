---
name: windows-disk-full
description: Windows server disk space low or full (C: drive, no space left).
systems: [windows]
risk: medium
keywords: [disk, space, full, c:, storage, drive, low disk, no space]
allowed_tools: [check_reachability, check_disk_usage, clear_temp_files, add_work_note, resolve_incident, escalate_incident]
---

# Windows disk full

Threshold: 30 percent used. This is a lab value so the real C: (34 percent) triggers the fix path. Production would use 85.

## Diagnose (read-only)
1. check_reachability with target "windows". If not reachable, escalate_incident saying the server is unreachable, and stop.
2. check_disk_usage with target "windows". Note every disk with used_pct above the threshold.
3. If no disk is above the threshold, add_work_note "no disk issue found", then resolve_incident saying usage is within limits.

## Fix (needs approval)
1. clear_temp_files with target "windows". The engineer must approve this call.
2. If the engineer rejects it, add_work_note and escalate_incident. Do not retry.

## Verify
1. check_disk_usage again and compare used_pct before and after.
2. If used_pct is now below the threshold, resolve_incident.
3. If not, add_work_note with the before and after numbers, then escalate_incident.

## Close notes template
Disk <drive> on windows was <before>% used. Cleared temp files older than 1 day (<freed_mb> MB freed). Now <after>% used.