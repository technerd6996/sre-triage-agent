"""Shows what ServiceNow and our database hold for one ticket.
Run from the repo root:  python -m scripts.check_ticket INC0010095"""
import sys

from app import store
from app.tools.snow import _request


def show(title, rows):
    print(f"\n== {title}")
    for r in rows or ["(none)"]:
        print("  ", r)


def main(number: str):
    for who in ("agent", "checker"):
        try:
            rows = _request("GET", "incident", identity=who, params={
                "sysparm_query": f"number={number}", "sysparm_limit": 1,
                "sysparm_fields": "sys_id,state,close_code,assignment_group,assigned_to,sys_updated_on,sys_updated_by",
                "sysparm_display_value": "all"})
            r = rows[0]
            show(f"incident as {who}", [f"{k}: {v.get('display_value') if isinstance(v, dict) else v}" for k, v in r.items()])
            sys_id = r["sys_id"]["value"] if isinstance(r["sys_id"], dict) else r["sys_id"]
        except Exception as e:
            show(f"incident as {who}", [f"FAILED: {str(e)[:250]}"])
    try:
        j = _request("GET", "sys_journal_field", params={
            "sysparm_query": f"element_id={sys_id}^element=work_notes^ORDERBYDESCsys_created_on",
            "sysparm_fields": "sys_created_on,sys_created_by,value", "sysparm_limit": 6})
        show("last work notes (newest first)", [f"{x['sys_created_on']} {x['sys_created_by']}: {x['value'][:110]!r}" for x in j])
    except Exception as e:
        show("work notes", [f"FAILED: {str(e)[:250]}"])
    a = store.supabase.table("audit").select("ts,actor,action,detail").eq("ticket", number).order("ts").execute().data
    show("audit rows", [f"{x['actor']}: {x['action']} {x['detail'][:90]}" for x in a])
    v = store.supabase.table("verifications").select("*").eq("ticket", number).execute().data
    show("verifications rows", [f"mode={x['mode']} verdict={x['verdict']} closed_at={x['closed_at']} {x['checks_json']}" for x in v])


if __name__ == "__main__":
    main(sys.argv[1])