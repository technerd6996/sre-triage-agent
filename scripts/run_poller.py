"""Run from the project root:  python -m scripts.run_poller
Polls the L1 queue and runs the agent on each new ticket. Approvals are asked in this terminal.
Needs the Colab session and ngrok URL alive. Ctrl+C to stop."""
import time

from dotenv import load_dotenv

load_dotenv()

from app.agent.agent import resume, start
from app.tools.snow import list_queue


class QueuePoller:
    """Hands out each queued ticket once per session."""
    def __init__(self):
        self.tried = set()

    def next_ticket(self):
        for number in list_queue():
            if number not in self.tried:
                self.tried.add(number)
                return number
        return None

INTERVAL = 15  # seconds


def drive(events):
    thread_id, pending = None, None
    for ev in events:
        t = ev["type"]
        if t == "start":
            thread_id = ev["thread_id"]
            print(f"[start]  {ev['ticket']} skill={ev['skill']} server={ev['server']}")
        elif t == "tool_call":
            print(f"[call]   {ev['name']} {ev['args']}")
        elif t == "tool_result":
            print(f"[result] {ev['name']}: {ev['content'][:200]}")
        elif t == "approval_required":
            pending = ev
        elif t in ("final", "nudge"):
            print(f"[{t}]  {ev['text']}")
        elif t in ("warning", "error"):
            print(f"[{t.upper()}] {ev['message']}")
    return thread_id, pending


poller = QueuePoller()
print(f"Polling every {INTERVAL}s. Ctrl+C to stop.")
while True:
    try:
        number = poller.next_ticket()
    except Exception as e:
        print("[poll error]", str(e)[:200])
        number = None
    if number:
        print(f"\n=== New ticket {number} ===")
        thread_id, pending = drive(start(number))
        while pending:
            for a in pending["actions"]:
                print(f">>> APPROVAL NEEDED: {a['name']} {a['args']}")
            ok = input("Approve? (y/n): ").strip().lower() == "y"
            _, pending = drive(resume(thread_id, ok))
    else:
        time.sleep(INTERVAL)