# nginx returns 502 Bad Gateway

## When to use
Users or monitoring report HTTP 502 or "bad gateway" from a site served through nginx. nginx is running, but the application behind it (the upstream) did not answer correctly.

## Likely causes
- The upstream application or its service is stopped, crashed or restarting.
- The upstream is overloaded and refusing or timing out connections (high CPU, memory or too many connections).
- nginx points at the wrong upstream address or port after a change.
- A full disk stops the upstream from writing logs or temp files.

## Checks
1. Read the nginx error log (usually /var/log/nginx/error.log). Lines such as "connect() failed (111: Connection refused) while connecting to upstream" mean the upstream is down. "upstream timed out" means it is up but too slow.
2. Check the upstream service status on the same server (for example the application service name).
3. Check CPU, memory and disk on the server. In the TRIAGE-1 chat you can ask: "CPU, memory and disk on <server>".
4. Look at recent changes: a deployment, a config edit or a certificate change in the last day.

## Fix
1. If the upstream service is stopped or crashed, restart it. In the TRIAGE-1 chat: "restart <service name> on <server>". Confirm that it came back active.
2. If the server is out of disk space, free space first (clear temp files, clean old logs), then restart the upstream.
3. If nginx points at the wrong upstream, correct the upstream address in the nginx config, test it with `nginx -t`, then reload nginx. Do not restart nginx unless a reload is not enough.
4. Request the page again and check the nginx error log for new errors.

## Escalate when
- The upstream restarts but fails again within 15 minutes.
- Errors continue and the upstream log shows an application fault. Hand over to the application team with the log lines.
