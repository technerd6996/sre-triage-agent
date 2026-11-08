import hashlib
from app import config
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from fastapi import APIRouter, Response, Request, HTTPException
from pydantic import BaseModel

log = logging.getLogger("uvicorn.error")
from app import bootstrap, rbac, ad_roles
from app.auth import forget_session, forget_user
from app.ports.errors import Unavailable
from app.store import supabase, audit
from passlib.context import CryptContext

router = APIRouter(prefix="/api/auth")
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

class LoginRequest(BaseModel):
    username: str
    password: str

MAX_FAILS, FAIL_WINDOW = 5, 300          # 5 bad logins per ACCOUNT in 5 minutes
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
    if _locked(f"ip:{ip}", config.get("security.login_max_ip_fails")) or _locked(f"user:{uname}"):
        raise HTTPException(429, "Too many failed logins. Try again in a few minutes.")

    res = supabase.table("users").select("*").eq("user_id", uname).execute()
    user = res.data[0] if res.data else None
    valid = False

    from app.ports import registry
    try:
        identity = registry.get_identity()
    except Exception:
        identity = None

    is_local_admin = (uname == bootstrap.DEFAULT_ADMIN_ID)

    if is_local_admin or not identity:
        # Local authentication (always used for the Default Super Admin)
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

        if valid and (stored.startswith("sha256-") or pwd_context.needs_update(stored)):
            supabase.table("users").update({"password_hash": pwd_context.hash(req.password)}).eq("user_id", uname).execute()
    else:
        # Active Directory authentication, then roles from mapped groups
        try:
            valid = identity.authenticate(uname, req.password)
        except Exception as e:
            log.warning(f"AD Auth failed for {uname}: {e}")
            valid = False

        if valid:
            if user and user.get("status") != "active":
                _fail(ip, uname)                       # disabled locally: same generic error

            try:
                roles = ad_roles.resolve_roles(identity, uname)
            except Unavailable as e:
                # Fail closed: never guess a role when the directory cannot be read
                log.warning(f"AD group lookup failed for {uname}: {e}")
                audit(None, None, uname, "login_denied_directory_unavailable")
                raise HTTPException(503, "The directory is unavailable. Try again shortly.")

            if not roles:
                audit(None, None, uname, "login_denied_no_group", "no mapped AD group")
                raise HTTPException(403, "Not authorised. Your account is not in any group mapped to a TRIAGE-1 role.")

            role_value = ",".join(roles)
            now = time.time()
            if not user:
                supabase.table("users").insert({
                    "user_id": uname, "name": uname.split("@")[0], "role": role_value,
                    "type": "human", "status": "active", "password_hash": "", "roles_checked_at": now
                }).execute()
                user = supabase.table("users").select("*").eq("user_id", uname).execute().data[0]
                audit(None, None, uname, "ad_user_provisioned", role_value)
            else:
                patch = {"roles_checked_at": now}
                if user.get("role") != role_value:
                    patch["role"] = role_value
                    audit(None, None, uname, "ad_roles_changed", f"{user.get('role')} -> {role_value}")
                    user["role"] = role_value
                    forget_user(uname)                 # drop cached sessions so the new roles apply now
                supabase.table("users").update(patch).eq("user_id", uname).execute()                  # drop cached sessions so the new roles apply now

    # One generic error for a wrong password and for a disabled account
    if not valid or (user and user.get("status") != "active"):
        _fail(ip, uname)

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
    return {"user_id": user["user_id"], "name": user["name"], "role": user["role"],
            "role_label": rbac.label_for(user["role"]), "caps": sorted(rbac.caps_for(user["role"])),
            "home": rbac.home_for(user["role"]),
            "must_change_password": bool(user.get("must_change_password"))}


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
    r = request.state.role
    return {"user_id": user, "name": request.state.user_name, "role": r, "role_label": rbac.label_for(r),
            "caps": sorted(rbac.caps_for(r)), "home": rbac.home_for(r)}

def _verify(password: str, stored: str) -> bool:
    if stored.startswith("sha256-"):
        return secrets.compare_digest(hashlib.sha256(password.encode()).hexdigest(), stored[7:])
    try:
        return pwd_context.verify(password, stored)
    except Exception:
        return False


@router.get("/banner")
def banner():
    """Public: lets the login page warn while the shipped default account still has its default password."""
    on = bootstrap.default_admin_unchanged()
    return {"default_admin": on, "username": bootstrap.DEFAULT_ADMIN_ID if on else None}


class ChangePassword(BaseModel):
    current_password: str
    new_password: str


@router.post("/change-password")
def change_password(req: ChangePassword, request: Request):
    uid = getattr(request.state, "user", None)
    if not uid:
        raise HTTPException(401)
    if _locked(f"user:{uid}"):
        raise HTTPException(429, "Too many failed attempts. Try again in a few minutes.")
    rows = supabase.table("users").select("password_hash").eq("user_id", uid).execute().data
    if not rows or not _verify(req.current_password, rows[0].get("password_hash") or ""):
        _FAILS[f"user:{uid}"].append(time.time())
        audit(None, None, uid, "password_change_failed")
        raise HTTPException(403, "The current password is incorrect")
    problem = bootstrap.check_new_password(req.new_password, uid, req.current_password)
    if problem:
        raise HTTPException(422, problem)
    supabase.table("users").update({"password_hash": pwd_context.hash(req.new_password),
                                    "must_change_password": False}).eq("user_id", uid).execute()
    token = request.cookies.get("triage_session") or ""
    keep = hashlib.sha256(token.encode()).hexdigest()
    supabase.table("sessions").delete().eq("user_id", uid).neq("token_hash", keep).execute()   # other devices sign out
    forget_user(uid)
    _FAILS.pop(f"user:{uid}", None)
    audit(None, None, uid, "password_changed")
    return {"ok": True}