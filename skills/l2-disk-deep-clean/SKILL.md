---
name: l2-disk-deep-clean
description: L2 engineer chat. Find what is filling a disk and delete old rotated logs when the temp cleanup was not enough.
audience: l2
systems: [linux, windows]
risk: high
keywords: [disk, disk full, logs, log cleanup, old logs, large files, free space, clean logs, deep clean, space]
allowed_tools: [find_large_files, clean_old_logs, add_work_note]
---

# L2 disk deep clean

## Steps
1. Choose the folder to inspect. Use the one the engineer names. If they name none, use /var/log on linux and C:\inetpub\logs\LogFiles on windows.
2. find_large_files with that target and path. Show the engineer the five biggest items and the disk percent used.
3. Recommend one cleanup: delete rotated or old logs older than N days in that folder. Suggest 14 days unless the engineer says otherwise. Explain what stays (live log files on linux, anything newer than N days). Stop and wait.
4. When the engineer agrees, call clean_old_logs with the same path and the days. The engineer must approve this call.
5. Report how many files or MB were removed and the disk percent before and after.
6. If the disk is still full, say what is left (call find_large_files again) and suggest options such as a bigger disk or moving data. Do not delete anything else on your own.
7. If the engineer gave an incident number, call add_work_note with the before and after.

## Rules
- Only the folders the tools allow can be used. If a path is refused, tell the engineer and ask for an allowed one.
- Never delete a second time without asking.
