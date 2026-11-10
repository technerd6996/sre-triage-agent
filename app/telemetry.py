"""Telemetry for the web app itself, in the OpenTelemetry model: traces, metrics and logs.

In scope: web requests, calls to outside systems (ServiceNow, AD, mail, the model server, the credential store),
chat and approver chat, and app health (database timing and pool, model queue wait, process CPU and memory).
Out of scope on purpose: agent runs and the audit log. Anything that happens inside a run (usage._run_ctx is set)
or in a run-supporting thread (mute()) is dropped, and the run tables are not timed.

Everything is stored in this app's own Postgres (otel_spans, otel_metrics, otel_logs) and shown in the admin panel.
Nothing leaves the machine. Telemetry must never break the app: every hook swallows its own errors.
"""
import atexit
import contextvars
import functools
import logging
import random
import re
import threading
import time
from contextlib import contextmanager

from app import config

log = logging.getLogger("uvicorn.error")

# ---- settings (admin panel > Telemetry) ----
config.register("telemetry.enabled", "Collect app telemetry (traces, metrics, logs)", True, "bool", "telemetry",
                help="Measures the web app: requests, calls to ServiceNow/AD/mail/the model server, chat, database and process health. "
                     "Agent runs and the audit log are never included. Turning it off stops collecting at once.")
config.register("telemetry.retention_hours", "Keep telemetry for (hours)", 72, "int", "telemetry", lo=1, hi=720)
config.register("telemetry.max_rows", "Most traces and log lines kept", 50000, "int", "telemetry", lo=1000, hi=1000000,
                help="A cap on stored spans and on stored log lines. The oldest go first.")
config.register("telemetry.trace_sample_percent", "Share of normal request traces kept (%)", 100, "int", "telemetry", lo=0, hi=100,
                help="Slow and failed requests are always kept. Metrics always count every request, whatever this says.")
config.register("telemetry.slow_ms", "A request or outside call slower than this is slow (ms)", 1000, "int", "telemetry", lo=50, hi=600000)
config.register("telemetry.slow_db_ms", "A database query slower than this gets its own trace entry (ms)", 250, "int", "telemetry", lo=10, hi=60000)
config.register("telemetry.log_level", "Lowest log level kept", "WARNING", "select", "telemetry",
                options=("INFO", "WARNING", "ERROR"),
                help="Logs are the app's own events: failed or slow outside calls and server errors. Not the server log, not runs.")
config.register("telemetry.exclude_paths", "Web paths not measured (comma separated prefixes)",
                "/api/agent,/api/admin/runs,/api/admin/jobs,/api/admin/telemetry,/api/health,/static", "text", "telemetry",
                help="Run screens and their polling are left out so this stays about the app, not the runs.")
config.register("telemetry.exclude_tables", "Database tables not timed (comma separated)",
                "runs,jobs,approvals,audit,llm_calls,events,checkpoints,checkpoint_writes,checkpoint_blobs,checkpoint_migrations",
                "text", "telemetry", help="Run and audit tables. The telemetry tables themselves are never timed.")
config.register("telemetry.metric_interval_seconds", "How often metrics are written (seconds)", 60, "int", "telemetry",
                lo=10, hi=600, live=False)

_DDL = """
create table if not exists public.otel_spans (
  span_id text primary key, trace_id text not null, parent_id text, name text, kind text,
  start_ts double precision not null, duration_ms double precision, status text, error text, attrs jsonb);
create index if not exists idx_otel_spans_trace on public.otel_spans (trace_id);
create index if not exists idx_otel_spans_start on public.otel_spans (start_ts desc);
create table if not exists public.otel_metrics (
  id bigserial primary key, ts double precision not null, name text not null, kind text, attrs jsonb,
  n_count bigint, total double precision, vmin double precision, vmax double precision,
  bounds jsonb, buckets jsonb, value double precision);
create index if not exists idx_otel_metrics_ts on public.otel_metrics (name, ts desc);
create table if not exists public.otel_logs (
  id bigserial primary key, ts double precision not null, severity text, body text,
  trace_id text, span_id text, attrs jsonb);
create index if not exists idx_otel_logs_ts on public.otel_logs (ts desc);
"""

BOUNDS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120]    # seconds

_ready = False
_providers = ()
_lock = threading.Lock()
_MUTED: contextvars.ContextVar = contextvars.ContextVar("telemetry_muted", default=False)
_tracer = None
_otel_logger = None
_h_http = _h_out = _h_db = _h_queue = _h_ttft = None
_warned = set()


def _warn_once(what: str, err) -> None:
    if what not in _warned:
        _warned.add(what)
        log.warning("Telemetry: %s: %s", what, str(err)[:200])


def mute() -> None:
    """Call at the top of a thread that belongs to agent runs (poller, dispatcher, watchdog): nothing it does is recorded."""
    _MUTED.set(True)


def _skip() -> bool:
    if not _ready or _MUTED.get():
        return True
    try:
        if not config.get("telemetry.enabled"):
            return True
        from app.llm import usage
        return usage._run_ctx.get() is not None          # inside an agent run: not the app's own performance
    except Exception:                                    # noqa: BLE001
        return True


def _csv(key: str) -> tuple:
    try:
        return tuple(p.strip() for p in str(config.get(key) or "").split(",") if p.strip())
    except Exception:                                    # noqa: BLE001
        return ()


# ---------------------------------------------------------------- pure helpers (tested without a database)

def quantile(bounds: list, buckets: list, q: float, vmax=None):
    """Estimate a quantile (0..1) from histogram buckets; None when empty. Never above the recorded maximum."""
    total = sum(buckets or [])
    if not total:
        return None
    rank, cum = q * total, 0
    for i, c in enumerate(buckets):
        if c and cum + c >= rank:
            lo = bounds[i - 1] if i > 0 else 0.0
            hi = bounds[i] if i < len(bounds) else (vmax if vmax is not None else lo)
            hi = max(hi, lo)
            val = lo + (hi - lo) * ((rank - cum) / c)
            return min(val, vmax) if vmax is not None else val
        cum += c
    return vmax


_FEATURES = (
    (re.compile(r"/api/approver-chat/(inquire|cmdb)"), "approver chat: checks"),
    (re.compile(r"/api/approver-chat(/.*)?"), "approver chat: actions"),
    (re.compile(r"/api/(chat|title|servers)(/.*)?"), "chat"),
    (re.compile(r"/api/issues(/.*)?"), "tickets from chat"),
    (re.compile(r"/api/auth(/.*)?"), "sign-in"),
    (re.compile(r"/api/(admin|users)(/.*)?"), "admin"),
    (re.compile(r"/(admin|agent)?"), "pages"),
)


def feature_for(route: str) -> str:
    for pat, name in _FEATURES:
        if pat.fullmatch(route or ""):
            return name
    return "other"


_ID = re.compile(r"/(INC\d+|[0-9a-fA-F]{8,}(-[0-9a-fA-F]{4,})*|\d+)(?=/|$)")


def normalise(path: str) -> str:
    """A route label with low variety when the framework did not give the route template."""
    return _ID.sub("/{id}", path or "/")[:120]


class _H:
    """Merged histogram rows."""

    def __init__(self):
        self.n, self.total, self.vmax, self.bounds, self.buckets = 0, 0.0, None, None, None

    def add(self, r: dict) -> None:
        self.n += r.get("n_count") or 0
        self.total += r.get("total") or 0.0
        if r.get("vmax") is not None:
            self.vmax = r["vmax"] if self.vmax is None else max(self.vmax, r["vmax"])
        b, k = r.get("bounds"), r.get("buckets")
        if b and k:
            if self.buckets is None:
                self.bounds, self.buckets = list(b), [0] * len(k)
            if len(k) == len(self.buckets):
                self.buckets = [x + y for x, y in zip(self.buckets, k)]

    def ms(self, q):
        v = quantile(self.bounds or [], self.buckets or [], q, self.vmax)
        return None if v is None else round(v * 1000, 1)

    def mean_ms(self):
        return round(self.total / self.n * 1000, 1) if self.n else None

    def max_ms(self):
        return None if self.vmax is None else round(self.vmax * 1000, 1)


def summarize(rows: list, hours: float, now: float = None) -> dict:
    """Metric rows (as stored in otel_metrics) -> the numbers the admin panel shows. Pure, so it is unit tested."""
    now = now or time.time()
    total, by_feature, by_route = _H(), {}, {}
    errs, client_errs = 0, 0
    feat_err, route_err, out_sys, out_op, out_err, out_ops_err = {}, {}, {}, {}, {}, {}
    db, db_n = _H(), 0
    queue, ttft = {}, {}
    gauges: dict = {}
    step = 300 if hours <= 6 else (900 if hours <= 24 else 3600)
    series: dict = {}
    for r in rows:
        name, a, ts = r.get("name"), r.get("attrs") or {}, r.get("ts") or 0
        if name == "http.server.request.duration":
            n = r.get("n_count") or 0
            code = int(a.get("http.response.status_code") or 0)
            feat = a.get("app.feature") or "other"
            route = f'{a.get("http.request.method", "?")} {a.get("http.route", "?")}'
            total.add(r)
            by_feature.setdefault(feat, _H()).add(r)
            by_route.setdefault(route, _H()).add(r)
            bad = code >= 500 or code == 0
            if bad:
                errs += n
                feat_err[feat] = feat_err.get(feat, 0) + n
                route_err[route] = route_err.get(route, 0) + n
            elif code >= 400:
                client_errs += n
            b = series.setdefault(int(ts // step * step), [0, 0])
            b[0] += n
            b[1] += n if bad else 0
        elif name == "triage.outbound.duration":
            system, op = a.get("system", "?"), f'{a.get("system", "?")}: {a.get("operation", "?")}'
            out_sys.setdefault(system, _H()).add(r)
            out_op.setdefault(op, _H()).add(r)
            if a.get("outcome") == "error":
                out_err[system] = out_err.get(system, 0) + (r.get("n_count") or 0)
                out_ops_err[op] = out_ops_err.get(op, 0) + (r.get("n_count") or 0)
        elif name == "db.client.operation.duration":
            db.add(r)
        elif name == "triage.model.queue.wait":
            queue.setdefault(a.get("kind", "?"), _H()).add(r)
        elif name == "triage.model.ttft":
            ttft.setdefault(a.get("kind", "?"), _H()).add(r)
        elif r.get("kind") == "gauge":
            g = gauges.setdefault(name, {"sum": 0.0, "n": 0, "max": 0.0, "last": 0.0, "last_ts": 0})
            v = r.get("value") or 0.0
            g["sum"] += v
            g["n"] += 1
            g["max"] = max(g["max"], v)
            if ts >= g["last_ts"]:
                g["last"], g["last_ts"] = v, ts

    def row(label, h, errors=0, key="name"):
        return {key: label, "requests": h.n, "errors": errors, "p50_ms": h.ms(0.5), "p95_ms": h.ms(0.95), "max_ms": h.max_ms()}

    def gv(name, scale=1.0, nd=1):
        g = gauges.get(name)
        if not g:
            return None
        return {"avg": round(g["sum"] / g["n"] * scale, nd), "max": round(g["max"] * scale, nd), "last": round(g["last"] * scale, nd)}

    routes = [row(k, h, route_err.get(k, 0), "route") for k, h in by_route.items()]
    routes.sort(key=lambda x: -(x["p95_ms"] or 0))
    outside = [dict(row(k, h, out_err.get(k, 0), "system"), calls=h.n) for k, h in out_sys.items()]
    for o in outside:
        o.pop("requests", None)
    ops = [dict(row(k, h, out_ops_err.get(k, 0), "operation"), calls=h.n) for k, h in out_op.items()]
    for o in ops:
        o.pop("requests", None)
    ops.sort(key=lambda x: -(x["p95_ms"] or 0))
    return {
        "hours": hours, "generated": now,
        "requests": {"total": total.n, "errors": errs, "client_errors": client_errs,
                     "error_rate": round(errs / total.n * 100, 2) if total.n else 0.0,
                     "p50_ms": total.ms(0.5), "p95_ms": total.ms(0.95), "p99_ms": total.ms(0.99), "max_ms": total.max_ms()},
        "features": sorted([row(k, h, feat_err.get(k, 0), "feature") for k, h in by_feature.items()], key=lambda x: -x["requests"]),
        "routes": routes[:15],
        "outbound": sorted(outside, key=lambda x: -x["calls"]),
        "operations": ops[:15],
        "database": {"queries": db.n, "p50_ms": db.ms(0.5), "p95_ms": db.ms(0.95), "max_ms": db.max_ms(),
                     "pool_in_use": gv("db.pool.in_use", 1, 1), "pool_waiting": gv("db.pool.waiting", 1, 1)},
        "model": {"queue_wait": [{"kind": k, "calls": h.n, "p50_ms": h.ms(0.5), "p95_ms": h.ms(0.95), "max_ms": h.max_ms()}
                                 for k, h in sorted(queue.items())],
                  "ttft": [{"kind": k, "calls": h.n, "avg_ms": h.mean_ms(), "p95_ms": h.ms(0.95)} for k, h in sorted(ttft.items())]},
        "process": {"cpu_percent": gv("process.cpu.utilization", 100.0), "memory_mb": gv("process.memory.usage", 1 / 1048576.0),
                    "threads": gv("process.thread.count", 1, 0)},
        "series": [{"t": t, "requests": v[0], "errors": v[1]} for t, v in sorted(series.items())], "step": step,
    }


# ---------------------------------------------------------------- storage

def _table(name: str):
    from app import store
    return store.supabase.table(name)


def ensure_tables() -> None:
    from app.db import get_pool
    with get_pool().connection() as c:
        c.execute(_DDL)


def _span_row(s) -> dict:
    ctx, par = s.get_span_context(), s.parent
    attrs = {k: (list(v) if isinstance(v, tuple) else v) for k, v in (s.attributes or {}).items()}
    st = s.status
    code = getattr(getattr(st, "status_code", None), "name", "UNSET")
    return {
        "span_id": f"{ctx.span_id:016x}", "trace_id": f"{ctx.trace_id:032x}",
        "parent_id": f"{par.span_id:016x}" if par is not None else None,
        "name": s.name[:160], "kind": getattr(s.kind, "name", "INTERNAL"),
        "start_ts": s.start_time / 1e9, "duration_ms": round((s.end_time - s.start_time) / 1e6, 3),
        "status": "error" if code == "ERROR" else "ok", "error": (getattr(st, "description", None) or "")[:300] or None,
        "attrs": attrs,
    }


def _build_providers():
    from opentelemetry import metrics as _m  # noqa: F401  (import check)
    from opentelemetry.sdk._logs import LoggerProvider
    try:                                                 # current names; the older ones are deprecated in the SDK
        from opentelemetry.sdk._logs.export import LogRecordExporter as LogExporter, LogRecordExportResult as LogExportResult
    except ImportError:                                  # an older SDK
        from opentelemetry.sdk._logs.export import LogExporter, LogExportResult
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import (Counter, Histogram, MeterProvider, ObservableCounter, ObservableGauge,
                                           ObservableUpDownCounter, UpDownCounter)
    from opentelemetry.sdk.metrics.export import (AggregationTemporality, MetricExporter, MetricExportResult,
                                                  PeriodicExportingMetricReader)
    from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
    from opentelemetry.trace import StatusCode

    resource = Resource.create({"service.name": "triage-1", "service.namespace": "sre"})

    class PgSpanExporter(SpanExporter):
        def export(self, spans):
            try:
                rows = [_span_row(s) for s in spans]
                if rows:
                    _table("otel_spans").insert(rows).execute()
                return SpanExportResult.SUCCESS
            except Exception as e:                       # noqa: BLE001
                _warn_once("writing spans", e)
                return SpanExportResult.FAILURE

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=30000):
            return True

    class TailSampler(SpanProcessor):
        """Holds a request's child spans until its root span ends, then keeps the whole trace if it was slow or failed,
        or by chance (telemetry.trace_sample_percent). Errors and slow requests are therefore never sampled away."""

        def __init__(self, delegate):
            self._d, self._pending, self._lk = delegate, {}, threading.Lock()

        def on_start(self, span, parent_context=None):
            pass

        def _keep(self, root, kids) -> bool:
            try:
                if any(s.status.status_code == StatusCode.ERROR for s in [root] + kids):
                    return True
                if (root.end_time - root.start_time) / 1e6 > config.get("telemetry.slow_ms"):
                    return True
                return random.random() * 100 < config.get("telemetry.trace_sample_percent")
            except Exception:                            # noqa: BLE001
                return True

        def on_end(self, span):
            tid = span.get_span_context().trace_id
            with self._lk:
                if span.parent is not None:
                    ent = self._pending.setdefault(tid, [time.time(), []])
                    if len(ent[1]) < 300:
                        ent[1].append(span)
                    if len(self._pending) > 500:         # roots that never ended: forget the oldest
                        cut = time.time() - 900
                        for k in [k for k, v in self._pending.items() if v[0] < cut]:
                            self._pending.pop(k, None)
                    return
                kids = self._pending.pop(tid, [0, []])[1]
            if self._keep(span, kids):
                for s in kids + [span]:
                    self._d.on_end(s)

        def shutdown(self):
            self._d.shutdown()

        def force_flush(self, timeout_millis=30000):
            return self._d.force_flush(timeout_millis)

    class PgMetricExporter(MetricExporter):
        def __init__(self):
            D, C = AggregationTemporality.DELTA, AggregationTemporality.CUMULATIVE
            super().__init__(preferred_temporality={Counter: D, UpDownCounter: C, Histogram: D, ObservableCounter: D,
                                                    ObservableUpDownCounter: C, ObservableGauge: C})

        def export(self, metrics_data, timeout_millis=10000, **kw):
            try:
                rows = []
                for rm in metrics_data.resource_metrics:
                    for sm in rm.scope_metrics:
                        for m in sm.metrics:
                            for dp in m.data.data_points:
                                attrs = dict(dp.attributes or {})
                                ts = dp.time_unix_nano / 1e9
                                if hasattr(dp, "bucket_counts"):
                                    if not dp.count:
                                        continue
                                    rows.append({"ts": ts, "name": m.name, "kind": "histogram", "attrs": attrs,
                                                 "n_count": int(dp.count), "total": float(dp.sum), "vmin": dp.min, "vmax": dp.max,
                                                 "bounds": [float(b) for b in dp.explicit_bounds],
                                                 "buckets": [int(c) for c in dp.bucket_counts], "value": None})
                                else:
                                    rows.append({"ts": ts, "name": m.name, "kind": "gauge", "attrs": attrs, "n_count": None,
                                                 "total": None, "vmin": None, "vmax": None, "bounds": None, "buckets": None,
                                                 "value": float(dp.value)})
                if rows:
                    _table("otel_metrics").insert(rows).execute()
                return MetricExportResult.SUCCESS
            except Exception as e:                       # noqa: BLE001
                _warn_once("writing metrics", e)
                return MetricExportResult.FAILURE

        def force_flush(self, timeout_millis=10000):
            return True

        def shutdown(self, timeout_millis=30000, **kw):
            pass

    class PgLogExporter(LogExporter):
        def export(self, batch):
            try:
                rows = []
                for item in batch:
                    r = getattr(item, "log_record", item)
                    ctx = getattr(r, "trace_id", 0) or 0
                    sp = getattr(r, "span_id", 0) or 0
                    attrs = {k: v for k, v in (r.attributes or {}).items()
                             if not str(k).startswith(("code.", "thread.", "log.", "exception."))}
                    sev = {"WARN": "WARNING", "FATAL": "CRITICAL"}.get(r.severity_text or "INFO", r.severity_text or "INFO")
                    rows.append({"ts": (r.timestamp or time.time_ns()) / 1e9, "severity": sev[:10],
                                 "body": str(r.body)[:600], "trace_id": f"{ctx:032x}" if ctx else None,
                                 "span_id": f"{sp:016x}" if sp else None, "attrs": attrs})
                if rows:
                    _table("otel_logs").insert(rows).execute()
                return LogExportResult.SUCCESS
            except Exception as e:                       # noqa: BLE001
                _warn_once("writing logs", e)
                return LogExportResult.FAILURE

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=10000):
            return True

    tp = TracerProvider(resource=resource)
    tp.add_span_processor(TailSampler(BatchSpanProcessor(PgSpanExporter(), schedule_delay_millis=5000, max_export_batch_size=256)))

    reader = PeriodicExportingMetricReader(PgMetricExporter(),
                                           export_interval_millis=int(config.get("telemetry.metric_interval_seconds")) * 1000)
    views = [View(instrument_name=n, aggregation=ExplicitBucketHistogramAggregation(BOUNDS))
             for n in ("http.server.request.duration", "triage.outbound.duration", "db.client.operation.duration",
                       "triage.model.queue.wait", "triage.model.ttft")]
    mp = MeterProvider(resource=resource, metric_readers=[reader], views=views)

    lp = LoggerProvider(resource=resource)
    lp.add_log_record_processor(BatchLogRecordProcessor(PgLogExporter(), schedule_delay_millis=5000))
    return tp, mp, lp


_proc = None


def _process():
    """One long-lived Process object: CPU percent is measured between calls, so a new object every time would always read 0."""
    global _proc
    if _proc is None:
        import psutil
        _proc = psutil.Process()
        _proc.cpu_percent(None)
    return _proc


def _observe_process(options):
    from opentelemetry.metrics import Observation
    import psutil
    yield Observation(_process().cpu_percent(None) / 100.0 / (psutil.cpu_count() or 1))


def _observe_memory(options):
    from opentelemetry.metrics import Observation
    yield Observation(_process().memory_info().rss)


def _observe_threads(options):
    from opentelemetry.metrics import Observation
    yield Observation(threading.active_count())


def _pool_stats() -> dict:
    from app.db import get_pool
    return get_pool().get_stats()


def _observe_pool_in_use(options):
    from opentelemetry.metrics import Observation
    s = _pool_stats()
    yield Observation(max(0, s.get("pool_size", 0) - s.get("pool_available", 0)))


def _observe_pool_waiting(options):
    from opentelemetry.metrics import Observation
    yield Observation(_pool_stats().get("requests_waiting", 0))


def setup() -> bool:
    """Start the providers once. Safe to call again. Returns whether telemetry is running."""
    global _ready, _tracer, _otel_logger, _h_http, _h_out, _h_db, _h_queue, _h_ttft, _providers
    with _lock:
        if _ready:
            return True
        try:
            ensure_tables()
        except Exception as e:                           # noqa: BLE001  start script also applies db/schema.sql
            log.warning("Telemetry tables could not be created here: %s", str(e)[:150])
        try:
            tp, mp, lp = _build_providers()
            _tracer = tp.get_tracer("triage.app")
            _otel_logger = lp.get_logger("triage.app")
            meter = mp.get_meter("triage.app")
            _h_http = meter.create_histogram("http.server.request.duration", unit="s", description="Web request duration")
            _h_out = meter.create_histogram("triage.outbound.duration", unit="s", description="Calls to outside systems")
            _h_db = meter.create_histogram("db.client.operation.duration", unit="s", description="Database query duration")
            _h_queue = meter.create_histogram("triage.model.queue.wait", unit="s", description="Wait for a model slot")
            _h_ttft = meter.create_histogram("triage.model.ttft", unit="s", description="Time to first token")
            meter.create_observable_gauge("process.cpu.utilization", callbacks=[_observe_process], unit="1")
            meter.create_observable_gauge("process.memory.usage", callbacks=[_observe_memory], unit="By")
            meter.create_observable_gauge("process.thread.count", callbacks=[_observe_threads])
            meter.create_observable_gauge("db.pool.in_use", callbacks=[_observe_pool_in_use])
            meter.create_observable_gauge("db.pool.waiting", callbacks=[_observe_pool_waiting])
            _providers = (tp, mp, lp)
            _ready = True
            threading.Thread(target=_retention_loop, daemon=True, name="telemetry-retention").start()
            return True
        except Exception as e:                           # noqa: BLE001
            log.warning("Telemetry could not start: %s", str(e)[:200])
            return False


def shutdown() -> None:
    """Flush and stop the providers once (also runs when the process exits)."""
    global _providers
    ps, _providers = _providers, ()
    for p in ps:
        try:
            p.shutdown()
        except Exception:                                # noqa: BLE001
            pass


atexit.register(shutdown)


def flush() -> None:
    """Write everything buffered right now (used by tests and by the status page)."""
    for p in _providers:
        try:
            p.force_flush()
        except Exception as e:                           # noqa: BLE001
            _warn_once("flush", e)


def purge(now: float = None) -> dict:
    """Delete telemetry past its retention and keep the stored spans and logs under the cap."""
    from app.db import get_pool
    now = now or time.time()
    cut = now - config.get("telemetry.retention_hours") * 3600
    cap = int(config.get("telemetry.max_rows"))
    out = {}
    with get_pool().connection() as c:
        out["spans"] = c.execute("delete from otel_spans where start_ts < %s", (cut,)).rowcount
        out["metrics"] = c.execute("delete from otel_metrics where ts < %s", (cut,)).rowcount
        out["logs"] = c.execute("delete from otel_logs where ts < %s", (cut,)).rowcount
        out["spans"] += c.execute("delete from otel_spans where span_id in "
                                  "(select span_id from otel_spans order by start_ts desc offset %s)", (cap,)).rowcount
        out["logs"] += c.execute("delete from otel_logs where id in "
                                 "(select id from otel_logs order by ts desc offset %s)", (cap,)).rowcount
    return out


def _retention_loop():
    mute()
    time.sleep(30)
    while True:
        try:
            purge()
        except Exception as e:                           # noqa: BLE001
            _warn_once("clean-up", e)
        time.sleep(600)


# ---------------------------------------------------------------- hooks used by the app

def log_event(level: int, message: str, **attrs) -> None:
    """One app event (a failed outside call, a server error). Dropped inside runs and below telemetry.log_level."""
    if _skip():
        return
    try:
        floor = getattr(logging, str(config.get("telemetry.log_level")).upper(), logging.WARNING)
        if level < floor or _otel_logger is None:
            return
        from opentelemetry._logs import SeverityNumber
        sn, name = ((SeverityNumber.ERROR, "ERROR") if level >= logging.ERROR
                    else (SeverityNumber.WARN, "WARNING") if level >= logging.WARNING else (SeverityNumber.INFO, "INFO"))
        _otel_logger.emit(severity_number=sn, severity_text=name, body=message[:400],
                          attributes={k: (v if isinstance(v, (str, int, float, bool)) else str(v)) for k, v in attrs.items() if v is not None})
    except Exception:                                    # noqa: BLE001
        pass


def annotate(**attrs) -> None:
    """Add facts to the current request's span, for example outcome='failed'. Never message text."""
    if _skip():
        return
    try:
        from opentelemetry import trace
        span = trace.get_current_span()
        for k, v in attrs.items():
            span.set_attribute(f"app.{k}", v)
    except Exception:                                    # noqa: BLE001
        pass


class _Box:
    """Handed to the body of outbound(): set .error to count the call as failed although nothing was raised."""
    __slots__ = ("span", "error")

    def __init__(self, span):
        self.span, self.error = span, None


@contextmanager
def outbound(system: str, operation: str, **attrs):
    """with outbound('servicenow', 'GET incident') as call: ...   Times a call to an outside system as a span and a metric.
    Raising counts as a failure; so does setting call.error for functions that return their failure."""
    if _skip():
        yield None
        return
    try:
        from opentelemetry import context, trace
        from opentelemetry.trace import SpanKind, Status, StatusCode
        span = _tracer.start_span(f"{system} {operation}"[:160], kind=SpanKind.CLIENT,
                                  attributes={"app.system": system, "app.operation": operation,
                                              **{k: v for k, v in attrs.items() if v is not None}})
        token = context.attach(trace.set_span_in_context(span))
    except Exception:                                    # noqa: BLE001
        yield None
        return
    t0, outcome, err = time.perf_counter(), "ok", None
    box = _Box(span)
    try:
        yield box
    except BaseException as e:                           # noqa: BLE001  recorded, then raised again unchanged
        outcome, err = "error", f"{type(e).__name__}: {str(e)[:160]}"
        try:
            span.set_status(Status(StatusCode.ERROR, err))
        except Exception:                                # noqa: BLE001
            pass
        raise
    finally:
        try:
            dt = time.perf_counter() - t0
            if outcome == "ok" and box.error:
                outcome, err = "error", str(box.error)[:200]
                span.set_status(Status(StatusCode.ERROR, err))
            span.set_attribute("app.outcome", outcome)
            _h_out.record(dt, {"system": system, "operation": operation, "outcome": outcome})
            if outcome == "error":
                log_event(logging.WARNING, f"{system} {operation} failed: {err}", system=system, operation=operation)
            elif system != "model" and dt * 1000 > config.get("telemetry.slow_ms"):
                log_event(logging.WARNING, f"{system} {operation} was slow: {dt * 1000:.0f} ms", system=system, operation=operation)
            span.end()
        except Exception:                                # noqa: BLE001
            pass
        try:
            context.detach(token)
        except Exception:                                # noqa: BLE001
            pass


def traced(system: str, operation: str, failed=None):
    """Decorator form of outbound() for a plain function or method.
    failed(result) may return a reason text when the function reports failure by returning it instead of raising."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            with outbound(system, operation) as call:
                result = fn(*a, **kw)
                if failed is not None and call is not None:
                    try:
                        call.error = failed(result)
                    except Exception:                    # noqa: BLE001
                        pass
                return result
        return wrapper
    return deco


def db_call(op: str, table: str, seconds: float) -> None:
    """Time one database query (not run or audit tables, never the telemetry tables)."""
    if _skip() or str(table).startswith("otel_") or table in _csv("telemetry.exclude_tables"):
        return
    try:
        _h_db.record(seconds, {"db.operation": op})
        if seconds * 1000 > config.get("telemetry.slow_db_ms"):
            end = time.time_ns()
            s = _tracer.start_span(f"db {op} {table}"[:160], kind=_client_kind(), start_time=end - int(seconds * 1e9),
                                   attributes={"db.operation": op, "db.table": table, "app.system": "database"})
            s.end(end_time=end)
    except Exception:                                    # noqa: BLE001
        pass


def _client_kind():
    from opentelemetry.trace import SpanKind
    return SpanKind.CLIENT


def record_model_call(kind: str, seconds: float, waited: float, stats: dict) -> None:
    """One chat/title/rerank call to the model server (agent calls are not recorded here)."""
    if _skip():
        return
    try:
        _h_out.record(seconds, {"system": "model", "operation": kind, "outcome": "ok"})
        _h_queue.record(max(0.0, waited or 0.0), {"kind": kind})
        if stats.get("ttft_s") is not None:
            _h_ttft.record(stats["ttft_s"], {"kind": kind})
        end = time.time_ns()
        s = _tracer.start_span(f"model {kind}", kind=_client_kind(), start_time=end - int((seconds + (waited or 0)) * 1e9),
                               attributes={"app.system": "model", "app.operation": kind, "app.outcome": "ok",
                                           "llm.tokens_in": int(stats.get("tokens_in") or 0),
                                           "llm.tokens_out": int(stats.get("tokens_out") or 0),
                                           "llm.queue_wait_s": round(waited or 0.0, 3)})
        s.end(end_time=end)
    except Exception:                                    # noqa: BLE001
        pass


# ---------------------------------------------------------------- web middleware

class TelemetryMiddleware:
    """Pure ASGI, so it times streaming chat replies to the last byte and keeps the request's span current for everything below."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or _skip():
            return await self.app(scope, receive, send)
        if path.startswith(_csv("telemetry.exclude_paths")) or (b"x-triage-probe", b"1") in scope.get("headers", ()):
            # Not measured: run screens and the admin self-check. Mute the whole request, so the calls it makes
            # (for example the run console reading ServiceNow) do not show up as stray traces of their own.
            token = _MUTED.set(True)
            try:
                return await self.app(scope, receive, send)
            finally:
                _MUTED.reset(token)
        try:
            from opentelemetry import context, trace
            from opentelemetry.trace import SpanKind
            method = scope.get("method", "GET")
            span = _tracer.start_span(f"{method} {normalise(path)}", kind=SpanKind.SERVER,
                                      attributes={"http.request.method": method, "url.path": normalise(path)})
            token = context.attach(trace.set_span_in_context(span))
        except Exception:                                # noqa: BLE001
            return await self.app(scope, receive, send)
        status, t0, failure = {"code": 0}, time.perf_counter(), None

        async def send_wrap(message):
            if message.get("type") == "http.response.start":
                status["code"] = int(message.get("status") or 0)
            await send(message)

        try:
            await self.app(scope, receive, send_wrap)
        except BaseException as e:                       # noqa: BLE001  recorded, then raised again unchanged
            failure = f"{type(e).__name__}: {str(e)[:160]}"
            raise
        finally:
            try:
                self._finish(scope, span, method, status["code"], time.perf_counter() - t0, failure)
            except Exception:                            # noqa: BLE001
                pass
            try:
                context.detach(token)
            except Exception:                            # noqa: BLE001
                pass

    @staticmethod
    def _finish(scope, span, method, code, dt, failure):
        from opentelemetry.trace import Status, StatusCode
        route = getattr(scope.get("route"), "path", None) or ("(unmatched)" if code == 404 else normalise(scope.get("path", "")))
        feature = feature_for(route)
        bad = failure is not None or code >= 500 or code == 0
        span.update_name(f"{method} {route}"[:160])
        span.set_attributes({"http.route": route, "http.response.status_code": code, "app.feature": feature})
        if bad:
            span.set_status(Status(StatusCode.ERROR, failure or f"HTTP {code}"))
            log_event(logging.ERROR, f"{method} {route} failed: {failure or 'HTTP ' + str(code)}", route=route, feature=feature)
        span.end()
        _h_http.record(dt, {"http.request.method": method, "http.route": route,
                            "http.response.status_code": code if not failure or code else 500, "app.feature": feature})
