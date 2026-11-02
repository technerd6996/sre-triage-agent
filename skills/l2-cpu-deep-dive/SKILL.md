---
name: l2-cpu-deep-dive
description: L2 engineer chat. Investigate high CPU or load on a server, then fix it when the engineer says so.
audience: l2
systems: [linux, windows]
risk: high
keywords: [cpu, high cpu, load average, high load, cpu spike, cpu utilization, runaway process, investigate cpu, cpu deep dive]
allowed_tools: [check_cpu_usage, top_processes, get_service_logs, check_memory_usage, restart_service, kill_process, add_work_note]
---

# L2 CPU deep dive

You are talking to an L2 engineer. Investigate first, then wait for the engineer to choose the fix.

## Investigate (read-only, run these without asking)
1. check_cpu_usage with the server's target. Note the load average on linux.
2. top_processes with sort_by "cpu".
3. If top_processes names a service, get_service_logs for that service.
4. If one process uses most of the CPU, also run check_memory_usage, because a process that is swapping looks like a CPU problem.
5. Give the engineer a short summary: CPU percent, load per core, the top 3 processes, key log lines. Say what you recommend and why. Stop and wait.

## Fix (only when the engineer asks)
6. To restart a service: restart_service. To stop one process: kill_process with the PID from top_processes and force false. Each needs the engineer's approval.
7. After any fix, call check_cpu_usage again and report before and after.
8. If CPU is still high, say so and suggest the next option. Do not try another fix on your own.
9. If the engineer gave an incident number, call add_work_note with what was found and done.

## Rules
- One fix at a time. Never use force true unless the engineer asks for it after a normal stop failed.
- A short spike is not a fault. If CPU is already under the limit, say so and do not change anything.
