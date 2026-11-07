"""Authentication + role enforcement using Supabase sessions."""
import hashlib
from app import config
import logging
import time
from fastapi import Request
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from app import ad_roles, bootstrap, rbac, store

log = logging.getLogger("uvicorn.error")


_CACHE: dict[str, tuple[float, float, dict]] = {}   # token_hash -> (cached_at, session_expires, user_info)


def _lookup_session(token: str | None) -> dict | None:
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now = time.time()
    hit = _CACHE.get(token_hash)
    if hit and now - hit[0] < config.get("security.session_cache_seconds") and now < hit[1]:
        return hit[2]
    res = store.supabase.table("sessions").select("user_id, expires_at").eq("token_hash", token_hash).execute()
    if not res.data or res.data[0]["expires_at"] <= now:
        _CACHE.pop(token_hash, None)
        return None
    ures = store.supabase.table("users").select(
        "user_id, name, role, status, must_change_password, roles_checked_at"
    ).eq("user_id", res.data[0]["user_id"]).execute()
    if not ures.data or ures.data[0].get("status") != "active":
        _CACHE.pop(token_hash, None)
        return None
    user = ures.data[0]

    # AD users: re-check group membership once their last check is old enough
    if ad_roles.is_stale(user.get("roles_checked_at")):
        status, user = ad_roles.refresh_user(user)
        if status == "revoked":
            forget_user(user["user_id"])          # drop cached sessions on every device
            _CACHE.pop(token_hash, None)
            return None
        if status == "changed":
            forget_user(user["user_id"])

    _CACHE[token_hash] = (now, res.data[0]["expires_at"], user)
    return user


def forget_session(token: str | None):
    if token:
        _CACHE.pop(hashlib.sha256(token.encode()).hexdigest(), None)


def forget_user(user_id: str):
    """Call after a role change, disable or revoke so it applies immediately."""
    for k in [k for k, v in _CACHE.items() if v[2]["user_id"] == user_id]:
        _CACHE.pop(k, None)

_PW_CHANGE_OK = ("/api/auth/change-password", "/api/auth/me", "/api/auth/logout")


def allowed_while_password_change_pending(path: str) -> bool:
    return path in _PW_CHANGE_OK or path.startswith("/static")


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in ("/api/health", "/api/auth/login", "/api/auth/banner") or request.method == "OPTIONS" or path.startswith("/static"):
            return await call_next(request)

        is_setup_path = path == "/setup" or path.startswith("/api/setup")
        if not bootstrap.is_installed():
            if is_setup_path:
                return await call_next(request)
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Setup is not complete.", "code": "setup_required"}, status_code=503)
            return Response(status_code=302, headers={"Location": "/setup"})
        if is_setup_path:                       # once installed, setup is closed to everyone
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Setup is already complete."}, status_code=404)
            return Response(status_code=302, headers={"Location": "/static/login.html"})

        user_info = await run_in_threadpool(_lookup_session, request.cookies.get("triage_session"))

        if not user_info:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
            return Response(status_code=302, headers={"Location": "/static/login.html"})

        if user_info.get("must_change_password") and not allowed_while_password_change_pending(path):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "You must change your password before continuing.",
                                     "code": "password_change_required"}, status_code=403)
            return Response(status_code=302, headers={"Location": "/static/change-password.html"})

        role = user_info["role"]
        need = rbac.rule_for(request.method, path)
        if need is None or not rbac.allows(role, need):
            why = "no rule for this route" if need is None else "needs " + " or ".join(need)
            store.audit(None, None, user_info["user_id"], "forbidden", f"{request.method} {path}: {why}")
            if not path.startswith("/api/") and rbac.is_page(path):
                home = rbac.home_for(role)
                if home != path:
                    return Response(status_code=302, headers={"Location": home})
            return JSONResponse({"detail": "You do not have permission to do this."}, status_code=403)

        request.state.user = user_info["user_id"]
        request.state.role = role
        request.state.caps = sorted(rbac.caps_for(role))
        request.state.user_name = user_info["name"]

        return await call_next(request)