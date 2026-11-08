# Disk space low or disk full

## When to use
An alert or user report says a disk is almost full (above 85 percent), or an application fails with "no space left on device" or "not enough disk space". Applies to Windows (C:, D:) and Linux (/, /var).

## Likely causes
- Old log files that were never rotated or cleaned.
- Temp files left behind by applications or installers.
- A runaway application writing large dumps or logs.
- Backups or downloads stored on the system drive.

## Checks
1. Confirm the usage. In the TRIAGE-1 chat: "What is the C drive utilization on <server>".
2. Find what is using the space. On Windows, check the largest folders under C:\Windows\Temp, C:\inetpub\logs and C:\Users\*\AppData\Local\Temp. On Linux, run `du -xh /var --max-depth=2 | sort -h | tail`.
3. Check whether usage is growing fast (run the check twice, 10 minutes apart). A fast climb means something is writing right now.

## Fix
1. Clear temp files older than one day. In the TRIAGE-1 chat: "free up space on <server>".
2. Clean old log files in an allowed log folder. In the chat: "clean old logs in <folder> older than 14 days". Only folders on the administrator's allowed list are accepted.
3. Check the usage again. The target is below 85 percent.
4. Do not delete anything else by hand without the owner's approval: databases, backups and user data are not covered by this runbook.

## Escalate when
- Space is still above 85 percent after the cleanup. The server owner must add capacity or archive data.
- Usage keeps climbing after cleanup. Find the process that is writing and hand over to the application owner.
