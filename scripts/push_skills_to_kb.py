"""Push local skills/*/SKILL.md into the ServiceNow KB. Safe to re-run: it updates existing articles.
Run from the repo root:  python -m scripts.push_skills_to_kb [--dry]
Needs the KB to exist first (default name: TRIAGE-1 Playbooks) and a user allowed to write to it.
Optional env: SKILLS_KB_NAME, SNOW_KB_CATEGORY (a kb_category name, only if your instance requires one).
"""
import html
import os
import sys
from dotenv import load_dotenv
load_dotenv()

from app.agent.skills import SKILLS_DIR, _parse_text, _validate
from app.tools.snow import _request, _lookup_sys_id


def to_html(raw: str) -> str:
    # one <p> per line; escaping keeps text like <username> intact (the loader unescapes it)
    return "".join(f"<p>{html.escape(ln) or '&nbsp;'}</p>" for ln in raw.strip().splitlines())


def main(dry: bool):
    kb_name = os.getenv("SKILLS_KB_NAME", "TRIAGE-1 Playbooks")
    kb_id = _lookup_sys_id("kb_knowledge_base", f"title={kb_name}", f"KB '{kb_name}'")
    cat = os.getenv("SNOW_KB_CATEGORY", "").strip()
    cat_id = _lookup_sys_id("kb_category", f"label={cat}", f"Category '{cat}'") if cat else None

    for p in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        if p.parent.name.startswith("_"):
            continue
        raw = p.read_text(encoding="utf-8")
        meta, body = _parse_text(raw)
        name = meta.get("name", p.parent.name)
        why = _validate(meta, body)
        if why:
            print(f"SKIP  {name}: {why}")
            continue
        payload = {"kb_knowledge_base": kb_id, "short_description": name,
                   "text": to_html(raw), "workflow_state": "published"}
        if cat_id:
            payload["kb_category"] = cat_id
        existing = _request("GET", "kb_knowledge", params={
            "sysparm_query": f"kb_knowledge_base={kb_id}^short_description={name}",
            "sysparm_fields": "sys_id,number", "sysparm_limit": 1})
        if dry:
            print(f"DRY   {name}: would {'update ' + existing[0]['number'] if existing else 'create'}")
            continue
        if existing:
            _request("PATCH", f"kb_knowledge/{existing[0]['sys_id']}", json=payload)
            print(f"UPDATED {name} ({existing[0]['number']})")
        else:
            r = _request("POST", "kb_knowledge", json=payload)
            print(f"CREATED {name} ({r.get('number')})")


if __name__ == "__main__":
    main("--dry" in sys.argv)