"""Run from the project root:
  python -m scripts.test_agent               creates a test ticket, then runs the agent on it
  python -m scripts.test_agent INC0010020    runs the agent on an existing ticket
Needs the Colab session and ngrok URL to be alive (COLAB_API_URL in .env)."""
import sys

from dotenv import load_dotenv

load_dotenv()

from app.agent.agent import resume, start
from app.tools.snow import create_incident, get_incident


def drive(events):
    thread_id, pending = None, None
    for ev in events:
        t = ev["type"]
        if t == "start":
            thread_id = ev["thread_id"]
            print(f"[start]  ticket={ev['ticket']} skill={ev['skill']}")
        elif t == "tool_call":
            print(f"[call]   {ev['name']} {ev['args']}")
        elif t == "tool_result":
            print(f"[result] {ev['name']}: {ev['content'][:250]}")
        elif t == "approval_required":
            pending = ev
        elif t == "final":
            print(f"[final]  {ev['text']}")
        elif t == "nudge":
            print(f"[nudge]  {ev['text']}")
        elif t == "warning":
            print(f"[WARN]   {ev['message']}")
        elif t == "error":
            print(f"[ERROR]  {ev['message']}")
    return thread_id, pending

if len(sys.argv) > 1:
    number = sys.argv[1]
else:
    number = create_incident.invoke({
        "short_description": "Disk space low on windows server",
        "description": "The C: drive on the windows server is filling up. Please check and fix.",
    })["number"]
print("Ticket:", number)

thread_id, pending = drive(start(number))
while pending:
    for a in pending["actions"]:
        print(f"\n>>> APPROVAL NEEDED: {a['name']} {a['args']}")
    ok = input("Approve? (y/n): ").strip().lower() == "y"
    _, pending = drive(resume(thread_id, ok))

print("\nFinal ticket:", get_incident.invoke({"number": number}))