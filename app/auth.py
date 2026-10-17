"""Authentication + role enforcement using Supabase sessions."""
import hashlib
import logging
import time
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from app import store

log = logging.getLogger("uvicorn.error")
LEVELS = {"viewer": 1, "approver": 2, "admin": 3}

import re

_RUN_ONE = re.compile(r"^/api/agent/runs/[^/]+$")

def required_role(method: str, path: str) -> str:
    # Admin only
    if (path.startswith(("/api/admin", "/admin", "/api/users", "/api/agent/audit"))
            or path == "/api/agent/auto"
            or (path.startswith("/api/rag") and method not in ("GET", "HEAD", "OPTIONS"))):
        return "admin"
    # Approver and above: Agent Console, KB pages, run lists, queue, decisions
    if path in ("/agent", "/knowledge") or path.startswith("/api/rag"):
        return "approver"
    if path.startswith("/api/agent/"):
        # Chat users still poll their own run and the live banner
        if method == "GET" and (_RUN_ONE.match(path) or path == "/api/agent/live"):
            return "viewer"
        return "approver"
    return "viewer"

CACHE_TTL = 45   # seconds a session lookup is reused; also the longest a disabled user keeps access
_CACHE: dict[str, tuple[float, float, dict]] = {}   # token_hash -> (cached_at, session_expires, user_info)


def _lookup_session(token: str | None) -> dict | None:
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now = time.time()
    hit = _CACHE.get(token_hash)
    if hit and now - hit[0] < CACHE_TTL and now < hit[1]:
        return hit[2]
    res = store.supabase.table("sessions").select("user_id, expires_at").eq("token_hash", token_hash).execute()
    if not res.data or res.data[0]["expires_at"] <= now:
        _CACHE.pop(token_hash, None)
        return None
    ures = store.supabase.table("users").select("user_id, name, role, status").eq("user_id", res.data[0]["user_id"]).execute()
    if not ures.data or ures.data[0].get("status") != "active":
        _CACHE.pop(token_hash, None)
        return None
    _CACHE[token_hash] = (now, res.data[0]["expires_at"], ures.data[0])
    return ures.data[0]


def forget_session(token: str | None):
    if token:
        _CACHE.pop(hashlib.sha256(token.encode()).hexdigest(), None)


def forget_user(user_id: str):
    """Call after a role change or disable so it applies immediately (used by the admin console later)."""
    for k in [k for k, v in _CACHE.items() if v[2]["user_id"] == user_id]:
        _CACHE.pop(k, None)

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in ("/api/health", "/api/auth/login") or request.method == "OPTIONS" or path.startswith("/static"):
            return await call_next(request)

        user_info = _lookup_session(request.cookies.get("triage_session"))

        if not user_info:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
            return Response(status_code=302, headers={"Location": "/static/login.html"})

        role = user_info["role"]
        need = required_role(request.method, path)
        
        if LEVELS.get(role, 0) < LEVELS.get(need, 1):
            store.audit(None, None, user_info["user_id"], "forbidden", f"{request.method} {path} needs {need}")
            return JSONResponse({"detail": f"This action requires the {need} role."}, status_code=403)

        # Redirect Admin away from the chat interface (Phase 1 rule)
        if path == "/" and role == "admin":
            return Response(status_code=302, headers={"Location": "/admin"})

        request.state.user = user_info["user_id"]
        request.state.role = role
        request.state.user_name = user_info["name"]

        return await call_next(request)