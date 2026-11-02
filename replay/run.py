"""Replay harness: runs the real triage agent on simulated tickets and scores the result.

  python -m replay.run --check                         offline: validate the cases, no model needed
  python -m replay.run --label qwen7b-slots1 --repeat 3
  python -m replay.run --label x --case win-spooler-fixable --tag windows
  python -m replay.run compare replay/results/a.json replay/results/b.json

Model calls are saved to llm_calls with kind "replay", so dashboards can leave them out.
Note: task_message() mirrors the text agent.start() builds. If start() changes, update it here.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CASES = HERE / "cases.json"
RESULTS = HERE / "results"


def load_cases(path=CASES) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def task_message(case: dict):
    """(message, skill, allowed tool names), built the same way agent.start() builds it."""
    from app.agent import skills
    short, desc = case["short_description"], case.get("description", "")
    host = case.get("host")
    platform = host["platform"] if host else None
    playbook, skill, allowed, _ = skills.build_playbook(f"{short} {desc}", platform)
    if host:
        server = (f'Affected server: {host["name"]} ({platform}, running). '
                  f'Use target "{platform}" for all server tools.')
    else:
        server = ("Affected server: unknown. No server was named in the ticket. "
                  "Call claim_incident, add_work_note, and escalate_incident giving that as the reason.")
        allowed = {"claim_incident", "get_incident", "add_work_note", "escalate_incident"}
    number = case.get("number", "INC9990001")
    msg = (f"Work incident {number}.\nShort description: {short}\nDescription: {desc}\n"
           f"{server}\n\nPLAYBOOK:\n{playbook}")
    return msg, skill, set(allowed)


def _drain(gen, events: list):
    """Collect a generator's events and return its return value (the run status)."""
    try:
        while True:
            events.append(next(gen))
    except StopIteration as e:
        return e.value


def grade(case: dict, world, names: list[str], outcome: str):
    exp = case["expect"]
    problems = []
    if outcome != exp["outcome"]:
        problems.append(f"outcome {outcome}, expected {exp['outcome']}")
    for t in exp.get("must_call", []):
        if t not in names:
            problems.append(f"never called {t}")
    for t in exp.get("must_not_call", []):
        if t in names:
            problems.append(f"called {t}")
    for t, n in (exp.get("max_calls") or {}).items():
        if names.count(t) > n:
            problems.append(f"called {t} {names.count(t)} times (max {n})")
    false_resolve = outcome == "resolved" and not world.truth_at_close
    if false_resolve:
        problems.append("false resolve: ticket closed but the problem was not fixed")
    return (not problems), problems, false_resolve

def score_checkers(world, skill, events, run_id) -> dict:
    """After the agent resolved, run both checkers against the world and compare with the ground truth."""
    from unittest import mock
    from app import checker
    run = {"id": run_id, "ticket": world.number, "skill": skill, "events": events,
           "server": (world.case.get("host") or {}).get("name")}
    code, spec = "unverified", checker._spec(run)
    with mock.patch.object(checker, "_soak", lambda: 0):       # no 60s soak in replay
        if spec:
            ok, _ = checker._run_checks(*spec)
            code = "pass" if ok is True else ("fail" if ok is False else "unverified")
        llm, _ = checker._run_llm_checker(run)
    return {"truth": "pass" if world.goal_met() else "fail", "code": code, "llm": llm}

def execute(case: dict, rep: int, captured: list) -> dict:
    from langchain.agents import create_agent
    from langchain.agents.middleware import HumanInTheLoopMiddleware
    from langgraph.types import Command
    try:
        from langgraph.checkpoint.memory import InMemorySaver
    except ImportError:
        from langgraph.checkpoint.memory import MemorySaver as InMemorySaver

    from app import config
    from app.agent import agent as A
    from app.agent.policy import interrupt_config
    from app.llm import usage
    from app.llm.colab_chat import ColabChat
    from replay.world import World, all_tools, patched

    base = {"case": case["id"], "rep": rep}
    msg, skill, allowed = task_message(case)
    tools = [t for t in all_tools() if t.name in allowed]
    missing = sorted(allowed - {t.name for t in tools})
    if missing:
        return {**base, "status": "skipped", "reason": f"tools not simulated: {missing}"}

    world = World(case)
    run_id = f"replay-{case['id']}-{rep}-{uuid.uuid4().hex[:4]}"
    model = config.get("model.name")
    usage.bind_run(run_id, kind="replay", skill=skill, ticket=world.number, model=model)
    captured.clear()
    events: list = []
    nudges = approvals = 0
    t0 = time.time()
    status = "done"
    chk = None
    try:
        with patched(world):
            agent = create_agent(
                model=ColabChat(url=os.getenv("COLAB_API_URL")), tools=tools, system_prompt=A.BASE_PROMPT,
                middleware=[HumanInTheLoopMiddleware(interrupt_on=interrupt_config(tools))],
                checkpointer=InMemorySaver())
            cfg = {"configurable": {"thread_id": run_id}, "recursion_limit": 30}
            payload = {"messages": [{"role": "user", "content": msg}]}
            for _ in range(25):                                   # hard stop against endless loops
                status = _drain(A._run(agent, payload, cfg), events)
                if status == "error":
                    break
                if status == "paused":
                    approvals += 1
                    n = len(events[-1].get("actions") or [1])
                    d = ({"type": "approve"} if case.get("approval", "approve") == "approve"
                         else {"type": "reject", "message": "Rejected by the engineer."})
                    payload = Command(resume={"decisions": [d] * n})
                    continue
                if world.closed() or nudges >= A.MAX_NUDGES:
                    break
                nudges += 1
                payload = {"messages": [{"role": "user", "content": A.NUDGE}]}
            if os.getenv("REPLAY_SCORE_CHECKERS") and world.outcome == "resolved":
                chk = score_checkers(world, skill, events, run_id)
    finally:
        usage.unbind_run(run_id)
    wall = time.time() - t0

    calls = [{"name": e["name"], "args": e["args"]} for e in events if e["type"] == "tool_call"]
    names = [c["name"] for c in calls]
    tool_errors = sum(1 for e in events if e["type"] == "tool_result"
                      and ('"error"' in e["content"] or e["content"].startswith("Error")))
    outcome = "error" if status == "error" else (world.outcome or "stuck")
    ok, problems, false_resolve = grade(case, world, names, outcome)
    ttfts = [m["ttft_s"] for m in captured if m.get("ttft_s")]
    return {
        **base, "status": "ok", "model": model, "skill": skill, "outcome": outcome, "passed": ok,
        "problems": problems, "false_resolve": false_resolve, "stuck": outcome == "stuck",
        "later_failure": bool(case.get("after_resolve")) and outcome == "resolved" and not world.goal_met(),
        "calls": names, "tool_errors": tool_errors, "nudges": nudges, "approvals": approvals,
        "error": next((e["message"] for e in events if e["type"] == "error"), None),
        "llm_calls": len(captured),
        "tokens_in": sum(m.get("tokens_in", 0) for m in captured),
        "tokens_out": sum(m.get("tokens_out", 0) for m in captured),
        "wasted_tokens": sum(m.get("wasted_tokens", 0) for m in captured),
        "gen_s": round(sum(m.get("gen_s", 0) for m in captured), 2),
        "wait_s": round(sum(m.get("wait_s", 0) for m in captured), 2),
        "ttft_avg_s": round(sum(ttfts) / len(ttfts), 3) if ttfts else None,
        "wall_s": round(wall, 2), "world_after": world.snapshot(), "checker": chk,
    }


# ---------------- summaries ----------------
def _pct(vals: list[float], p: float):
    if not vals:
        return None
    v = sorted(vals)
    return round(v[round(p * (len(v) - 1))], 2)


def _avg(vals: list):
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def summarise(runs: list[dict]) -> dict:
    done = [r for r in runs if r["status"] == "ok"]
    n = len(done)
    per_case = {}
    for r in done:
        c = per_case.setdefault(r["case"], {"runs": 0, "passed": 0})
        c["runs"] += 1
        c["passed"] += 1 if r["passed"] else 0
    return {
        "runs": n, "skipped": len(runs) - n,
        "pass_rate": round(sum(r["passed"] for r in done) / n, 3) if n else None,
        "false_resolves": sum(r["false_resolve"] for r in done),
        "stuck": sum(r["stuck"] for r in done),
        "errors": sum(1 for r in done if r["outcome"] == "error"),
        "avg_calls": _avg([len(r["calls"]) for r in done]),
        "avg_llm_calls": _avg([r["llm_calls"] for r in done]),
        "avg_tokens_in": _avg([r["tokens_in"] for r in done]),
        "avg_tokens_out": _avg([r["tokens_out"] for r in done]),
        "avg_wasted_tokens": _avg([r["wasted_tokens"] for r in done]),
        "avg_tool_errors": _avg([r["tool_errors"] for r in done]),
        "avg_ttft_s": _avg([r["ttft_avg_s"] for r in done]),
        "wall_p50_s": _pct([r["wall_s"] for r in done], 0.5),
        "wall_p95_s": _pct([r["wall_s"] for r in done], 0.95),
        "checker_runs": sum(1 for r in done if r.get("checker")),
        "code_wrong": sum(1 for r in done if r.get("checker") and r["checker"]["code"] in ("pass", "fail")
                          and r["checker"]["code"] != r["checker"]["truth"]),
        "llm_wrong": sum(1 for r in done if r.get("checker") and r["checker"]["llm"] in ("pass", "fail")
                         and r["checker"]["llm"] != r["checker"]["truth"]),
        "llm_unverified": sum(1 for r in done if r.get("checker") and r["checker"]["llm"] not in ("pass", "fail")),
        "per_case": per_case,
    }


def print_summary(label: str, s: dict) -> None:
    print(f"\n== {label} ==")
    for k, v in s.items():
        if k != "per_case":
            print(f"  {k:<20} {v}")
    print("\n  case                               passed")
    for cid, c in sorted(s["per_case"].items()):
        print(f"  {cid:<34} {c['passed']}/{c['runs']}")


def compare(a_path: str, b_path: str) -> None:
    a, b = (json.loads(Path(p).read_text(encoding="utf-8")) for p in (a_path, b_path))
    sa, sb = a["summary"], b["summary"]
    print(f"\n{'metric':<22}{a['label']:>22}{b['label']:>22}{'change':>12}")
    for k in sa:
        if k == "per_case":
            continue
        x, y = sa[k], sb.get(k)
        delta = f"{y - x:+.2f}" if isinstance(x, (int, float)) and isinstance(y, (int, float)) else ""
        print(f"{k:<22}{str(x):>22}{str(y):>22}{delta:>12}")
    print(f"\n{'case':<34}{a['label']:>16}{b['label']:>16}")
    for cid in sorted(set(sa["per_case"]) | set(sb["per_case"])):
        f = lambda s: (lambda c: f"{c['passed']}/{c['runs']}" if c else "-")(s["per_case"].get(cid))
        print(f"{cid:<34}{f(sa):>16}{f(sb):>16}")


# ---------------- entry points ----------------
def check(cases: list[dict]) -> int:
    """Offline: every case must pick the playbook it expects and use only simulated tools."""
    from replay.world import all_tools
    have = {t.name for t in all_tools()}
    bad = 0
    for c in cases:
        _, skill, allowed = task_message(c)
        want = c["expect"].get("skill", "*")
        problems = []
        if want != "*" and (skill or None) != want:
            problems.append(f"playbook is {skill}, expected {want}")
        if allowed - have:
            problems.append(f"tools not simulated: {sorted(allowed - have)}")
        print(f"{'FAIL' if problems else 'ok  '} {c['id']:<34} {skill or '-':<22} {'; '.join(problems)}")
        bad += bool(problems)
    print(f"\n{len(cases) - bad}/{len(cases)} cases valid")
    return 1 if bad else 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "compare":
        compare(sys.argv[2], sys.argv[3])
        return 0
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", help="name for this run, e.g. qwen7b-slots1")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--case", action="append", help="run only this case id (repeatable)")
    ap.add_argument("--tag", help="run only cases with this tag")
    ap.add_argument("--check", action="store_true", help="validate cases offline, no model")
    ap.add_argument("--cases", default=str(CASES))
    args = ap.parse_args()

    os.environ["SKILLS_SOURCE"] = "file"
    os.environ["SNOW_ASSIGNMENT_GROUP"] = "L1-SRE-Agent"
    cases = load_cases(args.cases)
    if args.case:
        cases = [c for c in cases if c["id"] in args.case]
    if args.tag:
        cases = [c for c in cases if args.tag in c.get("tags", [])]
    if args.check:
        return check(cases)
    if not args.label:
        ap.error("--label is required for a real run")

    try:
        from app import config
        config.hydrate_environ()
    except Exception as e:
        print(f"note: saved settings not loaded ({str(e)[:100]})")
    if not os.getenv("COLAB_API_URL"):
        print("COLAB_API_URL is not set: save the Model API URL in the Configuration pane or .env")
        return 2

    from app.llm import usage
    captured: list = []
    original = usage.log_call

    def tap(metrics: dict) -> None:
        captured.append(dict(metrics))
        original(metrics)
    usage.log_call = tap

    runs, dead = [], 0
    try:
        for rep in range(1, args.repeat + 1):
            for case in cases:
                r = execute(case, rep, captured)
                runs.append(r)
                if r["status"] == "skipped":
                    print(f"skip {case['id']}: {r['reason']}")
                    continue
                print(f"{'PASS' if r['passed'] else 'FAIL'} {case['id']:<34} rep {rep}  {r['outcome']:<10}"
                      f" calls={len(r['calls'])} tok={r['tokens_in']}/{r['tokens_out']} {r['wall_s']}s"
                      + (f"  <- {'; '.join(r['problems'])}" if r["problems"] else ""))
                if r.get("error"):
                    print(f"     error: {r['error'][:300]}")
                dead = dead + 1 if (r["outcome"] == "error" and r["llm_calls"] == 0) else 0
                if dead >= 3:
                    print("\nThe model did not answer 3 runs in a row. Stopping. Is it online?")
                    return 2
    finally:
        usage.log_call = original

    summary = summarise(runs)
    print_summary(args.label, summary)
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{args.label}-{datetime.now():%Y%m%d-%H%M%S}.json"
    from app import config as cfg
    settings = {k: cfg.get(k) for k in ("model.name", "model.temperature", "model.max_tokens", "model.slots")}
    out.write_text(json.dumps({"label": args.label, "settings": settings, "summary": summary, "runs": runs},
                              indent=1, default=str), encoding="utf-8")
    print(f"\nSaved {out}")
    return 0
 

if __name__ == "__main__":
    sys.exit(main())