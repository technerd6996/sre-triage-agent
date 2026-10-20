# scripts/probe_cancel.py   run: python -m scripts.probe_cancel
from dotenv import load_dotenv
load_dotenv()
from app.tools.snow import create_incident, _request, _incident_id

t = create_incident.invoke({"short_description": "probe: cancel test (safe to ignore)",
                            "description": "Created by scripts.probe_cancel", "urgency": 3, "impact": 3, "ci": ""})
n = t["number"]
print("created", n)
sid = _incident_id(n)
try:
    _request("PATCH", f"incident/{sid}", json={"state": "8", "work_notes": "probe: cancel"})
    print("OK     state to Canceled")
except Exception as e:
    print("DENIED", str(e)[:200])
rows = _request("GET", "incident", params={"sysparm_query": f"number={n}", "sysparm_fields": "state",
                                           "sysparm_display_value": "false", "sysparm_limit": 1})
print("final state:", rows[0]["state"], "(8 = Canceled)")