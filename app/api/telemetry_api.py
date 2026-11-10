"""Read-only API behind the admin Performance page: metrics summary, traces, one trace, logs (permission monitor.view)."""
import re
import time

from fastapi import APIRouter, HTTPException, Query

from app import config, store, telemetry

router = APIRouter(prefix="/api/admin/telemetry")

_HEX32 = re.compile(r"[0-9a-f]{32}")
_METRIC_COLS = "ts,name,kind,attrs,n_count,total,vmax,bounds,buckets,value"


def _hours(h: float) -> float:
    return min(max(float(h), 0.25), float(config.get("telemetry.retention_hours")))


@router.get("/status")
def status():
    """Is collection on, and how much is stored."""
    out = {"enabled": bool(config.get("telemetry.enabled")), "running": telemetry._ready,
           "retention_hours": config.get("telemetry.retention_hours"), "stored": {}}
    try:
        from app.db import get_pool
        with get_pool().connection() as c:
            for t in ("otel_spans", "otel_metrics", "otel_logs"):
                out["stored"][t] = c.execute(f"select count(*) as n from {t}").fetchone()["n"]
    except Exception:                                    # noqa: BLE001
        out["stored"] = None
    return out


@router.get("/summary")
def summary(hours: float = Query(24)):
    h = _hours(hours)
    try:
        rows = store.supabase.table("otel_metrics").select(_METRIC_COLS).gte("ts", time.time() - h * 3600) \
            .order("ts", desc=True).limit(200000).execute().data
    except Exception:                                    # noqa: BLE001
        raise HTTPException(503, "Telemetry data is unavailable.")
    return telemetry.summarize(rows, h)


@router.get("/traces")
def traces(hours: float = Query(24), min_ms: float = Query(0), errors_only: bool = Query(False),
           feature: str = Query(""), limit: int = Query(100)):
    h, limit = _hours(hours), min(max(int(limit), 1), 300)
    try:
        q = store.supabase.table("otel_spans").select("trace_id,span_id,name,kind,start_ts,duration_ms,status,error,attrs") \
            .gte("start_ts", time.time() - h * 3600).is_("parent_id", "null")
        if min_ms > 0:
            q = q.gte("duration_ms", float(min_ms))
        if errors_only:
            q = q.eq("status", "error")
        rows = q.order("start_ts", desc=True).limit(limit * 3 if feature else limit).execute().data
    except Exception:                                    # noqa: BLE001
        raise HTTPException(503, "Telemetry data is unavailable.")
    if feature:
        rows = [r for r in rows if (r.get("attrs") or {}).get("app.feature") == feature][:limit]
    return {"traces": rows}


@router.get("/traces/{trace_id}")
def trace(trace_id: str):
    if not _HEX32.fullmatch(trace_id or ""):
        raise HTTPException(400, "Not a trace id.")
    try:
        rows = store.supabase.table("otel_spans").select("*").eq("trace_id", trace_id).order("start_ts").limit(500).execute().data
        logs = store.supabase.table("otel_logs").select("ts,severity,body,attrs").eq("trace_id", trace_id).order("ts").limit(50).execute().data
    except Exception:                                    # noqa: BLE001
        raise HTTPException(503, "Telemetry data is unavailable.")
    if not rows:
        raise HTTPException(404, "That trace is no longer stored.")
    return {"spans": rows, "logs": logs}


@router.get("/logs")
def logs(hours: float = Query(24), level: str = Query(""), limit: int = Query(200)):
    h, limit = _hours(hours), min(max(int(limit), 1), 500)
    try:
        q = store.supabase.table("otel_logs").select("ts,severity,body,trace_id,attrs").gte("ts", time.time() - h * 3600)
        if level.upper() in ("INFO", "WARNING", "ERROR"):
            q = q.eq("severity", level.upper())
        rows = q.order("ts", desc=True).limit(limit).execute().data
    except Exception:                                    # noqa: BLE001
        raise HTTPException(503, "Telemetry data is unavailable.")
    return {"logs": rows}
