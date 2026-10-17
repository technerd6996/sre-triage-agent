"""Run from the project root:  python -m scripts.test_p0

Checks the P0 pieces. Needs .env (Supabase) and sql/p0_metrics.sql applied.
Writes rows with run_id 'p0-selftest' and removes them again.
"""
import sys
import threading
import time

from dotenv import load_dotenv

load_dotenv()

from app import store                      # noqa: E402
from app.agent.skills import _kw_match     # noqa: E402
from app.llm import usage                  # noqa: E402
from app.llm.gate import LLMBusy, llm_slot  # noqa: E402

RUN = "p0-selftest"
failures = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global failures
    failures += 0 if ok else 1
    print(("PASS  " if ok else "FAIL  ") + name + (f"   {extra}" if extra else ""))


# 1. skill keyword matching (L-6)
check("keyword 'c' no longer matches every ticket", not _kw_match("c", "print spooler stopped"))
check("keyword 'c' still matches 'C: drive is full'", _kw_match("c", "c: drive is full"))
check("plural still matches", _kw_match("service", "two services are down"))
check("part of a longer word does not match", not _kw_match("ping", "shipping label"))

# 2. gate: priority order and busy timeout
order = []


def worker(kind: str) -> None:
    with llm_slot(kind, wait_timeout=None):
        order.append(kind)


with llm_slot("triage"):
    t1 = threading.Thread(target=worker, args=("chat",)); t1.start(); time.sleep(0.3)
    t2 = threading.Thread(target=worker, args=("resume",)); t2.start(); time.sleep(0.3)
t1.join(); t2.join()
check("resume goes before chat when both are waiting", order == ["resume", "chat"], str(order))

busy = False
with llm_slot("triage"):
    try:
        with llm_slot("chat", wait_timeout=0.3):
            pass
    except LLMBusy:
        busy = True
check("chat gives up with LLMBusy when the model stays busy", busy)

# 3. token counter
state = usage.warm()
n = usage.count_tokens("The nginx service on victim-test is down")
check(f"token counter ({'Qwen tokenizer' if state == 'ok' else 'estimate: tokenizer unavailable'})",
      n > 0, f"{n} tokens")

# 4. llm_calls round trip
store.add_llm_call("triage", RUN, 100, 20, 1.5, 0.2)
store.add_llm_call("resume", RUN, 50, 10, 0.5, 0.0)
tot = store.llm_totals(RUN)
check("llm_calls insert and totals",
      tot == {"tokens_in": 150, "tokens_out": 30, "llm_calls": 2}, str(tot))
store.supabase.table("llm_calls").delete().eq("run_id", RUN).execute()

# 5. new runs columns
store.save_run({"id": RUN, "ticket": "INC0000000", "status": "done", "steps": 3,
                "tokens_in": 150, "tokens_out": 30, "requester": "selftest"})
row = store.supabase.table("runs").select("steps,tokens_in,requester").eq("id", RUN).execute().data
check("runs table has the new columns", bool(row) and row[0]["steps"] == 3, str(row))
store.supabase.table("runs").delete().eq("id", RUN).execute()

print("\nALL PASS" if not failures else f"\n{failures} FAILED")
sys.exit(1 if failures else 0)