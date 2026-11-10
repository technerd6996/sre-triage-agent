# Credential rotation checklist

Written 2026-11-10 from `scripts/scan_history.py` (it reports where a secret is and what kind, never the value).

**Why this is needed.** A secret that was ever committed stays readable in git history after the file is deleted. This repository has a GitHub remote (`origin`), so anything in history must be treated as known to anyone who can read that repository. Deleting the file, or adding it to `.gitignore`, does not help. Only changing the secret at its source does.

**Order.** Rotate first (sections 1 and 2), then decide about history (section 3). Rotating makes the old values worthless whether or not history is cleaned.

## 1. What was found

| Where | What | In history | Rotate? |
|---|---|---|---|
| `Creds/zabb.pem` | SSH private key (PEM) | 14 commits, no longer tracked, still on disk | **Yes** |
| `Creds/zabb.ppk` | The same key in PuTTY format | 14 commits, no longer tracked, still on disk | **Yes** (same key pair) |
| `Creds/Triage_1.txt` | Notes with the default admin user name and other lines | 7 commits, no longer tracked, still on disk | Review the file; see 2.6 |
| `.env.example` (old versions) | Real-looking values, not placeholders, for the items below | 13 commits, file since removed | **Yes**, each item below |

Values in old `.env.example` that looked real (length only, the value was not read):

| Setting | Looked real | What it unlocks |
|---|---|---|
| `SNOW_PASSWORD` (100 characters) with `SNOW_USER`, `SNOW_INSTANCE` | Yes | The ServiceNow integration user: read and write incidents, CMDB, knowledge |
| `WINDOWS_WINRM_PASSWORD` (17 characters) with `WINDOWS_WINRM_USER`, `WINDOWS_HOST` | Yes | Remote PowerShell on the Windows server |
| `SUPABASE_KEY` (46 characters) with `SUPABASE_URL` | Yes | The old hosted database, if that project still exists |
| `NEO4J_PASS` (43 characters) with `NEO4J_URI`, `NEO4J_USER` | Yes | The old graph database, if that instance still exists |
| `COLAB_API_URL` | An address, not a password | The model server address. Change it only if that server accepts requests without a key |
| `LINUX_HOST`, `WINDOWS_HOST`, `SNOW_ASSIGNEE` and similar | Names and addresses | Not secrets, but they tell a reader what to attack |

Not found in history: AWS access keys, GitHub tokens, database addresses with a password, the `TRIAGE_SECRET_KEY`. The AWS user `agent-traige` was entered in the admin panel only, so it is not in the repository.

## 2. Rotate each one

Tick each step. "Verify" uses the **Test** button of that section in Admin > Configuration unless it says otherwise.

### 2.1 ServiceNow integration user (`svc_triage_01`)
- [ ] In ServiceNow: User Administration > Users > `svc_triage_01` > set a new password (or have an admin do it).
- [ ] In TRIAGE-1: Admin > Configuration > Ticketing > password > Save.
- [ ] Verify: Ticketing > Test says the queue can be read. Then ask the chat a runbook question (the knowledge search uses the same user).

### 2.2 Windows remote account (WinRM)
- [ ] On the domain or the server: reset the password of the account used for WinRM.
- [ ] In TRIAGE-1: update it where it is stored (Credentials section, or the credential store entry for that server).
- [ ] Verify: in the approver chat ask `what is the uptime of term-win-srv`.

### 2.3 Linux SSH key (`zabb.pem` / `zabb.ppk`)
- [ ] Create a new key pair (for AWS: EC2 > Key pairs, or `ssh-keygen -t ed25519`).
- [ ] On every server that trusted the old key: add the new public key to `~/.ssh/authorized_keys` and **remove the old line**. Removing the old line is the step that matters.
- [ ] In TRIAGE-1: store the new private key in the credential entry for those servers.
- [ ] Verify: in the approver chat ask `what is the uptime of victim-test`. Then confirm the old key is refused: `ssh -i Creds\zabb.pem <user>@<host>` must fail.
- [ ] Delete `Creds\zabb.pem` and `Creds\zabb.ppk` from this machine, and from `D:\sre-triage-agent` if they are there.

### 2.4 Supabase project (old hosted database)
- [ ] If the project still exists: Supabase > Project Settings > API > roll the secret key, or delete the project if nothing uses it. TRIAGE-1 now uses local Postgres, so nothing here needs the new key.
- [ ] If it was deleted already: nothing to do. Note the date here: ________

### 2.5 Neo4j instance (old graph database)
- [ ] If the instance still exists: change the password or delete the instance. TRIAGE-1 no longer uses Neo4j.
- [ ] If it was deleted already: nothing to do. Note the date here: ________

### 2.6 `Creds/Triage_1.txt`
- [ ] Open the file and read it once. The scan only saw that it names the default admin account and holds a long copy command. If it holds any password or key that is still in use, rotate that too and add a line to this checklist.
- [ ] Delete the file from this machine and from `D:\sre-triage-agent`.

### 2.7 TRIAGE-1's own secrets (not leaked, listed so the list is complete)
- `TRIAGE_SECRET_KEY` and the database password live in `bootstrap.json`, never in the repository. No rotation is needed for this leak. Rotating the encryption key would need every saved credential to be re-encrypted; there is no tool for that yet.
- The super admin password: the old shipped default is in history as source code. New installs no longer use it (the password is chosen on the setup page). On this install, confirm the login page shows no "default password" warning.

## 3. Decide about history

Rotation is enough to make the old values useless. Cleaning history is optional and has a cost.

| Option | What it does | Cost |
|---|---|---|
| Leave history as it is | Nothing. Old values stay readable but no longer work | None. Names, addresses and user names stay visible |
| Rewrite history (`git filter-repo --path Creds --path .env.example --invert-paths`, then force push) | Removes those files from every commit | Every commit id changes. Every clone must be made again. A force push overwrites the remote. GitHub may keep old commits reachable for a while; ask GitHub Support to purge them. Forks keep their copy |
| New repository from the current files | A clean start with no history | The old repository must be deleted or made private, and the history is lost |

If the GitHub repository is or ever was public, treat every value in section 1 as public and also check the server and ServiceNow sign-in logs for use you do not recognise.

## 4. Afterwards
- [ ] Run `.venv\Scripts\python.exe scripts\scan_history.py`. The working folder section must be empty. History entries remain unless you chose a clean-up in section 3.
- [ ] Run the tests: `.venv\Scripts\python.exe -m pytest tests -q` (`test_no_hardcoded_secrets` guards the current files).
- [ ] Record the date each item was rotated in this file.
