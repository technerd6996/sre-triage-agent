---
name: aws-server-unreachable
description: Server down, unreachable, not responding, connection timeout, cannot connect (RDP, SSH, WinRM).
systems: [windows, linux]
risk: low
keywords: [unreachable, down, not responding, cannot connect, can't connect, timeout, timed out, offline, rdp, ssh, ping]
allowed_tools: [check_ec2_status, check_security_group, get_cloudwatch_metric, check_reachability, add_work_note, escalate_incident, escalate_l3]
---

# AWS server unreachable

This skill is read-only. Never resolve the ticket: every outcome ends in escalation.

## Diagnose (read-only)
1. check_ec2_status for the server's platform. Note state, system_status and instance_status.
2. If state is not "running" (stopped, stopping): add_work_note with the state, then escalate_incident saying the instance is not running and needs a start approved by L2. Stop.
3. If state is "running" but system_status or instance_status is not "ok": this is an infrastructure failure. Call escalate_l3 with number and a reason containing the exact status values, then escalate_incident. Stop.
4. check_security_group with port 5985 for windows or 22 for linux. If allowed is false: add_work_note naming the port and the open_inbound_ports, then escalate_incident asking L2 to approve a security group change. Stop.
5. check_reachability for the server's platform. If reachable is true, the server is up: add_work_note with the result and escalate_incident saying the problem is not network-level.
6. If not reachable but steps 1 to 4 were clean, get_cloudwatch_metric with CPUUtilization. Add the average and max values to add_work_note, then escalate_incident.

## Close notes template
State: <state/status>. Port <n>: <allowed/blocked>. Reachable: <yes/no>. CPU max: <n>%. Escalated because: <reason>.