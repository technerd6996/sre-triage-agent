---
name: windows-high-memory
description: Windows server memory high or low available memory (for example Zabbix: High memory utilization).
systems: [windows]
risk: medium
keywords: [memory, ram, out of memory, high memory, memory utilization, memory usage, available memory, lack of available memory, memory leak, memory full, paging]
allowed_tools: [check_reachability, check_memory_usage, top_processes, restart_service, add_work_note, resolve_incident, escalate_incident]
verify: check_memory_usage(target) x1 expect=under_threshold
---

# Windows high memory

The threshold is set in code (MEMORY_THRESHOLD_PCT), not here.

## Steps
1. check_reachability with target "windows". If reachable is false, follow its next_step. Stop.
2. check_memory_usage with target "windows". Follow its next_step.
3. If next_step says to call top_processes, call it with target "windows" and sort_by "memory". Follow its next_step.
4. If next_step says to call restart_service, call it with the target and service_name it gives. The engineer must approve this call. Never restart any other service.
5. If the engineer rejects it, call add_work_note and escalate_incident. Do not retry.
6. After restart_service, call check_memory_usage with target "windows" again and follow its next_step.
7. Restart at most once. If memory is still high after one restart, escalate.

## Close notes template
Memory on windows was <before>% used. Top consumer: <process> (<share>%). Restarted <service> after approval. Now <after>% used.
