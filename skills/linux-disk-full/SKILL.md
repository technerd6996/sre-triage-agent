---
name: linux-disk-full
description: Linux server disk space low or full.
systems: [linux]
risk: medium
keywords: [disk, space, full, no space, storage, filesystem, df, partition, low disk]
allowed_tools: [check_reachability, check_disk_usage, clear_temp_files, add_work_note, resolve_incident, escalate_incident]
---

# Linux disk full

## Steps
1. check_reachability with target "linux". If reachable is false, follow its next_step. Stop.
2. check_disk_usage with target "linux". Follow its next_step.
3. If next_step says to call clear_temp_files, call it with target "linux". The engineer must approve this call.
4. If the engineer rejects it, call add_work_note and escalate_incident. Do not retry.
5. After clear_temp_files, call check_disk_usage with target "linux" again and follow its next_step.

## Close notes template
Disk on linux was <before>% used. Cleared temp files older than 1 day. Now <after>% used.