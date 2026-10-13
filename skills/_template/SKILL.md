---
name: skill-name
description: One line. When should the agent pick this skill?
systems: [linux]            # linux | windows | servicenow
risk: low                   # low = read-only, medium = changes a service, high = destructive
allowed_tools: []           # tools this skill may call
---

# Skill title

## When to use
Symptoms / alert text that should trigger this skill.

## Diagnose (read-only)
1. Step one
2. Step two

## Fix (needs approval if risk >= medium)
1. Step one

## Verify
How to confirm the issue is actually fixed.

## Rollback
How to undo the fix if verification fails.

## Close notes template
What to write in the ServiceNow work notes / close notes.