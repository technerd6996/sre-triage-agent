import hashlib
import os
import secrets
import time
from collections import defaultdict, deque
from fastapi import APIRouter, Response, Request, HTTPException
from pydantic import BaseModel
from app.auth import forget_session
from app.store import supabase, audit
from passlib.context import CryptContext

router = APIRouter(prefix="/api/auth")
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

class LoginRequest(BaseModel):
    username: str
    password: str

MAX_FAILS, FAIL_WINDOW = 5, 300          # 5 bad logins per ACCOUNT in 5 minutes
MAX_IP_FAILS = int(os.getenv("LOGIN_MAX_IP_FAILS", "30"))   # per IP, high so one shared address cannot lock everyone
_FAILS: dict[str, deque] = defaultdict(deque)


def _locked(key: str, limit: int = MAX_FAILS) -> bool:
    q, now = _FAILS[key], time.time()
    while q and now - q[0] > FAIL_WINDOW:
        q.popleft()
    return len(q) >= limit


def _fail(ip: str, uname: str, detail: str = ""):
    now = time.time()
    _FAILS[f"ip:{ip}"].append(now)
    _FAILS[f"user:{uname}"].append(now)
    audit(None, None, ip, "login_failed", f"user={uname[:40]} {detail}".strip())
    raise HTTPException(401, "Invalid credentials")


@router.post("/login")
def login(req: LoginRequest, response: Response, request: Request):
    ip = request.client.host if request.client else "unknown"
    uname = req.username.strip().lower()
    if _locked(f"ip:{ip}", MAX_IP_FAILS) or _locked(f"user:{uname}"):
        raise HTTPException(429, "Too many failed logins. Try again in a few minutes.")

    res = supabase.table("users").select("*").eq("user_id", uname).execute()
    user = res.data[0] if res.data else None
    if not user:
        pwd_context.dummy_verify()                 # same timing whether or not the account exists
        _fail(ip, uname)

    stored = user.get("password_hash") or ""
    if stored.startswith("sha256-"):
        valid = secrets.compare_digest(hashlib.sha256(req.password.encode()).hexdigest(), stored[7:])
    else:
        try:
            valid = pwd_context.verify(req.password, stored)
        except Exception:
            valid = False
    # One generic error for a wrong password and for a disabled account
    if not valid or user["status"] != "active":
        _fail(ip, uname)

    if stored.startswith("sha256-") or pwd_context.needs_update(stored):
        supabase.table("users").update({"password_hash": pwd_context.hash(req.password)}).eq("user_id", uname).execute()

    _FAILS.pop(f"user:{uname}", None)
    supabase.table("sessions").delete().lt("expires_at", time.time()).execute()   # purge expired sessions

    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires = time.time() + 86400 * 7
    supabase.table("sessions").insert({"token_hash": token_hash, "user_id": user["user_id"], "expires_at": expires}).execute()
    supabase.table("users").update({"last_login": time.time()}).eq("user_id", user["user_id"]).execute()
    audit(None, None, user["user_id"], "login_success")

    response.set_cookie(key="triage_session", value=token, httponly=True, max_age=86400 * 7,
                        samesite="lax", secure=os.getenv("COOKIE_SECURE", "0") == "1")
    return {"user_id": user["user_id"], "name": user["name"], "role": user["role"]}
@router.post("/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get("triage_session")
    forget_session(token)
    if token:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        supabase.table("sessions").delete().eq("token_hash", token_hash).execute()
    response.delete_cookie("triage_session")
    return {"ok": True}

@router.get("/me")
def me(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401)
    return {"user_id": user, "name": request.state.user_name, "role": request.state.role}