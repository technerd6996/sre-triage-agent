# TRIAGE-1 Licensing Guide

This guide explains how TRIAGE-1 is licensed, how your licence key is created and delivered, and what happens as your usage grows.

## 1. Plans

Each plan sets three limits and a group of features.

| | **Starter** | **Team** | **Commander** |
|---|---|---|---|
| Managed servers | 25 | 100 | Unlimited |
| Approver seats | 2 | 10 | 50 |
| Automatic tickets per month | 200 | 1,500 | 10,000 |
| Automatic triage of the L1 queue | Yes | Yes | Yes |
| Read-only checks from the approver chat | Yes | Yes | Yes |
| Changes from the approver chat (service stop and start, startup type, session log-off, scheduled jobs, Linux packages) | | Yes | Yes |
| Owner approval by email (reboot, shutdown, package removal, Windows update install) | | | Yes |
| Telemetry export and alerts | | | Yes |

**What is counted**
- **Managed servers:** the servers in TRIAGE-1's host list, from your CMDB, AWS or a list you maintain.
- **Approver seats:** active people whose role lets them act in the approver chat or approve actions. Read-only users, auditors and service accounts are not counted.
- **Automatic tickets:** tickets that TRIAGE-1 picks up from the L1 queue by itself in a calendar month. Work started by a person from the chat is not counted.

Custom terms are possible. Your key carries its own limits and features, so a tailored agreement needs no special software build.

## 2. One ticketing tool per licence

Each key is issued for one ticketing tool:

- ServiceNow
- Jira Service Management
- BMC Helix / Remedy
- ManageEngine ServiceDesk Plus
- Freshservice

The installation connects only to the tool named in its key. To change tools, ask for a replacement key.

## 3. How your key is created

1. When your order is confirmed, the vendor records your organisation name, plan, ticketing tool and term. Any custom limits are recorded too.
2. The vendor's licensing system writes these details into a small licence record. It adds a unique licence ID, the issue date and the expiry date.
3. The record is **digitally signed** with the vendor's private signing key, using Ed25519 public-key signatures. The private key never leaves the vendor's secure environment and is never shipped with the product.
4. The signed record becomes a single line of text that starts with `TRIAGE1-`. This line is your licence key.

Every copy of TRIAGE-1 contains the vendor's matching **public** key. The public key can check a signature but cannot create one. As a result:

- A key cannot be made up, because creating a valid signature needs the vendor's private key.
- A key cannot be edited. Changing any character, such as a limit, the plan or the expiry date, breaks the signature, and TRIAGE-1 rejects the key.

## 4. How your key is delivered

- The key is sent to the licence contact named on your order, as text in an email or through your account representative.
- It is a single line of text with no file attached and no activation server involved.
- Treat it as confidential. It identifies your organisation, and your agreement allows it to be used only on your own installations.
- Renewals, upgrades, changes to limits and changes of ticketing tool are all handled the same way: a new key replaces the old one.

## 5. Entering the key

1. Sign in as an administrator and open **Admin > Licence**.
2. Paste the key and save. TRIAGE-1 checks it immediately and shows the plan, customer, ticketing tool, limits and current usage.
3. Use **Check licence** at any time to see a summary.

The check runs **entirely on your server**:

- No internet connection is needed.
- TRIAGE-1 never contacts the vendor.
- No usage data is sent anywhere.

This allows installation in isolated and air-gapped networks.

Without a key, TRIAGE-1 runs with the Starter limits and no paid features. This is useful for evaluation.

## 6. Growing past your plan

Limits are **soft**, so your operations never stop suddenly.

| Situation | What happens |
|---|---|
| Usage reaches 80% of a limit | The admin panel shows a warning. |
| Expiry is less than 30 days away | The admin panel shows a warning. |
| A limit is exceeded, or the licence expires | A **14-day grace period** starts, and the admin panel shows the end date. Everything keeps working. |
| The grace period ends while still over a limit | **Only new automatic tickets pause.** People can still sign in, use the chat, run checks, approve actions and finish work already in progress. |
| Usage drops below the limit, or a new key is entered | The grace period clears straight away and automatic work resumes. |

Features outside your plan are switched off rather than limited. If someone tries to use one, they see a plain message saying it is not included in the current plan.

## 7. Renewal and changes

- **Renewal:** ask for a renewal key before the expiry date and paste it in. No reinstallation or restart is needed.
- **Upgrade:** a key for the higher plan unlocks its features as soon as you save it.
- **Additional installations,** such as a test environment, are covered by the terms of your agreement. Ask for a separate key if your agreement requires one.

## 8. Questions

| Question | Answer |
|---|---|
| Does TRIAGE-1 send usage data to the vendor? | No. Usage is counted and shown only on your server. |
| What if our server has no internet access? | The licence works fully offline. |
| What happens if the key is lost? | It is kept in TRIAGE-1's settings, which are included in your encrypted backups. The vendor can also send it again. |
| Can we move to a different server? | Yes. Restore a backup on the new server, or paste the same key there and stop using the old server. |
