# High CPU usage on a server

## When to use
A server is slow and monitoring or a user reports CPU above 85 percent for more than a few minutes.

## Likely causes
- One process stuck in a loop or processing a large job.
- Antivirus or backup scanning during business hours.
- A traffic spike or too many concurrent requests.
- A recent deployment or configuration change.

## Checks
1. Confirm the load. In the TRIAGE-1 chat: "What is the CPU utilization on <server>".
2. Find the process using the most CPU. In the chat: "Which process is consuming the CPU on <server>". The reply lists the top five with CPU and memory.
3. Decide whether the top process is safe to touch: a system process (for example System, lsass, svchost) is not; an application or scheduled job usually is.
4. Check whether the load started after a change or at a regular time (a scheduled scan or backup).

## Fix
1. If the top process is an application service, restart that service. In the chat: "restart <service name> on <server>". Check the CPU again after two minutes.
2. If it is a scheduled scan or backup, let it finish or ask its owner to move it out of business hours.
3. Do not stop a process by its number from a runbook. Stopping a process needs the server owners' approval.
4. Add what you found (process, percentage, action taken) to the ticket.

## Escalate when
- CPU stays above 85 percent after the restart.
- The top process is a system process or an unknown one. Hand over to the server owner with the process list.
