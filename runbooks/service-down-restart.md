# A service is down: check, restart safely, verify

## When to use
A named service (Windows service or Linux systemd unit) is reported stopped, failed or not responding, and nothing larger (the whole server unreachable) is going on.

## Likely causes
- The service crashed or was stopped by a deployment or patching.
- A dependency (database, another service) was down at start time.
- The server ran out of disk space or memory.

## Checks
1. Check the server is reachable and the service state. In the TRIAGE-1 chat: ask for the status of the service on the server.
2. Check disk and memory on the server first. A full disk or no free memory will make the service fail again after a restart. In the chat: "CPU, memory and disk on <server>".
3. Read the recent service log lines for the reason it stopped, if you can.

## Fix
1. If the service is stopped, start it. If it is running but not responding, restart it. In the chat: "restart <service name> on <server>" or "start <service name> on <server>".
2. Confirm the status is active. Wait two minutes and check again: a service that dies again is not fixed.
3. Record in the ticket: service name, status before and after, and the time.
4. Do not restart the same service over and over: the tools limit repeated restarts within an hour on purpose.

## Escalate when
- The service does not come back, or stops again within the hour.
- The log shows a fault in the application itself or a missing dependency. Hand over to the application owner with the log lines.
