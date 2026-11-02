---
name: l2-kill-process
description: L2 engineer chat. Stop a runaway or stuck process by PID or name, gracefully first.
audience: l2
systems: [linux, windows]
risk: high
keywords: [kill, kill process, stop process, terminate, runaway, stuck process, hung process, pid, end task]
allowed_tools: [top_processes, kill_process, check_cpu_usage, check_memory_usage, add_work_note]
---

# L2 kill process

## Steps
1. If the engineer gave a PID, use it. If they gave a name, call top_processes (sort_by "cpu" for CPU problems, "memory" otherwise) and find that process. If you cannot find exactly one match, show the table and ask which PID. Stop.
2. Tell the engineer the process name, PID and what it is using, and that you will ask it to exit normally first.
3. Call kill_process with force false. The engineer must approve this call.
4. If the result says the process has stopped, run check_cpu_usage or check_memory_usage (whichever problem the engineer described) and report before and after.
5. If it is still running, tell the engineer and ask whether to force it. Only call kill_process with force true if they say yes. It needs approval again.
6. If the engineer gave an incident number, call add_work_note with what was stopped and why.

## Rules
- Never guess a PID. Core system processes are refused by the tool: report that and stop.
- If the process belongs to a service that should keep running, mention that the service may restart it, and offer l2-restart-service instead.
