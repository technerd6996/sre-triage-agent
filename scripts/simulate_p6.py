"""Offline simulation of P6 slice 1 (playbooks from the ServiceNow KB). No network.
Run from the repo root:  python -m scripts.simulate_p6
"""
import html
import os
import re
import sys
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

from app.agent import skills
from app.tools import snow

FAILS = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"   [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def to_html(raw):                      # same conversion as scripts/push_skills_to_kb.py
    return "".join(f"<p>{html.escape(ln) or '&nbsp;'}</p>" for ln in raw.strip().splitlines())


def make_rows(transform=lambda name, raw: raw, ts="2026-10-18 10:00:00", only=None):
    rows, n = [], 1
    for p in sorted(skills.SKILLS_DIR.glob("*/SKILL.md")):
        if p.parent.name.startswith("_"):
            continue
        name = p.parent.name
        raw = transform(name, p.read_text(encoding="utf-8"))
        if raw is not None and (only is None or name in only):
            rows.append({"number": f"KB{n:07d}", "short_description": name,
                         "text": to_html(raw), "sys_updated_on": ts})
        n += 1
    return rows


def serve(rows=None, error=None):
    def fake(method, path, **kw):
        if error:
            raise RuntimeError(error)
        return rows
    snow._request = fake


def load(source):
    os.environ["SKILLS_SOURCE"] = source
    return skills.load_skills(force=True)


# 1. file mode
sk = load("file")
check("1 file mode: all playbooks come from files", len(sk) >= 7 and all(s["version"].startswith("file:") for s in sk.values()))
file_sk = sk

# 2. KB mode, faithful round trip
serve(make_rows())
sk = load("kb")
check("2 kb mode: source is kb", skills.status()["source"] == "kb", str(skills.status()))
check("2 kb mode: every playbook version starts with kb:", all(s["version"].startswith("kb:") for s in sk.values()))
same = all(sk[n]["body"] == file_sk[n]["body"] and sk[n]["meta"] == file_sk[n]["meta"] for n in file_sk if n in sk)
check("2 kb mode: HTML article gives the same body and front matter as the file", same)

# 3. edit in the KB takes effect only after the cache refresh
old = {n: s["version"] for n, s in sk.items()}
serve(make_rows(lambda n, raw: raw + "\n9. SIM-MARKER-STEP" if n == "linux-service-down" else raw, ts="2026-10-18 11:00:00"))
cached = skills.load_skills()          # no force: within TTL
check("3 cache: edit not visible before the TTL", "SIM-MARKER-STEP" not in cached["linux-service-down"]["body"])
sk = skills.load_skills(force=True)
check("3 refresh: edit visible and version changed",
      "SIM-MARKER-STEP" in sk["linux-service-down"]["body"] and sk["linux-service-down"]["version"] != old["linux-service-down"])
check("3 refresh: other playbooks keep the same text",
      sk["windows-disk-full"]["body"] == file_sk["windows-disk-full"]["body"])

# 4. article with a tool the policy does not know is refused, file copy used
bad = lambda n, raw: re.sub(r"allowed_tools:.*", "allowed_tools: [claim_incident, rm_everything]", raw) if n == "linux-service-down" else raw
serve(make_rows(bad))
sk = load("kb")
check("4 unknown tool: that article refused, file copy used", sk["linux-service-down"]["version"].startswith("file:"))
check("4 unknown tool: other articles still from kb", sk["windows-service-down"]["version"].startswith("kb:"))
check("4 unknown tool: rm_everything never reaches allowed tools",
      "rm_everything" not in skills.build_playbook("nginx service is down", "linux")[2])

# 5. broken front matter (editor removed the --- lines)
broken = lambda n, raw: raw.replace("---", "", 2) if n == "linux-disk-full" else raw
serve(make_rows(broken))
sk = load("kb")
check("5 broken front matter: refused, file copy used", sk["linux-disk-full"]["version"].startswith("file:"))

# 6. ServiceNow down
serve(error="503 Service Unavailable")
sk = load("kb")
st = skills.status()
check("6 SNOW down: falls back to files", st["source"] == "file" and all(s["version"].startswith("file:") for s in sk.values()))
check("6 SNOW down: error recorded", "503" in st["error"], st["error"])

# 7. KB empty or unpublished
serve([])
sk = load("kb")
check("7 empty KB: falls back to files", skills.status()["source"] == "file" and "no valid" in skills.status()["error"])

# 8. a KB with only some articles: files fill the gaps
serve(make_rows(only={"linux-service-down", "snow-incident-lifecycle"}))
sk = load("kb")
check("8 partial KB: KB articles used, the rest from files",
      sk["linux-service-down"]["version"].startswith("kb:") and sk["windows-service-down"]["version"].startswith("file:")
      and len(sk) == len(file_sk))

# 9. build_playbook returns the version a run will record
serve(make_rows())
load("kb")
out = skills.build_playbook("nginx service is down on the linux server", "linux")
check("9 build_playbook returns 4 values", len(out) == 4, str(len(out)))
text, name, allowed, version = out
check("9 linux-service-down picked", name == "linux-service-down", str(name))
check("9 version names base and technical skill, both from kb",
      "snow-incident-lifecycle=kb:" in version and f"{name}=kb:" in version, version)
check("9 allowed tools include claim_incident", "claim_incident" in allowed)
out = skills.build_playbook("the office printer is jammed", "linux")
check("9 no match: escalate-only tool set", out[1] is None and out[2] == {"claim_incident", "get_incident", "add_work_note", "escalate_incident"})

# 10. the three edits are in place (static check)
app = Path(skills.__file__).resolve().parents[1]
a = (app / "agent" / "agent.py").read_text(encoding="utf-8")
b = (app / "api" / "agent.py").read_text(encoding="utf-8")
check("10 agent.py unpacks 4 values", re.search(r"playbook, skill, allowed_tools, skill_version\s*=\s*build_playbook", a) is not None)
check("10 agent.py puts skill_version in the start event", '"skill_version": skill_version' in a)
check("10 api/agent.py saves skill_version on the run", 'skill_version=ev.get("skill_version")' in b)

print("\n" + ("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: " + "; ".join(FAILS)))
sys.exit(1 if FAILS else 0)