"""Run from the project root:  python -m scripts.debug_resolve
Writes close_code on a ticket, then reads it back to see if it persisted."""
import sys
from dotenv import load_dotenv

load_dotenv()

from app.tools.snow import _request, _sys_id

number = sys.argv[1] if len(sys.argv) > 1 else "INC0010013"
sys_id = _sys_id(number)

print("Writing close_code...")
resp = _request(
    "PATCH",
    f"incident/{sys_id}",
    params={"sysparm_display_value": "all", "sysparm_fields": "number,state,close_code,close_notes"},
    json={"close_code": "Solved (Permanently)", "close_notes": "debug test"},
)
print("Response from PATCH:", resp)

print("Reading it back...")
back = _request(
    "GET",
    f"incident/{sys_id}",
    params={"sysparm_display_value": "all", "sysparm_fields": "number,state,close_code,close_notes"},
)
print("Stored values:", back)

print("Roles of API user...")
roles = _request(
    "GET",
    "sys_user_has_role",
    params={"sysparm_query": "user.user_name=" + __import__("os").environ["SNOW_USER"],
            "sysparm_fields": "role.name", "sysparm_limit": 50},
)
print([r.get("role.name") or r for r in roles])