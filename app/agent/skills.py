"""Loads SKILL.md files. The code picks the skill, not the model, because a 7B model picks badly."""
import re
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "skills"
BASE_SKILL = "snow-incident-lifecycle"  # always included


def _parse(path: Path):
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
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


def load_skills() -> dict:
    skills = {}
    for p in SKILLS_DIR.glob("*/SKILL.md"):
        if p.parent.name.startswith("_"):
            continue
        meta, body = _parse(p)
        skills[meta.get("name", p.parent.name)] = {"meta": meta, "body": body}
    return skills


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
        score = sum(1 for kw in s["meta"].get("keywords", []) if kw.lower() in text)
        if score > best_score:
            best_name, best, best_score = name, s, score
    return best_name, best


def build_playbook(ticket_text: str, platform: str = None):
    """Returns (playbook_text, technical_skill_name, allowed_tool_names)."""
    skills = load_skills()
    parts = []
    allowed = set()

    if BASE_SKILL in skills:
        parts.append(skills[BASE_SKILL]["body"])
        allowed.update(skills[BASE_SKILL]["meta"].get("allowed_tools", []))

    name, tech = pick_skill(ticket_text, platform)
    if tech:
        parts.append(f"TECHNICAL SKILL: {name}\n{tech['body']}")
        allowed.update(tech["meta"].get("allowed_tools", []))
    else:
        parts.append(
            "TECHNICAL SKILL: none\n"
            "No L1 automated playbook matches this issue. "
            "Call claim_incident, call add_work_note stating no matching L1 playbook was found, "
            "and call escalate_incident. Do not run any other tools."
        )
        allowed = {"claim_incident", "get_incident", "add_work_note", "escalate_incident"}

    return "\n\n".join(parts), name, allowed