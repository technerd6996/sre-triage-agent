# TRIAGE-1: configuration action items

Everything below is for **you** to set up. None of it was changed by the build. Do the items in order. Each item says where to do it and how to check it worked. Items 1 to 5 are needed before the owner-approval actions (reboot, shutdown, package removal, Windows update install) can work at all.

## Status after the assistant ran everything it could (2026-11-09, 23:40)

| Item | State |
|---|---|
| 1 Restart the app | Done by the assistant (see the final report) |
| 2 Owner mail transport | **Done and verified (2026-11-09)**: SES accepts mail from `escalations_turtle@outlook.com` in ap-south-2 (`python -m scripts.test_ses` prints OK). P1 alerts stay on SNS. **Still to check by you**: whether the SES account is in sandbox (SES console > Account dashboard). In sandbox, each owner mailbox must also be a verified identity |
| 3 Owner emails | **Done for the three test servers (2026-11-10)**: `owned_by` = VedaVidyadaran S on term-win-srv, victim-test and zabbix-mon; owner mail resolves to the email on that user's **profile** (`vedavidyadaran@outlook.com`). TRIAGE-1 now matches your ServiceNow user by username when the profile email differs from your login, and copies you on owner mail using the profile email. For real owners: set `owned_by` or `managed_by` to users whose profile has a deliverable email (no CMDB field needed). The optional `u_owner_email` field is still not created |
| 4 `zabbix-mon` credential | **Needs you**: the secret is not available to the assistant |
| 5 Publish the six runbooks | **Needs you**: the integration user cannot publish |
| 6 Linux sudoers | **Needs you**. Finding: `ec2-user` on `victim-test` has `(ALL) NOPASSWD: ALL`, full unrestricted root |
| 7 Windows admin rights | Nothing to change that was found; first patch install still untested |
| 8, 9 Protected servers, reply wording | **Needs your decision** (which servers are critical) |
| 10 Run slots | **Done**: `runs.max_concurrent` = 5, `runs.reserved_slots` = 1 |
| 11 Test run | Read-only checks and one real L1 action (start `sysstat-collect` on `victim-test`, ticket INC0010705, resolved) **done**. Owner approval, mail, reboot, package and patch tests need items 2 and 3 first |
| 12 Housekeeping | Backup script written and one backup taken; **you** must schedule it (see item 12). Git commit done by the assistant. Old tickets INC0010687 and INC0010689 were already closed |

Other findings: there are about 85 agent runs stuck in *awaiting approval* for more than a day (INC0010410 to INC0010584 and others). Nobody has approved or rejected them on the Agent Console. They are harmless but they will keep showing as pending.

Today's findings that drive this list:
- The mail transport is **SNS**, which cannot address individual owners.
- No server in the CMDB has an owner email yet (`term-win-srv`, `victim-test` checked).
- `zabbix-mon` has no stored credential, so every check on it fails.
- The six runbook articles are **Draft**, so the chat cannot use them.
- `term-win-srv` is the machine TRIAGE-1 runs on, so it can never be rebooted or shut down from the chat.

---

## A. Blocking items

### 1. Restart the app and refresh the page
- Stop the app, then run: `powershell -ExecutionPolicy Bypass -File scripts\start.ps1`
- In the browser press **Ctrl+F5**.
- Why: the new code, and the new `owner_requests` table (the start script applies `db/schema.sql`).
- Check: the page source loads `script.js?v=30`; Admin > Configuration shows the new `owners.*` and `chat.*` settings.

### 2. Give owner mail a transport that can address people (SES or SMTP)
SNS publishes to one fixed topic and ignores To and Cc, so owners would never get the request or the notices. **Do not switch the main Notice transport**: your P1 alerts use it. Use the new, separate setting instead.

- Where: Admin > Configuration > **Notifications** > **Mail transport for owner approval** (`owners.mail_transport`). It is `same` today; choose `ses` or `smtp`.
- **Option A, SES** (configured: region `ap-south-2`, sender `escalations_turtle@outlook.com`; the owner transport is **already set to `ses`**):
  1. **You must verify the sender.** In the AWS console, region **ap-south-2**: SES > Verified identities > Create identity > Email address > `escalations_turtle@outlook.com`. AWS sends a verification mail to that Outlook mailbox: click its link (valid 24 hours). The IAM user `agent-traige` cannot do this itself (it may send, but not verify identities), and the assistant did not try other credentials.
  2. Check it: `python -m scripts.test_ses` from the repo root. It sends one message to Amazon's mailbox simulator, so nobody receives mail. It prints `OK` when the sender and the send permission both work. Today it prints `Email address is not verified`; that is the only thing missing.
  3. A new SES account is in **sandbox**: even after step 1 it can only mail addresses you have verified. Verify each owner mailbox as an SES identity too, or request production access (SES console > Account dashboard).
  4. Optional tidy-up: the IAM policy for `agent-traige` only needs `ses:SendEmail`.
- **Option B, SMTP**: fill `notifications.smtp_host`, `smtp_port`, `smtp_user`, `smtp_password`, `smtp_from`, then set the owner transport to `smtp`.
- Check: the Notifications **Test** button sends nothing. The real check is item 11, step 3. Until this is set, the chat refuses owner-approval actions and says why.

### 3. Add the owner email field in ServiceNow and fill in owners
Do this on the server CI table (`cmdb_ci_server`; if your servers live in another table, set `CMDB_HOST_TABLE`).

| Field | Type | Action |
|---|---|---|
| `u_owner_email` | String, length 255 | **Create it.** System Definition > Tables > `cmdb_ci_server` > New column. Label "Owner email", name `u_owner_email`. Add it to the form. Fill it with one or more shared mailbox addresses, separated by `,` or `;`. |
| `owned_by` | Reference to User | Fill for every server. The **user record's Email must be filled in**. |
| `managed_by` | Reference to User | Optional, also mailed. Same rule. |
| `support_group` | Reference to Group | Optional, also mailed. The **group record's Email must be filled in**. |
| `u_maintenance_window` | String | Optional, not used yet. |

- Permissions: the integration user (`svc_triage_01`) must be able to **read** those fields on the server table, plus `email` on `sys_user` and on `sys_user_group`.
- If your field names differ, change them under Admin > Notifications: `owners.email_field`, `owners.user_fields`, `owners.group_fields`.
- Check: in the chat say `install htop on victim-test`. The card must list "these owners are mailed: ..." and no longer say "No owner email was found".

### 4. Add the missing credential for `zabbix-mon`
- Add its credential to the credential store the same way as `victim-test` and `term-win-srv`, under its host id (`i-0d4474a746ead34fa`) or its address (`16.112.111.144`).
- Check: ask `uptime of zabbix-mon`. It should answer instead of "No credential available".

### 5. Publish the runbook articles
- In ServiceNow open the knowledge base **SRE Runbooks**. Review and publish KB0010046 to KB0010051 (AD lockout, low disk, high CPU, nginx 502, service down, print spooler).
- Optional: make `svc_triage_01` a **manager** of that knowledge base so future pushes can publish. Keeping a person as the publisher is the safer choice.
- Check: Admin > Knowledge source > **Test** says "6 published articles readable". Then ask the chat "the website shows bad gateway".

---

## B. Server side permissions

### 6. Linux: narrow passwordless sudo for the service account
The chat only ever runs fixed command templates with `sudo -n`. Give the service account passwordless sudo for **exactly these commands and nothing else**. Never `ALL`, never a shell, never `su`, `visudo`, `chmod`, `usermod` or `passwd`.

Commands the chat uses with sudo: `systemctl` (stop, start, restart, enable, disable, start --no-block), `journalctl`, `loginctl terminate-session`, `shutdown`, `apt-get` or `dnf`/`yum` (install, upgrade, remove, and the dry runs), `kill`, `find` (log cleanup).

Example drop-in, to be reviewed by your security team before use (file `/etc/sudoers.d/triage1`, check it with `visudo -c`):

```
Cmnd_Alias TRIAGE_SVC  = /usr/bin/systemctl stop *, /usr/bin/systemctl start *, /usr/bin/systemctl restart *, \
                         /usr/bin/systemctl enable *, /usr/bin/systemctl disable *, /usr/bin/systemctl start --no-block *
Cmnd_Alias TRIAGE_LOGS = /usr/bin/journalctl *
Cmnd_Alias TRIAGE_SESS = /usr/bin/loginctl terminate-session *
Cmnd_Alias TRIAGE_PWR  = /usr/sbin/shutdown -r *, /usr/sbin/shutdown -h *
Cmnd_Alias TRIAGE_PKG  = /usr/bin/dnf -y install *, /usr/bin/dnf -y upgrade *, /usr/bin/dnf -y remove *, /usr/bin/dnf --assumeno *
svc_triage ALL=(root) NOPASSWD: TRIAGE_SVC, TRIAGE_LOGS, TRIAGE_SESS, TRIAGE_PWR, TRIAGE_PKG
```
(Use `apt-get` lines instead of `dnf` on Debian or Ubuntu. Replace `svc_triage` with your service account.) Wildcards are wide on purpose here, because the app validates the arguments and keeps a deny list; narrow them further if your policy requires it.

### 7. Windows: the service account
- It must be a **local Administrator** on each Windows server (WinRM, stopping services, logging off sessions, `shutdown`, and registering the one-time SYSTEM task for updates).
- The **Task Scheduler** and **Windows Update** services must not be disabled. Group Policy or antivirus must allow a short script in `C:\ProgramData\triage`.
- Windows Update installs have never been run from the chat. **Try the first one on a non-production server.**

---

## C. Admin panel settings to review (all apply at once)

### 8. Protect what must never be touched from the chat
- Admin > **Agent behaviour**: `chat.never_power_hosts`: add domain controllers, database servers and anything critical. (The TRIAGE-1 server is always protected.)
- `chat.service_protected`: add services specific to your estate. `chat.package_denylist`: review the list.
- `approver_chat.verbs` and `approver_chat.service_aliases`: review the phrases people can use.

### 9. Review owner approval wording
- Admin > **Notifications**: `owners.approve_words`, `owners.refuse_words`, `owners.condition_words` (the defaults are strict on purpose), `owners.require_sender_match` (keep **on**), `owners.approval_ttl_hours` (24), `owners.reply_scan_lines` (3).

### 10. Run slots and model slots (from the earlier request)
- Set **Tickets running at once** (`runs.max_concurrent`) to 5 and **Run slots kept for chat and ad-hoc work** (`runs.reserved_slots`) to 1. Today they read 8 and 0.
- `model.slots` is 8 and `model.reserved_slots` is 2. Keep those only if your model server really handles parallel requests.

---

## D. Prove it works, safest first

### 11. A test run in this order
1. **Read-only checks** on `victim-test`: uptime, pending reboot, ping, errors, sessions, services, packages. (Already verified live.)
2. **An L1 change** on `victim-test`: `install htop on victim-test`. Check the ticket note and that the owner mail arrived.
3. **The owner approval path, with yourself as the owner.** Temporarily put **your own mailbox** in `u_owner_email` on `victim-test`, then:
   - say `reboot victim-test because testing` and press **Email the owners**;
   - reply to that mail from your mailbox with `Approved, go ahead`;
   - paste the whole reply into the chat, check the card, press **Confirm**;
   - watch the ticket for "back after N min", then put the real owner address back.
4. Also try the refusals on purpose: reply `Not approved`, reply with `Approved if ...`, and reply from a different address. Each must be rejected.
5. **Windows updates** on a non-production server: `pending windows updates on <server>`, then `install update KB... on <server>`, then `patch job status on <server>`.

---

## E. Housekeeping

### 12. Git, backups and old items
- `CLAUDE.md` is in `.gitignore`, but it is already tracked. To stop tracking it: `git rm --cached CLAUDE.md`, then commit. Commit the rest of the work too (nothing from this build is committed).
- **Backups:** `scripts\backup.ps1` backs up the database and a copy of `bootstrap.json` to `C:\ProgramData\triage\backups` (only you and Administrators can open it; 14 days kept). One backup was taken today (4 MB, restore-listing checked). **Schedule it yourself**: open PowerShell **as Administrator** and run `powershell -ExecutionPolicy Bypass -File scripts\backup.ps1 -Install` (nightly at 02:30; the assistant was not allowed to create scheduled tasks). Then copy the backup folder **off this machine**: it contains the encryption key.
- Rotate every secret that was in the old `Creds/` folder; it is still in git history.
- ServiceNow ticket **INC0010687** is still In Progress in the L1 queue, assigned to the agent. Reassign or close it.
- The old local runbook store is kept for one release as a way back (`knowledge.source = local`). After the ServiceNow knowledge base has proven itself, remove it.

---

## Quick reference: what is blocked until which item

| You want to | Needs items |
|---|---|
| Ask questions, run read-only checks, L1 changes (stop, install packages, log off ...) | 1 |
| Owner mail after package install | 1, 2, 3 |
| Reboot, shutdown, package removal, Windows update install | 1, 2, 3 (and 6 for Linux, 7 for Windows) |
| Chat answers from your runbooks | 5 |
| Checks on `zabbix-mon` | 4 |
