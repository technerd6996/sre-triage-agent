"""Probe what the checker account may do on one ticket.
Run: python -m scripts.probe_checker INC0010100          (work note only)
     python -m scripts.probe_checker INC0010100 --close  (also tries Resolved -> Closed)"""
import sys
from dotenv import load_dotenv
load_dotenv()
from app import checker

number = sys.argv[1]
sys_id, state = checker._ticket(number)
print("state as checker:", state)


def attempt(label, body):
    try:
        checker._snow("PATCH", f"incident/{sys_id}", json=body)
        print(f"OK     {label}")
    except Exception as e:
        print(f"DENIED {label}: {str(e)[:160]}")


attempt("work note only", {"work_notes": "probe: checker write test"})
if "--close" in sys.argv:
    attempt("state to Closed", {"state": checker.STATE_CLOSED, "work_notes": "probe: close test"})