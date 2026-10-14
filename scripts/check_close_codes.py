"""Run from the project root:  python -m scripts.check_close_codes
Shows the Resolution code values already used on resolved incidents (read-only)."""
from dotenv import load_dotenv

load_dotenv()

from app.tools.snow import _request

rows = _request(
    "GET",
    "incident",
    params={
        "sysparm_query": "close_codeISNOTEMPTY",
        "sysparm_fields": "number,close_code,state",
        "sysparm_display_value": "all",  # returns both the raw value and the label
        "sysparm_limit": 20,
    },
)

seen = set()
for r in rows:
    cc = r["close_code"]
    key = (cc["value"], cc["display_value"])
    if key not in seen:
        seen.add(key)
        print(f"value={cc['value']!r:45} label={cc['display_value']!r}")

if not seen:
    print("No resolved incidents with a resolution code found.")