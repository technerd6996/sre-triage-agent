"""Run from the project root:  python -m scripts.debug_close_code INC0010013
Tries different ways of writing close_code and reports which one actually sticks."""
import sys
from dotenv import load_dotenv

load_dotenv()

from app.tools.snow import _request, _sys_id

number = sys.argv[1] if len(sys.argv) > 1 else "INC0010013"
sys_id = _sys_id(number)


def attempt(label, body, extra=None):
    params = {"sysparm_display_value": "all", "sysparm_fields": "close_code,state"}
    params.update(extra or {})
    try:
        r = _request("PATCH", f"incident/{sys_id}", params=params, json=body)
        print(f"{label:55} close_code={r['close_code']['value']!r:35} state={r['state']['value']!r}")
    except Exception as e:
        print(f"{label:55} ERROR {str(e)[:160]}")


attempt("1. plain value", {"close_code": "Solved (Permanently)"})
attempt("2. input_display_value=true", {"close_code": "Solved (Permanently)"}, {"sysparm_input_display_value": "true"})
attempt("3. other value: Solved Remotely (Permanently)", {"close_code": "Solved Remotely (Permanently)"})
attempt("4. other value: Solved (Work Around)", {"close_code": "Solved (Work Around)"})
attempt("5. other value: Closed/Resolved by Caller", {"close_code": "Closed/Resolved by Caller"})
attempt(
    "6. all in one call (display values) - may resolve it",
    {"state": "Resolved", "close_code": "Solved (Permanently)", "close_notes": "debug test"},
    {"sysparm_input_display_value": "true"},
)