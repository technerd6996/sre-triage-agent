---
name: windows-disk-full
description: Windows server disk space low or full (C: drive, no space left).
systems: [windows]
risk: medium
keywords: [disk, space, full, storage, low disk, no space, c]
allowed_tools: [check_reachability, check_disk_usage, clear_temp_files, add_work_note, resolve_incident, escalate_incident]
verify: check_disk_usage(target) x1 expect=under_threshold
---

# Windows disk full

The threshold is set in code (DISK_THRESHOLD_PCT), not here.

## Steps
1. check_reachability with target "windows". If reachable is false, follow its next_step. Stop.
2. check_disk_usage with target "windows". Follow its next_step.
3. If next_step says to call clear_temp_files, call it with target "windows". The engineer must approve this call.
4. If the engineer rejects it, call add_work_note and escalate_incident. Do not retry.
5. After clear_temp_files, call check_disk_usage with target "windows" again and follow its next_step.

## Close notes template
Disk <drive> on windows was <before>% used. Cleared temp files older than 1 day (<freed_mb> MB freed). Now <after>% used.