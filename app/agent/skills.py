"""Loads playbooks. The code picks the skill, not the model (a 7B model picks badly).

P6: playbooks come from the ServiceNow KB "TRIAGE-1 Playbooks" when SKILLS_SOURCE=kb,
cached with a TTL; local SKILL.md files are the fallback and baseline.
Each loaded skill carries a `version` ("kb:KB0010012@2026-10-18 10:00:00" or "file:ab12cd34")
so a run can record exactly which playbook text it used.

Env: SKILLS_SOURCE = file (default) | kb ; SKILLS_KB_NAME ; SKILLS_TTL_SECONDS
"""
import hashlib
import html
import os
import re
import time
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "skills"
BASE_SKILL = "snow-incident-lifecycle"  # always included
_FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_CACHE = {"at": 0.0, "skills": None, "source": "file", "error": ""}


def _parse_text(text: str):
    m = _FRONT.match(text.strip())
    if not m:
        return {}, text.strip()
    meta = {}
    for line in m.group(1).splitlines():
        line = line.split("  #")[0]  # drop trailing comments
        k, _, v = line.partition(":")
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            meta[k.strip()] = [x.strip() for x in v[1:-1].split(",") if x.strip()]
        else:
            meta[k.strip()] = v
    return meta, m.group(2).strip()


def _parse(path: Path):
    return _parse_text(path.read_text(encoding="utf-8"))


def _load_files() -> dict:
    skills = {}
    for p in SKILLS_DIR.glob("*/SKILL.md"):
        if p.parent.name.startswith("_"):
            continue
        raw = p.read_text(encoding="utf-8")
        meta, body = _parse_text(raw)
        skills[meta.get("name", p.parent.name)] = {
            "meta": meta, "body": body,
            "version": "file:" + hashlib.sha1(raw.encode()).hexdigest()[:8]}
    return skills


# ---------------- ServiceNow KB ----------------
def _html_to_text(s: str) -> str:
    s = re.sub(r"(?i)<\s*br\s*/?>|</\s*(p|div|li|h[1-6]|tr)\s*>", "\n", s or "")
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s).replace("\xa0", " ")
    return "\n".join(ln.rstrip() for ln in s.splitlines()).strip()


def _validate(meta: dict, body: str) -> str:
    """Return '' if the article is usable, else the reason. policy.RISK is the hard limit:
    an article may only list tools the policy knows."""
    from app.agent.policy import RISK
    if not meta.get("name"):
        return "missing name"
    if not body:
        return "empty body"
    tools = meta.get("allowed_tools")
    if not isinstance(tools, list) or not tools:
        return "missing allowed_tools"
    bad = [t for t in tools if t not in RISK]
    if bad:
        return f"unknown tools {bad}"
    if meta.get("systems") is not None and not isinstance(meta["systems"], list):
        return "systems must be a [list]"
    return ""


def _load_kb() -> dict:
    from app.tools.snow import _request          # lazy: avoids a circular import
    kb = os.getenv("SKILLS_KB_NAME", "TRIAGE-1 Playbooks").replace("^", "")
    rows = _request("GET", "kb_knowledge", params={
        "sysparm_query": f"kb_knowledge_base.title={kb}^workflow_state=published^active=true",
        "sysparm_fields": "number,short_description,text,sys_updated_on",
        "sysparm_limit": 50})
    skills = {}
    for r in rows:
        meta, body = _parse_text(_html_to_text(r.get("text", "")))
        why = _validate(meta, body)
        if why:
            print(f"[skills] KB {r.get('number')} refused: {why}")
            continue
        skills[meta["name"]] = {"meta": meta, "body": body,
                                "version": f"kb:{r['number']}@{r.get('sys_updated_on', '')}"}
    return skills


def load_skills(force: bool = False) -> dict:
    ttl = int(os.getenv("SKILLS_TTL_SECONDS", "300"))
    if not force and _CACHE["skills"] is not None and time.time() - _CACHE["at"] < ttl:
        return _CACHE["skills"]
    skills, source, error = _load_files(), "file", ""
    if os.getenv("SKILLS_SOURCE", "file").lower() == "kb":
        try:
            kb = _load_kb()
            if kb:
                skills.update(kb)   # a KB article overrides the file of the same name; files fill the gaps
                source = "kb"
            else:
                error = "KB returned no valid articles"
        except Exception as e:      # SNOW down or KB missing: keep running on files
            error = str(e)[:200]
    if error:
        print(f"[skills] using local files: {error}")
    _CACHE.update(at=time.time(), skills=skills, source=source, error=error)
    return skills


def status() -> dict:
    """Where the playbooks came from and which version each has (for a later admin panel)."""
    sk = load_skills()
    return {"source": _CACHE["source"], "error": _CACHE["error"],
            "skills": {n: s["version"] for n, s in sk.items()}}


def _kw_match(keyword: str, text: str) -> bool:
    """Whole-word match, so a keyword like 'c' no longer matches every ticket.
    A trailing plural is allowed ('service' matches 'services'). `text` must be lower-case."""
    kw = re.escape(keyword.lower())
    return re.search(rf"(?<![a-z0-9]){kw}(?:e?s)?(?![a-z0-9])", text) is not None


def pick_skill(ticket_text: str, platform: str = None):
    """Technical skill whose keywords match best, limited to skills for this platform."""
    text = ticket_text.lower()
    best_name, best, best_score = None, None, 0
    for name, s in load_skills().items():
        if name == BASE_SKILL:
            continue
        systems = s["meta"].get("systems", [])
        if platform and systems and platform not in systems:
            continue
        score = sum(1 for kw in s["meta"].get("keywords", []) if _kw_match(kw, text))
        if score > best_score:
            best_name, best, best_score = name, s, score
    return best_name, best


def build_playbook(ticket_text: str, platform: str = None):
    """Returns (playbook_text, technical_skill_name, allowed_tool_names, version)."""
    skills = load_skills()
    parts, allowed, versions = [], set(), []

    if BASE_SKILL in skills:
        parts.append(skills[BASE_SKILL]["body"])
        allowed.update(skills[BASE_SKILL]["meta"].get("allowed_tools", []))
        versions.append(f"{BASE_SKILL}={skills[BASE_SKILL]['version']}")

    name, tech = pick_skill(ticket_text, platform)
    if tech:
        parts.append(f"TECHNICAL SKILL: {name}\n{tech['body']}")
        allowed.update(tech["meta"].get("allowed_tools", []))
        versions.append(f"{name}={tech['version']}")
    else:
        parts.append(
            "TECHNICAL SKILL: none\n"
            "No L1 automated playbook matches this issue. "
            "Call claim_incident, call add_work_note stating no matching L1 playbook was found, "
            "and call escalate_incident. Do not run any other tools."
        )
        allowed = {"claim_incident", "get_incident", "add_work_note", "escalate_incident"}

    return "\n\n".join(parts), name, allowed, "; ".join(versions)