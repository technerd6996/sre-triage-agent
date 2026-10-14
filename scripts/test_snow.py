"""Run from the project root:  python -m scripts.test_snow
Tests the SNOW workflow directly, with no LLM involved."""
from dotenv import load_dotenv

load_dotenv()

from app.tools.snow import create_incident, resolve_incident

SHOW = ("number", "state", "caller_id", "assignment_group", "assigned_to")


def show(rec: dict):
    print({k: rec.get(k) for k in SHOW})


print("1. Create (assigned + WIP)...")
rec = create_incident.invoke(
    {"short_description": "TEST: disk full on win-server", "description": "Created by test_snow.py"}
)
show(rec)
number = rec["number"]

print("2. Resolve...")
show(resolve_incident.invoke({"number": number, "close_notes": "Test resolution"}))