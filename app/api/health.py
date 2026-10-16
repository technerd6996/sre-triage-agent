"""Readiness check: tests each dependency with a short timeout.
/api/health (the Colab ping the UI uses) is unchanged."""
import os
import time

import requests
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app import store
from app.tools import aws
from app.tools.snow import _request

router = APIRouter(prefix="/api")


def _timed(fn) -> dict:
    t = time.time()
    try:
        detail = fn()
        return {"ok": True, "ms": int((time.time() - t) * 1000), "detail": detail}
    except Exception as e:
        return {"ok": False, "ms": int((time.time() - t) * 1000), "detail": str(e)[:200]}


def _llm():
    base = os.getenv("COLAB_API_URL", "").rsplit("/", 1)[0]
    r = requests.get(f"{base}/health", timeout=3)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    return "online"


def _snow():
    _request("GET", "incident", params={"sysparm_limit": 1, "sysparm_fields": "number"})
    return "reachable"


def _aws():
    return f"{len(aws.list_instances(force=True))} instances visible"


def _db():
    store.recent_audit(1)
    return "ok"


@router.get("/ready")
def ready():
    checks = {"llm": _timed(_llm), "servicenow": _timed(_snow), "aws": _timed(_aws), "database": _timed(_db)}
    ok = all(c["ok"] for c in checks.values())
    return JSONResponse({"status": "ready" if ok else "degraded", "checks": checks},
                        status_code=200 if ok else 503)