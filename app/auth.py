"""Authentication + role enforcement (HTTP Basic, roles from env).
Auth is OFF when TRIAGE_USERS is unset, so local dev keeps working.
Run behind HTTPS in production: Basic auth is not encrypted on plain HTTP."""
import base64
import hashlib
import logging
import os
import re
import secrets
import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from app import store

log = logging.getLogger("uvicorn.error")
LEVELS = {"viewer": 1, "approver": 2, "admin": 3}
MAX_FAILS, FAIL_WINDOW = 5, 300  # 5 bad logins per IP per 5 minutes -> temporary block
_FAILS: dict[str, deque] = defaultdict(deque)


def _load_users() -> dict:
    """TRIAGE_USERS="veda:pass1:admin,ravi:sha256-<hex>:approver,nive:pass3:viewer"
    No ':' or ',' inside passwords. Use sha256-<hex> to avoid plain text in .env."""
    users = {}
    for item in os.getenv("TRIAGE_USERS", "").split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":", 2)
        if len(parts) != 3 or parts[2] not in LEVELS or not parts[0] or not parts[1]:
            log.warning("TRIAGE_USERS: skipped invalid entry (format name:password:role)")
            continue
        users[parts[0]] = (parts[1], parts[2])
    return users


USERS = _load_users()
if USERS:
    log.info("Auth enabled for %d user(s).", len(USERS))
else:
    log.warning("TRIAGE_USERS is not set: authentication is DISABLED.")


def _verify(given: str, stored: str) -> bool:
    if stored.startswith("sha256-"):
        return secrets.compare_digest(hashlib.sha256(given.encode()).hexdigest(), stored[7:])
    return secrets.compare_digest(given.encode(), stored.encode())


def _parse_basic(header: str):
    if not header.lower().startswith("basic "):
        return None
    try:
        user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        return user, pw
    except Exception:
        return None


def required_role(method: str, path: str) -> str:
    if path.startswith("/api/agent/audit"):
        return "admin"
    if method == "POST" and re.match(r"^/api/agent/(run|auto)$", path):
        return "approver"
    if method == "POST" and re.match(r"^/api/agent/runs/[^/]+/decision$", path):
        return "approver"
    if path.startswith("/api/rag") and method not in ("GET", "HEAD", "OPTIONS"):
        return "admin"
    return "viewer"


def _locked(ip: str) -> bool:
    q, now = _FAILS[ip], time.time()
    while q and now - q[0] > FAIL_WINDOW:
        q.popleft()
    return len(q) >= MAX_FAILS


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        if not USERS or path == "/api/health" or request.method == "OPTIONS":
            return await call_next(request)

        ip = request.client.host if request.client else "unknown"
        if _locked(ip):
            return JSONResponse({"detail": "Too many failed logins. Try again in a few minutes."}, status_code=429)

        creds = _parse_basic(request.headers.get("authorization", ""))
        entry = USERS.get(creds[0]) if creds else None
        if not creds or not entry or not _verify(creds[1], entry[0]):
            if creds:
                _FAILS[ip].append(time.time())
                store.audit(None, None, ip, "login_failed", f"user={creds[0][:40]}")
            return Response(status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="TRIAGE-1", charset="UTF-8"'})

        user, role = creds[0], entry[1]
        need = required_role(request.method, path)
        if LEVELS[role] < LEVELS[need]:
            store.audit(None, None, user, "forbidden", f"{request.method} {path} needs {need}")
            return JSONResponse({"detail": f"This action requires the {need} role."}, status_code=403)

        request.state.user = f"{user} ({role})"
        return await call_next(request)