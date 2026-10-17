"""Run from the project root:  python -m scripts.simulate_zabbix
Creates a ticket in the L1 queue shaped like a Zabbix alert (New, unassigned), as the integration will."""
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

from app.tools import aws
from app.tools.snow import _caller, _group, _group_id, _request, _user_id

windows = [i for i in aws.list_instances(force=True) if i["platform"] == "windows" and i["state"] == "running"]
if not windows:
    raise SystemExit("No running Windows instance found in AWS. Check AWS_REGION and credentials.")
host = "victim-test"
now = datetime.now()
problem = "Nginx service stopped"

created = _request(
    "POST",
    "incident",
    json={
        "short_description": f"Zabbix: {problem} (host: {host})",
        "description": (
            f"Problem started at {now:%H:%M:%S} on {now:%Y.%m.%d}\n"
            f"Problem name: {problem}\nHost: {host}\nSeverity: Warning\n"
            f"Original problem ID: {now:%H%M%S}"
        ),
        "urgency": "3",
        "caller_id": _user_id(_caller()),
        "assignment_group": _group_id(_group()),
    },
)
print("Created", created["number"], "in the L1 queue (New, unassigned)")