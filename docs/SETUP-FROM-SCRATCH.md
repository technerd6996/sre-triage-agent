# TRIAGE-1: setting up everything from scratch

Written 2026-11-13 after the original VM, AWS lab and integrations were lost. Follow the parts in order. Each part
ends with a **Check** so you know it worked before moving on. Values in `<angle brackets>` are yours to choose.

```
                  ┌────────────── AWS (one region) ───────────────┐
 Zabbix agents ──►│ zabbix-mon ──webhook──► ServiceNow incident    │
 on servers       │                          (L1-SRE-Agent queue)  │
                  │                                │ poll 15 s     │
                  │ TRIAGE-1 host ◄────────────────┘               │
                  │   │  SSH 22 / WinRM 5985 (public IP, /32 only) │
                  │   ├──► victim-test (Linux)                     │
                  │   ├──► win-test (Windows)                      │
                  │   ├──► Secrets Manager (server logins)         │
                  │   └──► SES (owner mail)                        │
                  └────────────────────────────────────────────────┘
 Model server (Colab T4 or GPU VM, OpenAI-compatible, /health) ◄── TRIAGE-1
```

| Part | What | Time |
|---|---|---|
| 0 | Secure what you still have | 15 min |
| 1 | AWS: region, IAM, security groups, servers | 1-2 h |
| 2 | Server logins in Secrets Manager | 20 min |
| 3 | ServiceNow: instance, users, group, CMDB, knowledge | 1-2 h |
| 4 | Model server | 30-60 min |
| 5 | TRIAGE-1 itself | 30 min |
| 6 | Zabbix: server, agents, ServiceNow webhook | 1-2 h |
| 7 | End-to-end test | 30 min |
| 8 | Backups and keeping it safe | 15 min |

---

## Part 0. Secure what you still have

1. **Push the code.** Everything on this VM that is not on GitHub can be lost the same way:
   `git add -A`, `git commit`, `git push` (the repo is `github.com/technerd6996/sre-triage-agent`).
2. **Copy two files off this VM** to a password manager or an encrypted drive:
   - `C:\ProgramData\triage\bootstrap.json` (database password + the key that encrypts saved credentials)
   - `C:\Users\VedaVidyadaran\TRIAGE1-vendor\triage1-licence-private.pem` (licence signing key)
3. **Rotate old secrets.** Anything that was in the old `Creds/` folder is still in git history (see
   `docs/CREDENTIAL-ROTATION.md`). The old AWS access keys, ServiceNow passwords and SSH keys must be treated as leaked:
   delete them; do not reuse them below.

---

## Part 1. AWS

### 1.1 Pick one region and use it everywhere
The old lab used **ap-south-2 (Hyderabad)**. `ap-south-1 (Mumbai)` has more services and instance types. Whatever you
pick goes into TRIAGE-1's **AWS region** setting later, and every resource below must be in it.

### 1.2 IAM: a role for the TRIAGE-1 host (preferred over access keys)
IAM > Policies > Create policy > JSON. Replace `<region>`, `<account-id>`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "ReadServers", "Effect": "Allow",
      "Action": ["ec2:DescribeInstances", "ec2:DescribeSecurityGroups", "cloudwatch:GetMetricStatistics"],
      "Resource": "*" },
    { "Sid": "ServerLogins", "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:<region>:<account-id>:secret:triage/pam/hosts/*" },
    { "Sid": "OwnerMail", "Effect": "Allow", "Action": "ses:SendEmail", "Resource": "*" },
    { "Sid": "P1Alerts", "Effect": "Allow", "Action": "sns:Publish",
      "Resource": "arn:aws:sns:<region>:<account-id>:<topic-name>" }
  ]
}
```
Name it `triage1-app`. Drop the `P1Alerts` statement if you will not use SNS. If you later use the L3 Lambda
(`infra.l3_lambda`), add `lambda:InvokeFunction` on that one function.

IAM > Roles > Create role > AWS service > EC2 > attach `triage1-app` > name `triage1-app-role`.
Attach it to the TRIAGE-1 host: EC2 > instance > Actions > Security > Modify IAM role.

> Only if TRIAGE-1 runs **outside** AWS (your laptop): create an IAM user with the same policy and an access key, and
> enter it in Admin > Connections > Notifications (**AWS access key ID / secret**). Never commit it.

### 1.3 Security groups
Find the TRIAGE-1 host's **public** IP (TRIAGE-1 connects to servers over their public IPs).

| Group | Inbound rule | Source |
|---|---|---|
| `sg-linux-targets` | TCP 22 (SSH) | `<triage1-public-ip>/32` |
| `sg-linux-targets` | TCP 10050 (Zabbix agent) | `sg-zabbix` |
| `sg-windows-targets` | TCP 5985 (WinRM) | `<triage1-public-ip>/32` |
| `sg-windows-targets` | TCP 10050 (Zabbix agent) | `sg-zabbix` |
| `sg-windows-targets` | TCP 3389 (RDP, for you) | `<your-own-ip>/32` |
| `sg-zabbix` | TCP 80/443 (web UI) | `<your-own-ip>/32` |
| `sg-zabbix` | TCP 10051 (active agents) | `sg-linux-targets`, `sg-windows-targets` |
| `sg-zabbix` | TCP 22 | `<your-own-ip>/32` |

Never open 22, 5985 or 3389 to `0.0.0.0/0`. If the TRIAGE-1 host has no Elastic IP, its public IP changes when it
stops; give it an **Elastic IP** so these rules stay valid.

### 1.4 Servers (EC2). The **Name tag is what TRIAGE-1 and Zabbix match on**, so keep names identical everywhere.

| Name tag | AMI | Type | Security group | Purpose |
|---|---|---|---|---|
| `triage1-app` | Windows Server 2022/2025 | t3.large | own SG, RDP from your IP | Runs TRIAGE-1 (or use this VM) |
| `victim-test` | Amazon Linux 2023 | t3.micro | `sg-linux-targets` | Linux target |
| `win-test` | Windows Server 2022 | t3.medium | `sg-windows-targets` | Windows target |
| `zabbix-mon` | Ubuntu 24.04 LTS | t3.medium | `sg-zabbix` + `sg-linux-targets` | Zabbix server |

All target servers need a **public IP** (auto-assign on launch, or an Elastic IP). Create one SSH key pair for the
Linux servers and keep the `.pem` safe (it goes into Secrets Manager in Part 2).

**On `victim-test`** (things for the agent to diagnose):
```bash
sudo dnf install -y nginx sysstat
sudo systemctl enable --now nginx
```
Then add the narrow sudoers file from `docs/CONFIGURATION-CHECKLIST.md` item 6 for the account TRIAGE-1 uses (do not
leave `ec2-user` with `NOPASSWD: ALL` if you can avoid it).

**On `win-test`** (PowerShell as Administrator), enable WinRM over HTTP with NTLM:
```powershell
winrm quickconfig -q
Set-Item WSMan:\localhost\Service\Auth\Negotiate $true
New-NetFirewallRule -DisplayName "WinRM 5985 from TRIAGE-1" -Direction Inbound -Protocol TCP -LocalPort 5985 -RemoteAddress <triage1-public-ip> -Action Allow
```
Create a local admin account for TRIAGE-1 (for example `svc_triage`) with a long password.

**Check:** from the TRIAGE-1 host, `Test-NetConnection <victim-test-public-ip> -Port 22` and
`Test-NetConnection <win-test-public-ip> -Port 5985` both say `TcpTestSucceeded : True`.

---

## Part 2. Server logins in AWS Secrets Manager

TRIAGE-1 looks up `triage/pam/hosts/<instance-id>` first, then `triage/pam/hosts/<public-ip>`. Use the **instance id**
(it never changes). Secrets Manager > Store a new secret > Other type of secret > Plaintext:

Linux (`triage/pam/hosts/<victim-test instance id>`):
```json
{"username": "ec2-user", "port": 22, "private_key": "-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----\n", "passphrase": ""}
```
(Paste the key with `\n` for line breaks. Use a dedicated service account instead of `ec2-user` if you created one.)

Windows (`triage/pam/hosts/<win-test instance id>`):
```json
{"username": "svc_triage", "password": "<password>", "port": 5985, "transport": "ntlm"}
```
Add one for `zabbix-mon` too, so TRIAGE-1 can check the monitoring server itself.

**Check:** after Part 5, ask the approver chat `uptime of victim-test`. "No credential available" means the secret
name or region is wrong.

---

## Part 3. ServiceNow

### 3.1 Instance
Get a free Personal Developer Instance at `developer.servicenow.com` (Request instance). PDIs **hibernate after about
10 days without use and can be reclaimed**: log in regularly, and keep a copy of what you create (Part 8).

### 3.2 Users (System Security > Users > New). Use long random passwords, tick **Web service access only** for the
service accounts.

| User ID | Roles | Used for |
|---|---|---|
| `svc_triage_01` | `itil`, `knowledge` | TRIAGE-1 agent: reads/updates incidents, CMDB, KB |
| `svc_triage_checker` | `itil` | Independent checker (must be a different identity) |
| `sre_triage_agent` | (none) | Caller on tickets TRIAGE-1 creates |
| `zabbix_integration` | `itil` | Zabbix creates incidents with it (Part 6) |

Your own user needs an **email** on its profile: owner mail goes there.

### 3.3 Groups (User Administration > Groups > New)
- `L1-SRE-Agent`: the queue TRIAGE-1 works. Add `svc_triage_01` as a member.
- `L2-SRE`: the escalation group. Add yourself. Fill in the group **Email**.

### 3.4 CMDB: one server CI per EC2 server
Configuration > Servers (`cmdb_ci_server`) > New, for `victim-test`, `win-test`, `zabbix-mon`, `triage1-app`:
- **Name** = exactly the EC2 Name tag
- **IP Address** = its public IP
- **Owned by** = you (needed for owner approval by email)
- **Support group** = `L2-SRE`

### 3.5 Knowledge bases
- Knowledge > Knowledge Bases > New: **SRE Runbooks** (chat answers come from its published articles).
- Create articles from the six files in `runbooks/` (copy each file's text into a new article) and **Publish** them.
- Playbooks that steer agent runs are read from the `skills/` folder by default (`knowledge.skills_source = file`),
  so no second knowledge base is needed.

**Check:** in ServiceNow, log in as `svc_triage_01` (or use REST API Explorer as that user) and open an incident and
`cmdb_ci_server`; both must be readable.

---

## Part 4. Model server

TRIAGE-1 needs an **OpenAI-compatible chat completions** endpoint with streaming, plus a `GET /health` that returns
200 at the same base address. vLLM provides both. (Ollama and LM Studio have no `/health`, so TRIAGE-1 would show the
model as offline; that fallback is not built yet.)

**Option A: Google Colab with a T4 GPU (what the project used).** In a Colab notebook with Runtime type T4 GPU:
```python
!pip install -q vllm pyngrok
import subprocess
subprocess.Popen("vllm serve Qwen/Qwen3-8B-AWQ --max-model-len 8192 --gpu-memory-utilization 0.90 --port 8000 > vllm.log 2>&1", shell=True)
# wait until vllm.log shows "Application startup complete", then:
from pyngrok import ngrok
ngrok.set_auth_token("<your ngrok token>")
print(ngrok.connect(8000).public_url)
```
Model API URL for TRIAGE-1 = `<that ngrok url>/v1/chat/completions`. Colab sessions end after a few hours and the
ngrok URL changes each time: update the setting when it does. If the model does not fit or vLLM refuses the T4, use
`Qwen/Qwen2.5-7B-Instruct-AWQ` and set **Model name** to the same string.

**Option B: a GPU VM (stable, costs money).** For example AWS `g4dn.xlarge` (T4) with the same `vllm serve` command,
reachable only from the TRIAGE-1 host. URL = `http://<gpu-vm-ip>:8000/v1/chat/completions`.

**Check:** `curl <base-url>/health` returns 200, and the Admin > Behaviour > Model runtime **Test** button passes.

---

## Part 5. TRIAGE-1

On the TRIAGE-1 host (Windows), PowerShell **as Administrator** the first time:
```powershell
winget install Git.Git
winget install Python.Python.3.13
# open a NEW PowerShell window, then:
git clone https://github.com/technerd6996/sre-triage-agent.git
cd sre-triage-agent
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-prod.txt -c packaging\constraints.txt pytest
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```
The first run installs PostgreSQL 17, creates the database and `C:\ProgramData\triage\bootstrap.json`, and prints
`SETUP REQUIRED: ... setup code: XXXX`. Open `http://127.0.0.1:8000/setup`, enter the code and choose the admin password.

Then in the admin page:

| Where | Setting | Value |
|---|---|---|
| Connections > Ticketing | ServiceNow instance URL | `https://<dev12345>.service-now.com` |
| | ServiceNow user / password (agent) | `svc_triage_01` / its password |
| | ServiceNow user / password (checker) | `svc_triage_checker` / its password |
| | Assignment group | `L1-SRE-Agent` |
| | Escalation group | `L2-SRE` |
| | Assignee / Ticket caller | `svc_triage_01` / `sre_triage_agent` |
| Connections > Notifications | AWS region | your region from 1.1 |
| | Notice transport, SES sender | `ses`, a sender you verified in SES |
| Connections > Credentials (PAM) | Where server logins are stored | `awssm` (prefix stays `triage/pam/hosts`) |
| Connections > Knowledge source | Knowledge source / KB name | `servicenow` / `SRE Runbooks` |
| Behaviour > Model runtime | Model API URL, Model name | from Part 4 |
| Behaviour > Licence and plan | Licence key | issue one with `scripts\licence_tool.py` (see below) |
| Connections > Identity (AD) | | skip unless you have a domain; local accounts work without it |

Licence key (run on the machine that holds the private key):
```powershell
.venv\Scripts\python.exe scripts\licence_tool.py issue --key <path>\triage1-licence-private.pem --customer "Lab" --plan Commander --itsm servicenow --days 365
```

SES: in the SES console (your region) verify the sender address and your own mailbox under **Verified identities**.
A new SES account is in sandbox and can only mail verified addresses.

**Check:** each section's **Test** button passes; the admin Overview shows the plan and a real server count (not `?`).

---

## Part 6. Zabbix

### 6.1 Zabbix server on `zabbix-mon`
Go to `zabbix.com/download` and choose: **Zabbix 7.0 LTS, Ubuntu 24.04, Server + Frontend + Agent 2, MySQL, Apache**.
The page prints the exact commands; run them in order (repository package, `apt install ...`, create the MySQL
database and user, import the initial schema, set `DBPassword` in `/etc/zabbix/zabbix_server.conf`, restart the
services). Then open `http://<zabbix-mon-public-ip>/zabbix`, finish the web wizard, log in as `Admin` / `zabbix` and
**change that password straight away**.

### 6.2 Agents on the target servers
**Linux (`victim-test`, Amazon Linux 2023):** on `zabbix.com/download` choose Zabbix 7.0, the matching OS, **Agent 2**,
and run the commands. Then in `/etc/zabbix/zabbix_agent2.conf`:
```
Server=<zabbix-mon private IP>
ServerActive=<zabbix-mon private IP>
Hostname=victim-test
```
`sudo systemctl enable --now zabbix-agent2`

**Windows (`win-test`):** download the Zabbix Agent 2 MSI (7.0 LTS) from `zabbix.com/download_agents`, install it with
Server / Active server = `<zabbix-mon private IP>` and Hostname = `win-test`.

**Hosts in Zabbix:** Data collection > Hosts > Create host:
- **Host name** = the EC2 Name tag (`victim-test`, `win-test`). TRIAGE-1 finds the server by this name in the ticket.
- Interface: Agent, the server's **private** IP, port 10050.
- Templates: `Linux by Zabbix agent` / `Windows by Zabbix agent`. Add `Nginx by Zabbix agent` to `victim-test`.

**Check:** the host's availability icon turns green within a few minutes.

### 6.3 Zabbix → ServiceNow (the built-in ServiceNow webhook)
1. **Alerts > Media types > ServiceNow**: set `servicenow_url` = `https://<dev12345>.service-now.com`,
   `servicenow_user` = `zabbix_integration`, `servicenow_password` = its password. Enable the media type.
2. In the same media type, **Message templates**: for *Problem* set the subject to
   `Zabbix: {EVENT.NAME} (host: {HOST.NAME})`. TRIAGE-1 and its playbooks expect this shape (it is also what
   `scripts/simulate_zabbix.py` creates), and it is what the assignment rule below matches on.
3. **Users > Users > Create user** `servicenow-bot`, user group *No access to the frontend*. On its **Media** tab add
   type **ServiceNow**, Send to `servicenow`, severities Warning and above. Give it read permission to the host groups
   of your servers (Permissions tab / user role), or no alerts will be sent.
4. **Alerts > Actions > Trigger actions > Create action** `Create ServiceNow incident`: condition
   *Trigger severity is greater than or equals Warning*; Operations: send to user `servicenow-bot` via ServiceNow;
   also add a Recovery operation so the incident gets the resolution note.

### 6.4 Route Zabbix incidents into TRIAGE-1's queue (in ServiceNow)
System Policy > Rules > **Assignment** > New:
- Table: Incident. Conditions: *Short description starts with* `Zabbix:`
- Assign to group: `L1-SRE-Agent`

TRIAGE-1 polls that group every 15 seconds (`agent.poll_seconds`) once **Auto mode** is on in the Agent console.

**Check:** on `victim-test` run `sudo systemctl stop nginx`. Within a few minutes Zabbix raises *Nginx is down* (or a
similar trigger), a new incident `Zabbix: ... (host: victim-test)` appears in `L1-SRE-Agent`, and with auto mode on,
TRIAGE-1 picks it up. Without Zabbix you can test the same path with `python -m scripts.simulate_zabbix`.

---

## Part 7. End-to-end test (safest first)
1. Approver chat: `uptime of victim-test`, `cpu and memory on win-test` (read-only, run at once).
2. `restart nginx on victim-test` → card → **Confirm** (an L1 change; a ticket is created and noted).
3. Stop nginx on `victim-test`, let Zabbix raise the incident, and watch TRIAGE-1 triage, fix and the checker verify it.
4. Owner approval: follow `docs/CONFIGURATION-CHECKLIST.md` item 11 step 3 (reboot `victim-test` with yourself as owner).
5. Offline evaluation: `.venv\Scripts\python.exe -m replay.run --check`, then a labelled run for the dissertation.

---

## Part 8. Backups so this does not happen again
- `powershell -ExecutionPolicy Bypass -File scripts\backup.ps1 -Install` (as Administrator): nightly encrypted database
  backup at 02:30 into `C:\ProgramData\triage\backups`. Copy that folder **off the machine** regularly.
- Keep `bootstrap.json` and the licence private key in a password manager (Part 0).
- ServiceNow PDI: export your users, groups, CIs, KB articles and the assignment rule as an **update set** or XML
  (right-click list header > Export > XML) after any change.
- Zabbix: Data collection > Hosts > select all > **Export** (YAML), and export the media type and action.
- Commit and push after every working session.
