---
name: linux-high-cpu
description: Linux server CPU high or load average too high (for example Zabbix: High CPU utilization, Load average is too high).
systems: [linux]
risk: medium
keywords: [cpu, high cpu, cpu utilization, cpu usage, cpu load, load average, high load, cpu spike, 100% cpu, processor, cpu saturated]
allowed_tools: [check_reachability, check_cpu_usage, top_processes, restart_service, add_work_note, resolve_incident, escalate_incident]
verify: check_cpu_usage(target) x1 expect=under_threshold
---

# Linux high CPU

The threshold is set in code (CPU_THRESHOLD_PCT), not here.

## Steps
1. check_reachability with target "linux". If reachable is false, follow its next_step. Stop.
2. check_cpu_usage with target "linux". Follow its next_step.
3. If next_step says to call top_processes, call it with target "linux" and sort_by "cpu". Follow its next_step.
4. If next_step says to call restart_service, call it with the target and service_name it gives. The engineer must approve this call. Never restart any other service.
5. If the engineer rejects it, call add_work_note and escalate_incident. Do not retry.
6. After restart_service, call check_cpu_usage with target "linux" again and follow its next_step.
7. Restart at most once. If cpu is still high after one restart, escalate.

## Close notes template
CPU on linux was <before>% busy. Top consumer: <process> (<share>%). Restarted <service> after approval. Now <after>% busy.
