"""Push runbooks/*.md into the ServiceNow knowledge base the chat searches (admin setting knowledge.kb_name,
default "SRE Runbooks"). Creates the knowledge base if it is missing. Safe to re-run: existing articles (matched by
title) are updated, nothing is deleted.

Run from the repo root:   python -m scripts.push_runbooks_to_kb [--dry]
Needs a ServiceNow user that may create knowledge bases and articles (the integration user in the settings).
Optional env: SNOW_KB_CATEGORY (a kb_category label, only if your instance requires one).

File format: the first line is "# Title" (the article title); "## Heading" becomes a heading, "- item" a bullet list,
"1. item" a numbered list, `code` stays code, anything else is a paragraph."""
import html
import os
import re
import sys
from pathlib import Path

from app import bootstrap_store

bootstrap_store.load()
from app import config  # noqa: E402
from app import knowledge  # noqa: E402,F401  (registers the knowledge.* settings)

config.hydrate_environ()

from app.adapters.servicenow.client import _lookup_sys_id, _request, _user_id  # noqa: E402

RUNBOOKS = Path(__file__).resolve().parent.parent / "runbooks"


def _inline(text: str) -> str:
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", html.escape(text))


def to_html(md: str) -> str:
    out, kind = [], None                       # kind = the list currently open ("ul" or "ol")

    def close():
        nonlocal kind
        if kind:
            out.append(f"</{kind}>")
            kind = None

    for line in md.splitlines()[1:]:           # line 0 is the title
        s = line.strip()
        if not s:
            close()
        elif s.startswith("## "):
            close()
            out.append(f"<h3>{_inline(s[3:])}</h3>")
        elif s.startswith("- "):
            if kind != "ul":
                close()
                out.append("<ul>")
                kind = "ul"
            out.append(f"<li>{_inline(s[2:])}</li>")
        elif re.match(r"\d+\.\s", s):
            if kind != "ol":
                close()
                out.append("<ol>")
                kind = "ol"
            out.append(f"<li>{_inline(re.sub(r'^\d+\.\s', '', s))}</li>")
        else:
            close()
            out.append(f"<p>{_inline(s)}</p>")
    close()
    return "".join(out)


def ensure_kb(name: str, dry: bool):
    rows = _request("GET", "kb_knowledge_base", params={
        "sysparm_query": f"title={name}", "sysparm_fields": "sys_id,title", "sysparm_limit": 1})
    if rows:
        print(f"KB    '{name}' exists")
        return rows[0]["sys_id"]
    if dry:
        print(f"DRY   would create the knowledge base '{name}'")
        return None
    body = {"title": name, "description": "Runbooks the TRIAGE-1 chat searches when answering SRE questions.", "active": "true"}
    try:
        created = _request("POST", "kb_knowledge_base", json=body)
    except RuntimeError as e:
        if "owner" not in str(e).lower():
            raise
        body["owner"] = _user_id(os.environ["SNOW_USER"])          # some instances require an owner
        created = _request("POST", "kb_knowledge_base", json=body)
    print(f"KB    created '{name}'")
    return created["sys_id"]


def main(dry: bool):
    name = (config.get("knowledge.kb_name") or "SRE Runbooks").replace("^", "").strip()
    kb_id = ensure_kb(name, dry)
    cat = os.getenv("SNOW_KB_CATEGORY", "").strip()
    cat_id = _lookup_sys_id("kb_category", f"label={cat}", f"Category '{cat}'") if cat else None

    for p in sorted(RUNBOOKS.glob("*.md")):
        md = p.read_text(encoding="utf-8")
        title = md.splitlines()[0].lstrip("# ").strip()
        payload = {"short_description": title, "text": to_html(md), "workflow_state": "published"}
        if kb_id:
            payload["kb_knowledge_base"] = kb_id
        if cat_id:
            payload["kb_category"] = cat_id
        existing = (_request("GET", "kb_knowledge", params={
            "sysparm_query": f"kb_knowledge_base={kb_id}^short_description={title.replace('^', '')}",
            "sysparm_fields": "sys_id,number", "sysparm_limit": 1}) if kb_id else [])
        if dry:
            print(f"DRY   {title}: would {'update ' + existing[0]['number'] if existing else 'create'}")
        elif existing:
            _request("PATCH", f"kb_knowledge/{existing[0]['sys_id']}", json=payload)
            print(f"UPDATED {title} ({existing[0]['number']})")
        else:
            r = _request("POST", "kb_knowledge", json=payload)
            print(f"CREATED {title} ({r.get('number')})")


if __name__ == "__main__":
    main("--dry" in sys.argv)
