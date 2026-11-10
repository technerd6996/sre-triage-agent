"""App telemetry (OpenTelemetry): pure helpers, the summary maths, what is excluded, and the real SDK pipeline against a fake store."""
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config, rbac, telemetry
from app.llm import usage


# ---------------------------------------------------------------- pure helpers

def test_quantile_interpolates_inside_a_bucket_and_never_exceeds_the_recorded_max():
    bounds = [0.1, 0.5, 1.0]
    buckets = [0, 10, 0, 0]                                   # ten calls, all between 0.1 and 0.5 s
    assert 0.1 < telemetry.quantile(bounds, buckets, 0.5, 0.4) <= 0.4
    assert telemetry.quantile(bounds, buckets, 0.99, 0.2) <= 0.2
    assert telemetry.quantile(bounds, [0, 0, 0, 0], 0.5) is None
    assert telemetry.quantile(bounds, [0, 0, 0, 3], 0.5, 7.0) <= 7.0          # the open-ended last bucket uses the max


@pytest.mark.parametrize("route,feature", [
    ("/api/approver-chat/inquire", "approver chat: checks"),
    ("/api/approver-chat/cmdb", "approver chat: checks"),
    ("/api/approver-chat/act/confirm", "approver chat: actions"),
    ("/api/chat", "chat"), ("/api/title", "chat"), ("/api/issues/{n}", "tickets from chat"),
    ("/api/auth/login", "sign-in"), ("/api/admin/audit", "admin"), ("/admin", "pages"), ("/", "pages"),
    ("/something/else", "other")])
def test_routes_are_grouped_into_areas(route, feature):
    assert telemetry.feature_for(route) == feature


def test_routes_without_a_template_lose_their_ids():
    assert telemetry.normalise("/api/agent/runs/3f2a9c1e-77ab-4c1d-9d0e-aabbccddeeff") == "/api/agent/runs/{id}"
    assert telemetry.normalise("/api/issues/INC0010714/note") == "/api/issues/{id}/note"
    assert telemetry.normalise("/api/servers") == "/api/servers"


def _hist(name, attrs, n, total, buckets, vmax, ts=1000.0):
    return {"name": name, "kind": "histogram", "attrs": attrs, "ts": ts, "n_count": n, "total": total, "vmax": vmax,
            "bounds": telemetry.BOUNDS, "buckets": buckets, "value": None}


def _gauge(name, value, ts=1000.0):
    return {"name": name, "kind": "gauge", "attrs": {}, "ts": ts, "value": value}


def test_summary_counts_failures_by_area_and_converts_units():
    b = [0] * (len(telemetry.BOUNDS) + 1)
    fast = list(b); fast[4] = 10                               # 10 requests between 50 and 100 ms
    slow = list(b); slow[9] = 2                                # 2 requests between 2.5 and 5 s
    rows = [
        _hist("http.server.request.duration", {"http.request.method": "GET", "http.route": "/api/chat", "http.response.status_code": 200, "app.feature": "chat"}, 10, 0.8, fast, 0.1),
        _hist("http.server.request.duration", {"http.request.method": "GET", "http.route": "/api/chat", "http.response.status_code": 500, "app.feature": "chat"}, 2, 8.0, slow, 5.0),
        _hist("http.server.request.duration", {"http.request.method": "GET", "http.route": "/api/x", "http.response.status_code": 404, "app.feature": "other"}, 3, 0.03, fast, 0.01),
        _hist("triage.outbound.duration", {"system": "servicenow", "operation": "GET incident", "outcome": "ok"}, 5, 1.0, fast, 0.3),
        _hist("triage.outbound.duration", {"system": "servicenow", "operation": "GET incident", "outcome": "error"}, 1, 5.0, slow, 5.0),
        _hist("triage.model.queue.wait", {"kind": "chat"}, 4, 2.0, fast, 0.9),
        _gauge("process.cpu.utilization", 0.25), _gauge("process.memory.usage", 104857600.0), _gauge("process.thread.count", 12),
    ]
    s = telemetry.summarize(rows, 24, now=2000.0)
    assert s["requests"]["total"] == 15 and s["requests"]["errors"] == 2 and s["requests"]["client_errors"] == 3
    assert s["requests"]["error_rate"] == round(2 / 15 * 100, 2)
    chat = next(f for f in s["features"] if f["feature"] == "chat")
    assert chat["requests"] == 12 and chat["errors"] == 2
    assert s["outbound"][0]["system"] == "servicenow" and s["outbound"][0]["errors"] == 1 and s["outbound"][0]["calls"] == 6
    assert s["model"]["queue_wait"][0]["kind"] == "chat"
    assert s["process"]["cpu_percent"]["last"] == 25.0 and s["process"]["memory_mb"]["last"] == 100.0
    assert s["series"][0]["requests"] == 15 and s["series"][0]["errors"] == 2 and s["step"] == 900
    assert s["routes"][0]["route"] == "GET /api/chat"                            # slowest p95 first


def test_summary_of_nothing_is_empty_not_an_error():
    s = telemetry.summarize([], 24)
    assert s["requests"]["total"] == 0 and s["requests"]["p95_ms"] is None and s["series"] == []


# ---------------------------------------------------------------- permission

def test_telemetry_pages_need_monitor_view_and_nothing_else_opens_them():
    for path in ("/api/admin/telemetry/summary", "/api/admin/telemetry/status", "/api/admin/telemetry/logs",
                 "/api/admin/telemetry/traces", "/api/admin/telemetry/traces/" + "a" * 32):
        assert rbac.rule_for("GET", path) == ("monitor.view",), path
    assert not rbac.allows("users", rbac.rule_for("GET", "/api/admin/telemetry/summary"))
    assert rbac.allows("auditors", rbac.rule_for("GET", "/api/admin/telemetry/summary"))
    assert rbac.rule_for("POST", "/api/admin/telemetry/summary") == ("config.manage",)    # no write route; the admin catch-all is stricter


# ---------------------------------------------------------------- the real SDK pipeline against a fake store

class FakeStore:
    def __init__(self):
        self.rows = {"otel_spans": [], "otel_metrics": [], "otel_logs": []}

    def table(self, name):
        outer = self

        class Q:
            def insert(self, rows):
                outer.rows[name].extend(rows if isinstance(rows, list) else [rows])
                return self

            def execute(self):
                return self
        return Q()


@pytest.fixture
def tel(monkeypatch):
    """Real OpenTelemetry providers, writing into memory. Restores the module afterwards."""
    fake = FakeStore()
    monkeypatch.setattr(telemetry, "_table", fake.table)
    monkeypatch.setattr(telemetry, "ensure_tables", lambda: None)
    monkeypatch.setattr(telemetry, "_retention_loop", lambda: None)
    monkeypatch.setattr(telemetry, "_pool_stats", lambda: {"pool_size": 2, "pool_available": 1, "requests_waiting": 0})
    for name, val in (("_ready", False), ("_providers", ()), ("_tracer", None), ("_h_http", None), ("_h_out", None),
                      ("_h_db", None), ("_h_queue", None), ("_h_ttft", None)):
        monkeypatch.setattr(telemetry, name, val)
    monkeypatch.setattr(telemetry, "_otel_logger", None)
    assert telemetry.setup() is True
    yield fake
    telemetry.shutdown()


def _app():
    app = FastAPI()
    app.add_middleware(telemetry.TelemetryMiddleware)

    @app.get("/api/approver-chat/inquire")
    def ok():
        with telemetry.outbound("servicenow", "GET incident"):
            pass
        return {"ok": 1}

    @app.get("/api/issues/{n}")
    def one(n: int):
        return {"n": n}

    @app.get("/api/admin/boom")
    def boom():
        with telemetry.outbound("mail", "send") as call:
            call.error = "SMTP refused"                     # a function that reports failure by returning it
        raise RuntimeError("kaboom")

    @app.get("/api/agent/runs")
    def runs():
        return {}

    @app.get("/api/inside")
    def inside():
        usage.bind_run("t-run")
        try:
            with telemetry.outbound("servicenow", "GET incident"):
                pass
        finally:
            usage.unbind_run("t-run")
        return {}
    return TestClient(app, raise_server_exceptions=False)


def _names(fake):
    return [r["name"] for r in fake.rows["otel_spans"]]


def test_requests_become_traces_metrics_and_logs(tel):
    c = _app()
    assert c.get("/api/approver-chat/inquire").status_code == 200
    assert c.get("/api/issues/77").status_code == 200
    assert c.get("/api/admin/boom").status_code == 500
    telemetry.flush()
    names = _names(tel)
    assert "GET /api/approver-chat/inquire" in names and "servicenow GET incident" in names
    assert "GET /api/issues/{n}" in names                                   # the route template, not /api/issues/77
    assert not any("77" in n for n in names)
    root = next(r for r in tel.rows["otel_spans"] if r["name"] == "GET /api/approver-chat/inquire")
    child = next(r for r in tel.rows["otel_spans"] if r["name"] == "servicenow GET incident")
    assert child["parent_id"] == root["span_id"] and child["trace_id"] == root["trace_id"] and root["parent_id"] is None
    assert root["attrs"]["app.feature"] == "approver chat: checks" and root["attrs"]["http.response.status_code"] == 200
    bad = next(r for r in tel.rows["otel_spans"] if r["name"] == "GET /api/admin/boom")
    assert bad["status"] == "error" and "kaboom" in bad["error"]
    mail = next(r for r in tel.rows["otel_spans"] if r["name"] == "mail send")
    assert mail["status"] == "error" and mail["error"] == "SMTP refused"      # failure by return value counts
    logs = tel.rows["otel_logs"]
    assert any(l["severity"] == "ERROR" and l["trace_id"] == bad["trace_id"] for l in logs)
    assert any(l["severity"] == "WARNING" and "SMTP refused" in l["body"] for l in logs)
    names = {m["name"] for m in tel.rows["otel_metrics"]}
    assert {"http.server.request.duration", "triage.outbound.duration", "process.memory.usage", "db.pool.in_use"} <= names
    rows = [m for m in tel.rows["otel_metrics"] if m["name"] == "http.server.request.duration"]
    assert sum(m["n_count"] for m in rows) == 3
    assert {m["attrs"]["http.response.status_code"] for m in rows} == {200, 500}


def test_runs_are_never_measured(tel):
    c = _app()
    assert c.get("/api/agent/runs").status_code == 200                       # an excluded path
    assert c.get("/api/inside").status_code == 200                           # a call made inside a run context
    telemetry.flush()
    names = _names(tel)
    assert "GET /api/agent/runs" not in names
    assert "servicenow GET incident" not in names                            # dropped: it ran inside a run
    assert "GET /api/inside" in names                                        # the request itself is the app's


def test_a_muted_thread_records_nothing(tel):
    seen = []

    def worker():
        telemetry.mute()
        with telemetry.outbound("servicenow", "GET incident"):
            pass
        seen.append(telemetry._skip())
    t = threading.Thread(target=worker)
    t.start(); t.join()
    telemetry.flush()
    assert seen == [True] and _names(tel) == []


def test_switching_telemetry_off_stops_collection_at_once(tel, monkeypatch):
    real = config.get
    monkeypatch.setattr(config, "get", lambda k: False if k == "telemetry.enabled" else real(k))
    _app().get("/api/approver-chat/inquire")
    telemetry.flush()
    assert _names(tel) == []


def test_tail_sampling_keeps_failed_and_slow_traces_even_at_zero_percent(tel, monkeypatch):
    real = config.get
    monkeypatch.setattr(config, "get", lambda k: {"telemetry.trace_sample_percent": 0, "telemetry.slow_ms": 50}.get(k, None) if k in
                        ("telemetry.trace_sample_percent", "telemetry.slow_ms") else real(k))
    c = _app()
    c.get("/api/approver-chat/inquire")                                     # fast and fine: sampled away
    c.get("/api/admin/boom")                                                # failed: always kept
    telemetry.flush()
    names = _names(tel)
    assert "GET /api/approver-chat/inquire" not in names and "servicenow GET incident" not in names
    assert "GET /api/admin/boom" in names and "mail send" in names          # the whole failed trace, children included
    telemetry.flush()
    rows = [m for m in tel.rows["otel_metrics"] if m["name"] == "http.server.request.duration"]
    assert sum(m["n_count"] for m in rows) == 2                              # metrics still count every request


def test_database_timing_skips_run_tables_and_its_own_tables(tel):
    telemetry.db_call("select", "settings", 0.01)
    telemetry.db_call("select", "runs", 0.01)
    telemetry.db_call("select", "otel_spans", 0.01)
    telemetry.db_call("select", "audit", 0.01)
    telemetry.flush()
    rows = [m for m in tel.rows["otel_metrics"] if m["name"] == "db.client.operation.duration"]
    assert sum(m["n_count"] for m in rows) == 1


def test_a_slow_query_gets_its_own_trace_entry(tel, monkeypatch):
    real = config.get
    monkeypatch.setattr(config, "get", lambda k: 10 if k == "telemetry.slow_db_ms" else real(k))
    telemetry.db_call("select", "settings", 0.5)
    telemetry.db_call("select", "settings", 0.001)
    telemetry.flush()
    assert _names(tel) == ["db select settings"]


def test_a_model_call_records_queue_wait_first_token_and_a_span(tel):
    telemetry.record_model_call("chat", 2.0, 0.4, {"ttft_s": 0.3, "tokens_in": 100, "tokens_out": 50})
    telemetry.flush()
    names = {m["name"] for m in tel.rows["otel_metrics"]}
    assert {"triage.model.queue.wait", "triage.model.ttft", "triage.outbound.duration"} <= names
    span = next(r for r in tel.rows["otel_spans"] if r["name"] == "model chat")
    assert span["attrs"]["llm.tokens_out"] == 50 and span["attrs"]["llm.queue_wait_s"] == 0.4


def test_telemetry_that_is_not_started_does_nothing_and_never_raises(monkeypatch):
    monkeypatch.setattr(telemetry, "_ready", False)
    with telemetry.outbound("servicenow", "GET incident") as call:
        assert call is None
    telemetry.db_call("select", "settings", 1.0)
    telemetry.record_model_call("chat", 1.0, 0.0, {})
    telemetry.log_event(40, "x")
    telemetry.annotate(outcome="ok")
    assert telemetry.traced("x", "y")(lambda: 5)() == 5


def test_excluded_requests_are_muted_so_the_calls_they_make_leave_no_stray_traces(tel):
    app = FastAPI()
    app.add_middleware(telemetry.TelemetryMiddleware)

    @app.get("/api/agent/queue")
    def queue():                                               # the run console reading ServiceNow
        with telemetry.outbound("servicenow", "GET incident"):
            pass
        return {}

    @app.get("/api/probe-me")
    def probe():
        with telemetry.outbound("servicenow", "GET incident"):
            pass
        return {}
    c = TestClient(app)
    assert c.get("/api/agent/queue").status_code == 200
    assert c.get("/api/probe-me", headers={"X-Triage-Probe": "1"}).status_code == 200     # the admin self-check
    telemetry.flush()
    assert _names(tel) == [] and not any(m["name"] == "http.server.request.duration" for m in tel.rows["otel_metrics"])
    assert c.get("/api/probe-me").status_code == 200                                      # a real request is measured again
    telemetry.flush()
    assert "GET /api/probe-me" in _names(tel) and "servicenow GET incident" in _names(tel)


def test_the_knowledge_search_thread_keeps_the_requests_trace(tel, monkeypatch):
    import contextvars
    from opentelemetry import trace
    seen = {}

    def work():
        seen["ctx"] = trace.get_current_span().get_span_context().trace_id
    with telemetry.outbound("servicenow", "parent") as call:
        want = call.span.get_span_context().trace_id
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(1) as ex:
            ex.submit(contextvars.copy_context().run, work).result()
    assert seen["ctx"] == want
