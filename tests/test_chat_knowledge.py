"""The chat endpoint end to end with ServiceNow knowledge: what reaches the model, what the page is told, and that the
chat still answers when knowledge fails."""
import asyncio
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import app.main as m
from app import knowledge


class _Stream:
    status_code = 200
    stats = {"tokens_in": 1, "tokens_out": 1, "ttft_s": 0.1}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, **k):
        yield "Restart nginx first, see KB0010001 for the steps and what to check afterwards."


@pytest.fixture
def chat(monkeypatch):
    sent, events = {}, []

    @contextmanager
    def slot(kind, wait_timeout=None):
        yield 0.0

    def post(messages, **kw):
        sent["messages"], sent["kw"] = messages, kw
        return _Stream()

    monkeypatch.setattr(m, "llm_slot", slot)
    monkeypatch.setattr(m, "llm_post", post)
    monkeypatch.setattr(m, "log_stream", lambda *a, **k: None)
    monkeypatch.setattr(m.store, "get_setting", lambda k, d=None: True)
    monkeypatch.setattr(m.store, "add_rag_event", lambda e: events.append(e))
    monkeypatch.setattr(m, "analyze_chat_for_incident", lambda q, h: None)
    real = m.config.get
    monkeypatch.setattr(m.config, "get", lambda k: "servicenow" if k == "knowledge.source" else real(k))
    monkeypatch.setattr(knowledge, "relationships", lambda q: "")

    def ask(query="nginx returns 502"):
        req = m.ChatRequest(query=query, history=[])
        resp = m.chat_endpoint(req, SimpleNamespace(state=SimpleNamespace(user="u@lab.local")))
        async def collect():
            return [c.decode() if isinstance(c, bytes) else c async for c in resp.body_iterator]
        lines = "".join(asyncio.run(collect())).split("\n\n")
        every = [json.loads(l[6:]) for l in lines if l.startswith("data: ") and l != "data: [DONE]"]
        ask.statuses = [e["status"] for e in every if "status" in e]          # progress lines for the page
        return [e for e in every if "status" not in e]
    ask.statuses = []
    return ask, sent, events


def _know(monkeypatch, **over):
    base = {"articles": [{"id": "KB0010001", "title": "nginx 502", "url": "https://x/kb_view.do?sysparm_article=KB0010001",
                          "text": "Restart nginx."}],
            "chunks": ["From KB0010001 (nginx 502):\nRestart nginx."], "ids": ["KB0010001"], "error": "", "seconds": 0.5}
    base.update(over)
    monkeypatch.setattr(knowledge, "retrieve", lambda q: base)


def test_the_model_gets_the_article_and_the_page_gets_the_link(chat, monkeypatch):
    ask, sent, events = chat
    _know(monkeypatch)
    out = ask()
    prompt = sent["messages"][-1]["content"]
    assert "RETRIEVED KNOWLEDGE" in prompt and "From KB0010001 (nginx 502)" in prompt and sent["kw"]["thinking"] is False
    meta = out[0]["meta"]
    assert meta["sources"] == ["KB0010001"] and meta["articles"][0]["url"].startswith("https://x/kb_view.do")
    assert events[0]["kind"] == "kb" and events[0]["retrieved"] == ["KB0010001"] and events[0]["phantom"] == []


def test_a_servicenow_failure_still_gets_an_answer_without_knowledge(chat, monkeypatch):
    ask, sent, events = chat
    _know(monkeypatch, articles=[], chunks=[], ids=[], error="ServiceNow search timed out")
    out = ask()
    assert "RETRIEVED KNOWLEDGE" not in sent["messages"][-1]["content"] and out[-1]["text"]
    assert out[0]["meta"]["sources"] == [] and events[0]["kind"] == "general"


def test_cmdb_topology_lines_are_added_to_the_knowledge_block(chat, monkeypatch):
    ask, sent, _ = chat
    _know(monkeypatch)
    monkeypatch.setattr(knowledge, "relationships", lambda q: "Topology: web01 supports checkout (criticality 1)")
    ask("what depends on web01")
    assert "Topology: web01 supports checkout" in sent["messages"][-1]["content"]


def test_an_answer_that_cites_an_article_that_was_not_retrieved_is_flagged(chat, monkeypatch):
    ask, sent, events = chat
    _know(monkeypatch, articles=[], chunks=[], ids=[])
    ask()                                               # the model's reply cites KB0010001, which was not retrieved
    assert events[0]["phantom"] == ["KB0010001"]


def test_the_page_is_told_each_real_step_in_order(chat, monkeypatch):
    ask, _, _ = chat
    _know(monkeypatch)
    monkeypatch.setattr(knowledge, "relationships", lambda q: "")
    ask()
    assert ask.statuses == ["Searching runbooks…", "Checking the CMDB…", "Waiting for the model…", "Writing the answer…"]


def test_no_search_step_is_announced_when_knowledge_is_off(chat, monkeypatch):
    ask, _, _ = chat
    real = m.config.get
    monkeypatch.setattr(m.config, "get", lambda k: "off" if k == "knowledge.source" else real(k))
    ask()
    assert "Searching runbooks…" not in ask.statuses and ask.statuses[-1] == "Writing the answer…"


def test_a_failure_while_preparing_the_answer_reaches_the_page_as_an_error(chat, monkeypatch):
    ask, _, _ = chat

    def boom(q):
        raise RuntimeError("database is down")
    monkeypatch.setattr(knowledge, "retrieve", boom)
    out = ask()
    assert out[-1]["error"].startswith("Something went wrong while preparing the answer") and "database" not in out[-1]["error"]
