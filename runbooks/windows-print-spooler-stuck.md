# Windows Print Spooler stopped or jobs stuck

## When to use
Users cannot print, print jobs stay in the queue, or the Print Spooler service is stopped or not responding on a Windows server.

## Likely causes
- The Print Spooler service (service name: Spooler) stopped or crashed.
- A corrupt print job blocks the queue.
- A faulty printer driver crashes the spooler repeatedly.
- The system drive is full, so the spooler cannot write its spool files.

## Checks
1. Check the service status of Spooler on the server. In the TRIAGE-1 chat: ask for the status, or "restart the print spooler service".
2. Check free disk space on C: (spool files live under C:\Windows\System32\spool\PRINTERS). Ask the chat: "C drive utilization on <server>".
3. In Event Viewer, Windows Logs, System, look for Print Spooler errors (Service Control Manager events 7031 or 7034) around the time of the problem.

## Fix
1. Restart the Print Spooler service. In the TRIAGE-1 chat: "restart the print spooler service on <server>". Confirm that the status is running afterwards.
2. If jobs are still stuck, stop the service, delete the files in C:\Windows\System32\spool\PRINTERS, and start the service again. Tell the users that queued jobs must be sent again.
3. If the disk is nearly full, free space first (ask the chat: "free up space on <server>"), then restart the spooler.
4. Ask a user to print a test page.

## Escalate when
- The spooler stops again within an hour of a restart. A faulty driver is likely: hand over to the desktop or print team with the Event Viewer error and the printer driver name.
