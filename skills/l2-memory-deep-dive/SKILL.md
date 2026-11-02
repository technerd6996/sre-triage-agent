---
name: l2-memory-deep-dive
description: L2 engineer chat. Investigate high memory, swapping or out-of-memory kills on a server, then fix it when the engineer says so.
audience: l2
systems: [linux, windows]
risk: high
keywords: [memory, ram, swap, oom, out of memory, memory leak, high memory, investigate memory, memory deep dive]
allowed_tools: [check_memory_usage, top_processes, check_oom_events, get_service_logs, restart_service, kill_process, add_work_note]
---

# L2 memory deep dive

You are talking to an L2 engineer. Investigate first, then wait for the engineer to choose the fix.

## Investigate (read-only, run these without asking)
1. check_memory_usage with the server's target.
2. top_processes with sort_by "memory".
3. check_oom_events with the same target.
4. If top_processes names a service, get_service_logs for that service.
5. Give the engineer a short summary: memory and swap numbers, the top 3 processes, any out-of-memory kills, the key log lines. Then say what you recommend and why (restart the service, stop one process, or look at the application first). Stop and wait.

## Fix (only when the engineer asks)
6. To restart a service: restart_service. To stop one process: kill_process with the PID from top_processes and force false. Each needs the engineer's approval.
7. After any fix, call check_memory_usage again and report before and after.
8. If memory is still high, say so and suggest the next option. Do not try another fix on your own.
9. If the engineer gave an incident number, call add_work_note with what was found and done.

## Rules
- One fix at a time. Never use force true unless the engineer asks for it after a normal stop failed.
- If a process is a core system process, the tool refuses. Report that and stop.
