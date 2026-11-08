"""Chat knowledge from ServiceNow: query building, passages, caching, reranking fallbacks, CMDB relationships."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app import config, knowledge


def _rows(*items):
    return [{"number": n, "short_description": t, "text": f"<p>{body}</p>", "sys_id": "x", "sys_updated_on": ""}
            for n, t, body in items]


@pytest.fixture
def sn(monkeypatch):
    """Fake ServiceNow: records each query and answers from `answers` (a list popped per call)."""
    calls, answers = [], []

    def fake(method, path, **kw):
        calls.append((method, path, kw.get("params", {})))
        return answers.pop(0) if answers else []
    monkeypatch.setattr(knowledge, "_request", fake)
    monkeypatch.setenv("SNOW_INSTANCE", "dev1234")
    knowledge._CACHE.clear()
    vals = {"knowledge.source": "servicenow", "knowledge.rerank": False}
    real = config.get
    monkeypatch.setattr(knowledge.config, "get", lambda k: vals[k] if k in vals else real(k))
    return SimpleNamespace(calls=calls, answers=answers, vals=vals)


def test_keywords_drop_filler_put_codes_first_and_never_carry_query_operators():
    kws = knowledge.keywords("Why is nginx returning a 502 on checkout-api?^ORactive=false")
    assert kws[0] == "502" and "nginx" in kws and "why" not in [k.lower() for k in kws] and "is" not in kws
    assert all("^" not in k and "=" not in k for k in kws)


def test_passages_keep_the_parts_that_match_the_question():
    text = "\n\n".join([f"filler paragraph number {i} " * 6 for i in range(10)] + ["Restart nginx when it returns 502 bad gateway."])
    out = knowledge.passages(text, ["nginx", "502"], 300)
    assert "502 bad gateway" in out and len(out) < 600


def test_search_asks_the_named_kb_for_published_articles_with_a_text_query(sn):
    sn.answers.append(_rows(("KB0010001", "nginx 502", "Restart nginx.")))
    res = knowledge.retrieve("nginx returns 502")
    method, path, params = sn.calls[0]
    assert path == "kb_knowledge" and "kb_knowledge_base.title=SRE Runbooks" in params["sysparm_query"]
    assert "workflow_state=published^active=true" in params["sysparm_query"] and "123TEXTQUERY321=" in params["sysparm_query"]
    assert res["ids"] == ["KB0010001"] and res["articles"][0]["url"].endswith("kb_view.do?sysparm_article=KB0010001")
    assert res["chunks"][0].startswith("From KB0010001 (nginx 502):")


def test_a_kb_name_cannot_inject_query_operators(sn):
    sn.vals["knowledge.kb_name"] = "SRE^ORactive=false"
    knowledge.retrieve("nginx 502")
    assert "SREORactive" in sn.calls[0][2]["sysparm_query"] and "SRE^OR" not in sn.calls[0][2]["sysparm_query"]


def test_when_nothing_matches_it_retries_with_the_three_longest_words(sn):
    sn.answers.extend([[], _rows(("KB0010002", "disk", "Clear logs."))])
    res = knowledge.retrieve("the application database server keeps running out of disk space overnight")
    assert len(sn.calls) == 2 and res["ids"] == ["KB0010002"]


def test_the_same_question_is_answered_from_the_cache(sn):
    sn.answers.append(_rows(("KB0010001", "t", "body")))
    sn.vals["knowledge.cache_seconds"] = 60
    knowledge.retrieve("nginx 502")
    knowledge.retrieve("nginx 502")
    assert len(sn.calls) == 1


def test_a_servicenow_failure_gives_an_empty_result_with_a_reason_not_an_exception(monkeypatch, sn):
    def boom(*a, **k):
        raise RuntimeError("SNOW 401")
    monkeypatch.setattr(knowledge, "_request", boom)
    res = knowledge.retrieve("nginx 502")
    assert res["chunks"] == [] and "SNOW 401" in res["error"]


def test_source_off_or_local_does_not_call_servicenow(sn):
    for mode in ("off", "local"):
        sn.vals["knowledge.source"] = mode
        assert knowledge.retrieve("nginx 502")["chunks"] == []
    assert sn.calls == []


# ---------------------------------------------------------------- reranking
ARTS = [{"id": f"KB000000{i}", "title": f"t{i}", "text": "x", "url": "u", "updated": ""} for i in range(1, 6)]


def _model(monkeypatch, reply=None, busy=False):
    @contextmanager
    def slot(kind, wait_timeout=None):
        if busy:
            from app.llm.gate import LLMBusy
            raise LLMBusy("busy")
        yield 0.0

    class Stream:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_content(self, **k):
            yield reply

    monkeypatch.setattr("app.llm.gate.llm_slot", slot)
    monkeypatch.setattr("app.llm.client.llm_post", lambda *a, **k: Stream())
    monkeypatch.setattr("app.llm.client.log_stream", lambda *a, **k: None)


def test_rerank_puts_the_models_choice_first(monkeypatch, sn):
    sn.vals["knowledge.rerank"] = True
    _model(monkeypatch, '{"ids": ["KB0000004", "KB0000002", "KB9999999"]}')
    assert [a["id"] for a in knowledge._rerank("q", ARTS, 3)] == ["KB0000004", "KB0000002"]


@pytest.mark.parametrize("reply", ["not json at all", '{"ids": []}', '{"ids": ["KB404"]}'])
def test_rerank_falls_back_to_servicenows_order_on_bad_output(monkeypatch, sn, reply):
    sn.vals["knowledge.rerank"] = True
    _model(monkeypatch, reply)
    assert [a["id"] for a in knowledge._rerank("q", ARTS, 3)] == ["KB0000001", "KB0000002", "KB0000003"]


def test_rerank_falls_back_when_the_model_is_busy_and_is_skipped_when_not_needed(monkeypatch, sn):
    sn.vals["knowledge.rerank"] = True
    _model(monkeypatch, busy=True)
    assert [a["id"] for a in knowledge._rerank("q", ARTS, 2)] == ["KB0000001", "KB0000002"]
    assert knowledge._rerank("q", ARTS[:2], 3) == ARTS[:2]                     # nothing to rank, no model call


# ---------------------------------------------------------------- relationships and the admin test
def test_relationships_become_topology_lines(monkeypatch):
    host = SimpleNamespace(name="web01")
    monkeypatch.setattr("app.ports.registry.get_hosts", lambda: SimpleNamespace(find_in_text=lambda q: host))
    monkeypatch.setattr("app.tools.cmdb.service_context", lambda h: {"services": [
        {"name": "checkout", "criticality": 1, "criticality_label": "1 - most critical", "owner": "Ann", "support_group": "Web"}]})
    out = knowledge.relationships("why is web01 slow")
    assert out == "Topology: web01 supports checkout (criticality 1 - most critical, owner Ann, support group Web)"


def test_no_server_named_or_cmdb_down_gives_no_relationships(monkeypatch):
    monkeypatch.setattr("app.ports.registry.get_hosts", lambda: SimpleNamespace(find_in_text=lambda q: None))
    assert knowledge.relationships("what is a 502") == ""

    def boom():
        raise RuntimeError("down")
    monkeypatch.setattr("app.ports.registry.get_hosts", boom)
    assert knowledge.relationships("web01 slow") == ""


def test_admin_test_reports_a_missing_or_empty_kb(sn):
    sn.answers.append([])
    with pytest.raises(RuntimeError, match="No knowledge base titled"):
        knowledge.test()
    sn.answers.extend([[{"sys_id": "k"}], [], []])
    with pytest.raises(RuntimeError, match="no published"):
        knowledge.test()
    sn.answers.extend([[{"sys_id": "k"}], [], _rows(("KB1", "a", "b"))])          # only drafts exist
    with pytest.raises(RuntimeError, match="none are published"):
        knowledge.test()
    sn.answers.extend([[{"sys_id": "k"}], _rows(("KB1", "a", "b"), ("KB2", "c", "d"))])
    assert "2 published articles" in knowledge.test()
