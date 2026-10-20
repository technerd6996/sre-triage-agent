from fastapi import APIRouter, Request
from pydantic import BaseModel
from app import store

router = APIRouter()


class ChatSwitch(BaseModel):
    enabled: bool


@router.get("/api/chat/status")                      # any logged-in user: lets the UI know chat is off
def chat_status():
    return {"enabled": bool(store.get_setting("chat_enabled", True)),
            "ticket_mode": store.get_setting("ticket_mode", "confirm")}


@router.post("/api/admin/chat")                      # /api/admin/* is admin-only in required_role
def set_chat(req: ChatSwitch, request: Request):
    who = getattr(request.state, "user", "unknown")
    store.set_setting("chat_enabled", req.enabled, who)
    store.audit(None, None, who, "chat_on" if req.enabled else "chat_off")
    return {"enabled": req.enabled}

class Autonomy(BaseModel):
    mode: str


@router.get("/api/admin/autonomy")
def get_autonomy():
    return {"mode": store.get_setting("autonomy_mode", "shadow")}


@router.post("/api/admin/autonomy")
def set_autonomy(req: Autonomy, request: Request):
    from fastapi import HTTPException
    if req.mode not in ("off", "shadow", "on"):
        raise HTTPException(422, "mode must be off, shadow or on")
    who = getattr(request.state, "user", "unknown")
    store.set_setting("autonomy_mode", req.mode, who)
    store.audit(None, None, who, "autonomy_mode", req.mode)
    return {"mode": req.mode}

class P1Settings(BaseModel):
    bridge_link: str = ""
    to: list[str] = []
    cc: list[str] = []


@router.get("/api/admin/p1")
def get_p1():
    return {"bridge_link": store.get_setting("bridge_link", ""), "to": store.get_setting("p1_notice_to", []),
            "cc": store.get_setting("p1_notice_cc", [])}


@router.post("/api/admin/p1")
def set_p1(req: P1Settings, request: Request):
    from fastapi import HTTPException
    from app.escalation import EMAIL
    bad = [a for a in req.to + req.cc if not EMAIL.match(a)]
    if bad:
        raise HTTPException(422, f"Invalid email: {', '.join(bad)}")
    if req.bridge_link and not req.bridge_link.startswith("https://"):
        raise HTTPException(422, "Bridge link must start with https://")
    who = getattr(request.state, "user", "unknown")
    store.set_setting("bridge_link", req.bridge_link, who)
    store.set_setting("p1_notice_to", req.to, who)
    store.set_setting("p1_notice_cc", req.cc, who)
    store.audit(None, None, who, "p1_settings_changed")
    return {"ok": True}

class TicketMode(BaseModel):
    mode: str


@router.get("/api/admin/ticket-mode")
def get_ticket_mode():
    return {"mode": store.get_setting("ticket_mode", "confirm")}


@router.post("/api/admin/ticket-mode")
def set_ticket_mode(req: TicketMode, request: Request):
    from fastapi import HTTPException
    if req.mode not in ("confirm", "auto"):
        raise HTTPException(422, "mode must be confirm or auto")
    who = getattr(request.state, "user", "unknown")
    store.set_setting("ticket_mode", req.mode, who)
    store.audit(None, None, who, "ticket_mode", req.mode)
    return {"mode": req.mode}

import csv, io, re
import time
from fastapi import HTTPException
from fastapi.responses import Response as _Resp
from passlib.context import CryptContext
from app.auth import forget_user

_pwd = CryptContext(schemes=["bcrypt"])
_ROLES = ("viewer", "approver", "admin")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_db = store.supabase


class NewUser(BaseModel):
    user_id: str
    name: str
    role: str = "viewer"
    password: str


class UserPatch(BaseModel):
    role: str | None = None
    status: str | None = None
    password: str | None = None


@router.get("/api/admin/users")
def list_users():
    return _db.table("users").select("user_id,name,role,type,status,last_login").order("user_id").execute().data


@router.post("/api/admin/users")
def add_user(req: NewUser, request: Request):
    uid = req.user_id.strip().lower()
    if not _EMAIL.match(uid):
        raise HTTPException(422, "User id must be an email address")
    if req.role not in _ROLES:
        raise HTTPException(422, "Invalid role")
    if len(req.password) < 8:
        raise HTTPException(422, "Password must be at least 8 characters")
    if _db.table("users").select("user_id").eq("user_id", uid).execute().data:
        raise HTTPException(409, "User already exists")
    _db.table("users").insert({"user_id": uid, "name": req.name.strip() or uid.split("@")[0],
                               "role": req.role, "type": "human", "status": "active",
                               "password_hash": _pwd.hash(req.password)}).execute()
    store.audit(None, None, getattr(request.state, "user", "?"), "user_created", f"{uid} as {req.role}")
    return {"ok": True}


@router.patch("/api/admin/users/{uid}")
def patch_user(uid: str, req: UserPatch, request: Request):
    who = getattr(request.state, "user", "?")
    rows = _db.table("users").select("user_id,role,status").eq("user_id", uid).execute().data
    if not rows:
        raise HTTPException(404, "No such user")
    cur, patch = rows[0], {}
    if req.role is not None:
        if req.role not in _ROLES:
            raise HTTPException(422, "Invalid role")
        patch["role"] = req.role
    if req.status is not None:
        if req.status not in ("active", "disabled"):
            raise HTTPException(422, "Invalid status")
        patch["status"] = req.status
    if req.password is not None:
        if len(req.password) < 8:
            raise HTTPException(422, "Password must be at least 8 characters")
        patch["password_hash"] = _pwd.hash(req.password)
    if not patch:
        raise HTTPException(422, "Nothing to change")
    losing_admin = cur["role"] == "admin" and cur["status"] == "active" and (
        patch.get("role", "admin") != "admin" or patch.get("status", "active") != "active")
    if losing_admin:
        others = _db.table("users").select("user_id").eq("role", "admin").eq("status", "active").neq("user_id", uid).execute().data
        if not others:
            raise HTTPException(409, "The last active admin cannot be demoted or disabled")
    _db.table("users").update(patch).eq("user_id", uid).execute()
    if patch.get("status") == "disabled" or "role" in patch:
        _db.table("sessions").delete().eq("user_id", uid).execute() if patch.get("status") == "disabled" else None
        forget_user(uid)
    store.audit(None, None, who, "user_changed", f"{uid}: " + ", ".join(k for k in patch if k != "password_hash")
                + (" password_reset" if "password_hash" in patch else ""))
    return {"ok": True}


def _audit_rows(actor, action, ticket, days, limit):
    q = _db.table("audit").select("*").order("ts", desc=True).limit(min(limit, 5000))
    if actor:
        q = q.ilike("actor", f"%{actor}%")
    if action:
        q = q.ilike("action", f"%{action}%")
    if ticket:
        q = q.ilike("ticket", f"%{ticket}%")
    if days:
        q = q.gte("ts", time.time() - days * 86400)
    return q.execute().data


@router.get("/api/admin/audit")
def audit_list(actor: str = "", action: str = "", ticket: str = "", days: int = 7, limit: int = 200):
    return _audit_rows(actor, action, ticket, days, limit)


@router.get("/api/admin/audit.csv")
def audit_csv(actor: str = "", action: str = "", ticket: str = "", days: int = 7):
    rows = _audit_rows(actor, action, ticket, days, 5000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time", "actor", "action", "ticket", "run_id", "detail"])
    for r in rows:
        w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])), r["actor"], r["action"],
                    r.get("ticket") or "", r.get("run_id") or "", r.get("detail") or ""])
    return _Resp(buf.getvalue(), media_type="text/csv",
                 headers={"Content-Disposition": "attachment; filename=audit.csv"})


@router.get("/api/admin/runs")
def admin_runs(outcome: str = "", limit: int = 50):
    q = _db.table("runs").select("id,ticket,status,skill,server,outcome,started,ended,steps,nudges,tokens_in,tokens_out,requester") \
        .order("created", desc=True).limit(min(limit, 200))
    if outcome:
        q = q.eq("outcome", outcome)
    return q.execute().data


def _p90(v):
    v = sorted(v)
    return round(v[int(0.9 * (len(v) - 1))], 1) if v else None


@router.get("/api/admin/bot-stats")
def bot_stats(days: int = 7):
    cut = time.time() - days * 86400
    runs = _db.table("runs").select("skill,outcome,started,ended,steps,nudges,status").gte("created", cut).execute().data
    done = [r for r in runs if r["status"] not in ("running", "awaiting_approval")]
    n = len(done) or 1
    cnt: dict = {}
    for r in done:
        cnt[r.get("outcome") or "none"] = cnt.get(r.get("outcome") or "none", 0) + 1
    mttr = [(r["ended"] - r["started"]) / 60 for r in done
            if r.get("outcome") == "resolved" and r.get("started") and r.get("ended")]
    skills: dict = {}
    for r in runs:
        k = r.get("skill") or "(no playbook)"
        skills[k] = skills.get(k, 0) + 1
    appr = _db.table("approvals").select("requested_at,decided_at,decided_by,decision,tool") \
        .gte("requested_at", cut).execute().data
    waits = [(a["decided_at"] - a["requested_at"]) / 60 for a in appr if a.get("decided_at")]
    by_who: dict = {}
    for a in appr:
        if a.get("decided_at"):
            s = by_who.setdefault(a.get("decided_by") or "?", {"approved": 0, "rejected": 0})
            s["approved" if a.get("decision") else "rejected"] += 1
    au = _db.table("audit").select("action,detail,ticket").gte("ts", cut) \
        .in_("action", ["run_started", "ticket_auto_created", "ticket_undone"]).execute().data
    made = {a["ticket"] for a in au if a["ticket"] and (a["action"] == "ticket_auto_created"
            or (a["action"] == "run_started" and "source=chat" in (a.get("detail") or "")))}
    undone = {a["ticket"] for a in au if a["action"] == "ticket_undone" and a["ticket"]}
    checker = None
    try:
        v = _db.table("verifications").select("verdict").order("ts", desc=True).limit(500).execute().data
        checker = {}
        for x in v:
            checker[x["verdict"]] = checker.get(x["verdict"], 0) + 1
    except Exception:
        pass
    return {"runs": len(runs), "finished": len(done), "outcomes": cnt,
            "resolved_pct": round(100 * cnt.get("resolved", 0) / n),
            "escalated_pct": round(100 * cnt.get("escalated", 0) / n),
            "failed_pct": round(100 * sum(cnt.get(k, 0) for k in ("incomplete", "error", "interrupted")) / n),
            "mttr_min": round(sum(mttr) / len(mttr), 1) if mttr else None,
            "avg_steps": round(sum(r.get("steps") or 0 for r in done) / n, 1),
            "avg_nudges": round(sum(r.get("nudges") or 0 for r in done) / n, 1),
            "skills": skills, "approval_wait_avg_min": round(sum(waits) / len(waits), 1) if waits else None,
            "approval_wait_p90_min": _p90(waits), "approvers": by_who, "checker": checker,
            "chat_tickets": len(made), "chat_undone": len(undone)}

@router.get("/api/admin/rag-stats")
def rag_stats(days: int = 30):
    _db.table("rag_events").delete().lt("ts", time.time() - int(store.get_setting("rag_retention_days", 30)) * 86400).execute()
    rows = _db.table("rag_events").select("*").gte("ts", time.time() - days * 86400).execute().data
    kinds, gaps, docs, phantom = {}, {}, {}, 0
    for r in rows:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
        for d in r.get("retrieved") or []:
            docs[d] = docs.get(d, 0) + 1
        phantom += 1 if r.get("phantom") else 0
        if r["kind"] == "general":
            k = (r.get("query") or "").lower()[:120]
            gaps[k] = gaps.get(k, 0) + 1
    real = [r for r in rows if r["kind"] in ("kb", "general")]
    tot = [r["t_total"] for r in rows if r.get("t_total")]
    return {"queries": len(rows), "kinds": kinds, "phantom": phantom,
            "hit_rate": round(100 * kinds.get("kb", 0) / len(real)) if real else None,
            "gaps": sorted(gaps.items(), key=lambda x: -x[1])[:15],
            "top_docs": sorted(docs.items(), key=lambda x: -x[1])[:10],
            "avg_total": round(sum(tot) / len(tot), 1) if tot else None, "p90_total": _p90(tot),
            "avg_graph": round(sum(r["t_graph"] or 0 for r in rows) / len(rows), 2) if rows else None,
            "avg_vec": round(sum(r["t_vec"] or 0 for r in rows) / len(rows), 2) if rows else None,
            "recent_scores": [{"q": r["query"], "scores": r.get("scores")} for r in rows[-5:]]}

@router.get("/api/admin/summary")
def summary(days: int = 7):
    days = max(1, min(days, 30))
    now = time.time()
    cut, prev = now - days * 86400, now - 2 * days * 86400
    rows = _db.table("runs").select("ticket,skill,outcome,status,started,ended,created").gte("created", prev).execute().data

    def part(rs):
        done = [r for r in rs if r["status"] not in ("running", "awaiting_approval")]
        res = [r for r in done if r.get("outcome") == "resolved"]
        mt = [(r["ended"] - r["started"]) / 60 for r in res if r.get("started") and r.get("ended")]
        return {"handled": len(rs), "finished": len(done), "resolved": len(res),
                "escalated": sum(1 for r in done if r.get("outcome") == "escalated"),
                "mttr_min": round(sum(mt) / len(mt), 1) if mt else None}

    def latest(rs):                      # one row per ticket: its most recent run
        d = {}
        for r in sorted(rs, key=lambda r: r["created"]):
            d[r["ticket"]] = r
        return list(d.values())

    cur = latest([r for r in rows if r["created"] >= cut])
    c, p = part(cur), part(latest([r for r in rows if r["created"] < cut]))
    mins = store.get_setting("manual_minutes_per_ticket", 20)
    daily = {}
    for r in sorted(cur, key=lambda r: r["created"]):
        k = time.strftime("%a %d", time.localtime(r["created"]))
        daily[k] = daily.get(k, 0) + 1
    skills = {}
    for r in cur:
        k = r.get("skill") or "No playbook yet"
        skills[k] = skills.get(k, 0) + 1
    attention = [{"ticket": r["ticket"], "outcome": r.get("outcome") or r["status"], "skill": r.get("skill")}
                 for r in sorted(cur, key=lambda r: -r["created"])
                 if (r.get("outcome") or r["status"]) in ("escalated", "error", "incomplete", "interrupted")][:5]
    try:
        pending = len(_db.table("approvals").select("id").is_("decided_at", "null").execute().data)
    except Exception:
        pending = None
    chk = None
    try:
        v = _db.table("verifications").select("verdict").order("ts", desc=True).limit(200).execute().data
        chk = round(100 * sum(1 for x in v if str(x["verdict"]).endswith("pass")) / len(v)) if v else None
    except Exception:
        pass
    kb = None
    try:
        rg = _db.table("rag_events").select("kind").gte("ts", cut).execute().data
        real = [x for x in rg if x["kind"] in ("kb", "general")]
        kb = round(100 * sum(1 for x in real if x["kind"] == "kb") / len(real)) if real else None
    except Exception:
        pass
    net = max(0, mins - (c["mttr_min"] or 0))
    return {"days": days, "cur": c, "prev": p, "hours_saved": round(c["resolved"] * net / 60, 1), "manual_minutes": mins,
            "resolved_pct": round(100 * c["resolved"] / c["finished"]) if c["finished"] else None,
            "daily": daily, "skills": sorted(skills.items(), key=lambda x: -x[1])[:5],
            "attention": attention, "pending": pending, "checker_pass": chk, "kb_rate": kb}

@router.get("/api/admin/api-health")
def api_health(request: Request):
    import requests as _rq
    from concurrent.futures import ThreadPoolExecutor
    skip = {"/api/admin/api-health", "/api/ready", "/api/admin/audit.csv"}   # ready is covered by the dependency checks
    paths = sorted({r.path for r in request.app.routes
                    if "GET" in (getattr(r, "methods", None) or ())
                    and r.path.startswith("/api/") and "{" not in r.path and r.path not in skip})
    base, ck = str(request.base_url).rstrip("/"), request.cookies

    def probe(p):
        t = time.time()
        try:
            r = _rq.get(base + p, cookies=ck, timeout=8)
            return {"path": p, "ok": r.status_code < 500, "status": r.status_code, "ms": int((time.time() - t) * 1000)}
        except Exception as e:
            return {"path": p, "ok": False, "status": None, "ms": int((time.time() - t) * 1000), "error": str(e)[:80]}

    with ThreadPoolExecutor(8) as ex:
        return {"results": list(ex.map(probe, paths))}

@router.post("/api/admin/runs/{run_id}/cancel")
def admin_cancel_run(run_id: str, request: Request):
    from app.api.agent import cancel_run
    res = cancel_run(run_id, getattr(request.state, "user", "?"))
    if res == "missing":
        raise HTTPException(404, "Run is no longer held in memory (only the last 20 runs can be cancelled)")
    if res == "finished":
        raise HTTPException(409, "This run has already finished")
    return {"ok": True}


@router.post("/api/admin/runs/{run_id}/rerun")
def admin_rerun(run_id: str, request: Request):
    from app.api.agent import start_run
    rows = _db.table("runs").select("ticket").eq("id", run_id).execute().data
    if not rows:
        raise HTTPException(404, "No such run")
    who = getattr(request.state, "user", "?")
    from app import jobs
    job, pos = jobs.enqueue(rows[0]["ticket"], "rerun", who, 2)
    return {"queued": True, "position": pos}


_OPS = {"manual_minutes_per_ticket": (1, 600, 20), "approval_timeout_min": (1, 240, 30), "rag_retention_days": (7, 365, 30)}


@router.get("/api/admin/settings")
def get_ops_settings():
    return {k: store.get_setting(k, d) for k, (lo, hi, d) in _OPS.items()}


@router.post("/api/admin/settings")
def set_ops_settings(body: dict, request: Request):
    clean = {}
    for k, v in body.items():
        if k not in _OPS:
            raise HTTPException(422, f"Unknown setting {k}")
        lo, hi, _ = _OPS[k]
        try:
            v = int(v)
        except (TypeError, ValueError):
            raise HTTPException(422, f"{k} must be a number")
        if not lo <= v <= hi:
            raise HTTPException(422, f"{k} must be between {lo} and {hi}")
        clean[k] = v
    who, changed = getattr(request.state, "user", "?"), []
    for k, v in clean.items():
        old = store.get_setting(k, _OPS[k][2])
        if old != v:
            store.set_setting(k, v, who)
            changed.append(f"{k}: {old} -> {v}")
    if changed:
        store.audit(None, None, who, "settings_changed", "; ".join(changed))
    return {"ok": True, "changed": len(changed)}

@router.get("/api/admin/jobs")
def admin_jobs():
    from app import jobs
    return sorted(jobs.queued(), key=lambda r: (r["priority"], r["created"]))


@router.post("/api/admin/jobs/{job_id}/cancel")
def admin_cancel_job(job_id: str, request: Request):
    from app import jobs
    jobs.set_status(job_id, "cancelled")
    store.audit(None, None, getattr(request.state, "user", "?"), "job_cancelled", job_id)
    return {"ok": True}