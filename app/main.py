import os
import json
import re
import logging
import time
import requests
from typing import Annotated, Literal
from app import bootstrap_store

bootstrap_store.load()             # database connection and encryption key (no .env file; see bootstrap_store.py)
from app import config             # noqa: E402
from app.agent import prompts as _prompts  # noqa: F401  (registers the prompt settings)
config.hydrate_environ()           # saved settings and secrets fill the environment before anything reads it

from pydantic import AfterValidator
from fastapi import FastAPI, UploadFile, File
from fastapi.concurrency import run_in_threadpool
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from app import knowledge
from app.ports import registry
from app import store
from app.tools.snow import create_incident, add_work_note
from app.api.agent import _active, start_run
from app.api.issues import redact
from app.llm import usage
from app.llm.gate import LLMBusy, llm_slot
from app.llm.client import llm_post, log_stream
from app.api.admin import router as admin_router

# Centralized SRE persona rules
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

log = logging.getLogger("uvicorn.error")

app = FastAPI()
from app.auth import AuthMiddleware
from app import bootstrap
app.add_middleware(AuthMiddleware)
from app import telemetry
app.add_middleware(telemetry.TelemetryMiddleware)       # added after Auth, so it is outermost and times everything
app.router.on_startup.append(telemetry.setup)
from app import backup  # noqa: E402
app.router.on_startup.append(backup.start)            # daily database + key-file backup, made by the app itself      # OpenTelemetry providers + tables, once the app is starting


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Basic hardening headers: no framing (clickjacking), no MIME sniffing, no referrer leaks."""
    response = await call_next(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response
bootstrap.announce_setup_if_needed()          # prints the setup code in the console on a fresh install
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

try:
    from app.tools.host_lock import start_watchdog
    start_watchdog()    # warns in the log if a per-host lock is ever held unusually long (a leak)
except Exception as e:
    log.warning("Host-lock watchdog not started: %s", e)

try:
    from app.api.agent import router as agent_router
    app.include_router(agent_router)
except Exception as e:  # the chat must keep working even if the agent side fails to load
    log.warning("Agent API not loaded: %s", e)
try:
    from app.api.issues import router as issues_router
    app.include_router(issues_router)
except Exception as e:
    log.warning("Issues API not loaded: %s", e)
try:
    from app.api.health import router as health_router
    app.include_router(health_router)
except Exception as e:
    log.warning("Health API not loaded: %s", e)
try:
    from app.api.approver_chat import router as approver_chat_router
    app.include_router(approver_chat_router)
except Exception as e:
    log.warning("Approver-chat API not loaded: %s", e)
try:
    from app.api.auth import router as auth_router
    app.include_router(auth_router)
except Exception as e:
    log.warning("Auth API not loaded: %s", e)

app.include_router(admin_router)
from app.api.telemetry_api import router as telemetry_router
app.include_router(telemetry_router)
from app.api.config_pane import router as config_pane_router
app.include_router(config_pane_router)
from app.api.setup import router as setup_router
app.include_router(setup_router)



# --- Infrastructure Clients ---
_EMB = None


def _embedder():
    """Only the old 'local' knowledge source needs this model (about 1 GB), so it loads on first use."""
    global _EMB
    if _EMB is None:
        from sentence_transformers import SentenceTransformer
        _EMB = SentenceTransformer('all-MiniLM-L6-v2')
    return _EMB


from app import store as _store

supabase = _store.supabase

# --- Log Truncation Globals ---
SIGNAL_PATTERN = re.compile(r'(ERROR|CRITICAL|FATAL|WARN|Exception|Traceback)', re.IGNORECASE)

# --- Request Validation Limits ---
def _check_input(v: str) -> str:
    if len(v) > config.get("limits.max_input_chars"):       # one chat message / attached file
        raise ValueError(f"must be at most {config.get('limits.max_input_chars')} characters")
    return v


InputText = Annotated[str, AfterValidator(_check_input)]

class ChatMessage(BaseModel):
    """One past turn. The client may only send user/assistant roles, never 'system'."""
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: InputText


class ChatRequest(BaseModel):
    query: InputText = Field(min_length=1)
    history: list[ChatMessage] = Field(default_factory=list)

    @field_validator("history")
    @classmethod
    def history_not_too_long(cls, v: list) -> list:
        if len(v) > config.get("limits.max_history_messages"):
            raise ValueError(f"at most {config.get('limits.max_history_messages')} messages")
        return v

    @field_validator("query")
    @classmethod
    def query_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("query must not be blank")
        return v


def smart_truncate_log(text: str, max_chars: int | None = None) -> str:
    max_chars = max_chars or config.get("limits.max_file_chars")
    """Extracts signal lines from massive logs using regex to prevent LLM context overflow."""
    if len(text) <= max_chars:
        return text
    lines = text.splitlines()
    kept, seen = [], set()
    for i, line in enumerate(lines):
        if SIGNAL_PATTERN.search(line):
            for j in range(max(0, i - 1), min(len(lines), i + 3)):
                if j not in seen:
                    seen.add(j)
                    kept.append((j, lines[j]))
    kept.sort(key=lambda x: x[0])
    signal_text = "\n".join(l for _, l in kept)
    if signal_text and len(signal_text) <= max_chars:
        return f"[Note: file truncated for length]\n\n{signal_text}"
    half = max_chars // 2
    return text[:half] + f"\n\n... [truncated] ...\n\n" + text[-half:]


# ======================================================================
@app.get("/setup", response_class=HTMLResponse)
async def serve_setup_ui():
    with open(BASE_DIR / "static" / "setup.html", "r", encoding="utf-8") as f:
        return f.read()


@app.get("/admin", response_class=HTMLResponse)
async def serve_admin_ui():
    with open(BASE_DIR / "static" / "admin.html", "r", encoding="utf-8") as f:
        return f.read()

# ---- COLAB PING STATUS ----
@app.get("/api/health")
def health_check():
    try:
        # Extract base ngrok URL (e.g., https://xxxx.ngrok-free.app)
        base_url = os.getenv("COLAB_API_URL", "").split("/v1")[0]
        res = requests.get(f"{base_url}/health", timeout=3)
        if res.status_code == 200:
            return {"status": "online"}
    except Exception:
        pass
    return {"status": "offline"}


# --- PAGE ROUTING ---
@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    page = "index.html" if store.get_setting("chat_enabled", True) else "chat_off.html"
    with open(BASE_DIR / "static" / page, "r", encoding="utf-8") as f:
        return f.read()


@app.get("/agent", response_class=HTMLResponse)
async def serve_agent_ui():
    with open(BASE_DIR / "static" / "agent.html", "r", encoding="utf-8") as f:
        return f.read()


def retrieval_query(query: str, history: list) -> str:
    q = query.strip()
    if len(q.split()) < 8 and history:          # short follow-up like "How to fix it?"
        prev = [m["content"] for m in history if m["role"] == "user"]
        if prev:
            q = prev[-1][:300] + " " + q        # add the previous user question
    return q

# ---- TITLE INFERENCE ----

class TitleRequest(BaseModel):
    text: InputText = Field(min_length=1)


@app.post("/api/title")
def generate_title(req: TitleRequest):
    # 1. Scrub the "[Attached file: ...]" metadata from the prompt
    clean_text = re.sub(r'\[Attached file:[^\]]+\]', '', req.text).strip()

    # Fallback: if the user attached an empty file, just use the filename
    if not clean_text:
        match = re.search(r'\[Attached file:\s*([^\]]+)\]', req.text)
        clean_text = match.group(1) if match else req.text

    # 2. Truncate text to save tokens
    trunc_text = clean_text[:400]

    # 3. Stricter Prompt
    sys_msg = "Summarize the core topic of the following incident or text into a concise title using up to 6 words. Do not use quotes, punctuation, or include file names. Return ONLY the title."
    colab_messages = [
        {"role": "system", "content": sys_msg},
        {"role": "user", "content": trunc_text}
    ]

    fallback = _fallback_title(clean_text)
    try:
        full_title = ""
        with llm_slot("title", wait_timeout=15) as waited:
            t0 = time.time()
            with llm_post(colab_messages, timeout=(10, 60), max_tokens=48, thinking=False) as r:
                if r.status_code != 200:
                    return {"title": fallback}
                for chunk in r.iter_content(chunk_size=512, decode_unicode=True):
                    if chunk:
                        full_title += chunk
        log_stream("title", r, time.time() - t0, waited)

        title = re.sub(r"[\"'\[\]]", "", full_title).strip()
        # Reject HTML/error pages and anything that isn't a short plain title
        if not title or "<" in title or ">" in title or len(title) > 60:
            return {"title": fallback}
        return {"title": title}
    except Exception:
        return {"title": fallback}


def _fallback_title(text: str) -> str:
    """Used when the model is busy or fails: the ticket number, else the first six words."""
    m = re.search(r"\bINC\d{5,}\b", text or "", re.I)
    if m:
        return f"{m.group(0).upper()} follow-up"
    words = re.sub(r"[^\w\s-]", " ", text or "").split()[:6]
    return " ".join(words)[:60] or "Incident Triage"


# --- CHAT HELPERS ---
TIMESTAMP_LINE_RE = re.compile(r'^\s*\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}', re.MULTILINE)
STACK_FRAME_RE = re.compile(r'^\s+at\s+[\w.$<>]+\(', re.MULTILINE)
# (?<!...)/(?!...) instead of \b, because \b does not match before "_" in names like RB-003_pod.md
DOC_ID_RE = re.compile(r'(?<![A-Za-z0-9])(?:RB-\d{3}|SOP-\d{3}|INC-\d{4}-\d{3}|KB\d{7})(?!\d)', re.IGNORECASE)


def looks_like_log(text: str) -> bool:
    """True if the text is incident evidence: timestamped log lines, stack frames, or many error-signal lines."""
    timestamps = len(TIMESTAMP_LINE_RE.findall(text))
    signals = sum(1 for line in text.splitlines() if SIGNAL_PATTERN.search(line))
    frames = len(STACK_FRAME_RE.findall(text))
    return (timestamps >= 2
            or (timestamps >= 1 and signals >= 1)
            or frames >= 2
            or signals >= 4
            or "Traceback (most recent call last)" in text)


def retrieved_doc_ids(raw_chunks: list) -> list:
    """IDs of the documents that retrieval actually returned (chunk filenames)."""
    ids = []
    for content in raw_chunks:
        m = re.match(r"^\[(.*?)\]", content)
        ids += DOC_ID_RE.findall(m.group(1) if m else "")
    return list(dict.fromkeys(i.upper() for i in ids))  # unique, original order


def format_chunk(content: str) -> str:
    """Turns '[RB-009_x.md] text' into 'From RB-009_x.md:' + newline + text, so the model doesn't copy the bracket tag."""
    m = re.match(r"^\[(.*?)\]\s*(.*)$", content, re.DOTALL)
    return f"From {m.group(1)}:\n{m.group(2)}" if m else content

# ---- Chat -> "Create incident" detection ----
# Chat never creates tickets. It only tells the UI to show the Create incident button.
ISSUE_HINT_RE = re.compile(
    r"\b(down|unreachable|offline|timeout|timed out|cannot connect|can't connect|"
    r"not responding|not running|isn't running|is not running|stopped|inactive|"
    r"dead|failing|disk|space|full|c:|high cpu|high memory|locked|lockout|lock out|unlock|"
    r"cannot login|can't login|login failed|error|failed|crash|alert|issue|problem|"
    r"broken|outage|won't start|not starting)\b",
    re.IGNORECASE,
)
CONCEPT_QUESTION_RE = re.compile(
    r"^\s*(what\s+(is|are|does)|how\s+does|explain\b|why\s+would\s+a\b|"
    r"what\s+is\s+impacted|what\s+depends\s+on|what\s+connects\s+to)",
    re.IGNORECASE,
)
HOWTO_RE = re.compile(
    r"^\s*(how\s+(do|can|to|should)|can\s+you\s+(explain|tell|show)|tell\s+me|show\s+me|"
    r"steps\s+to|what\s+should)\b",
    re.IGNORECASE,
)
EXPLICIT_TICKET_RE = re.compile(
    r"\b(create|raise|open|log|file)\s+(a\s+|an\s+)?(ticket|incident)\b", re.IGNORECASE)


def analyze_chat_for_incident(query: str, history: list[dict]) -> dict | None:
    """Decides whether the UI offers the Create incident button.
    mode 'issue'    : the message reports a live problem.
    mode 'explicit' : the user asked for a ticket. The issue text is this message,
                      or the last issue the user described earlier in the chat."""
    suggested = None
    q = query.strip()
    is_log = looks_like_log(q)
    explicit = bool(EXPLICIT_TICKET_RE.search(q)) and not is_log
    issue_text = q

    if explicit:
        if not ISSUE_HINT_RE.search(q):
            issue_text = next(
                (m["content"] for m in reversed(history)
                 if m["role"] == "user"
                 and not EXPLICIT_TICKET_RE.search(m["content"])
                 and (looks_like_log(m["content"]) or ISSUE_HINT_RE.search(m["content"]))),
                "")
    elif not is_log:
        if DOC_ID_RE.search(q) or re.search(r"\b(runbook|sop)\b", q, re.IGNORECASE):
            return None
        if CONCEPT_QUESTION_RE.search(q) or HOWTO_RE.search(q):
            return None
        if not ISSUE_HINT_RE.search(q):
            return None

    if issue_text:
        try:
            host = registry.get_hosts().find_in_text(issue_text)
            if host:
                suggested = host.name or host.id
        except Exception as e:
            log.warning("Host lookup in chat failed: %s", e)

    return {"status": "needs_server", "mode": "explicit" if explicit else "issue",
            "description": issue_text[:1800], "suggested_server": suggested}


def canned_stream(text: str, incident: dict | None):
    """A fixed reply in the same stream format, so no RAG lookup or model call is needed."""
    def gen():
        yield f"data: {json.dumps({'meta': {'sources': [], 'incident': incident, 'kind': 'canned'}})}\n\n"
        yield f"data: {json.dumps({'text': text})}\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")

def _status(text: str) -> str:
    """A progress line for the page: what the server is really doing right now (shown above the answer until it starts)."""
    return f"data: {json.dumps({'status': text})}\n\n"


OFFTOPIC_RE = re.compile(r"^\s*I['\u2019]?m a triage assistant", re.IGNORECASE)
OFFTOPIC_REPLY = ("I'm a triage assistant, so I can only help with error logs, stack traces, and specific "
                  "SRE or infrastructure questions. Try pasting a log, or ask something like why nginx "
                  "returns a 502 or what is impacted if redis is down.")
config.register("prompt.offtopic", "Reply to off-topic chat messages", OFFTOPIC_REPLY, "text", "prompts", multiline=True, max_len=40000)
# --- CHAT INFERENCE ---
# Plain 'def' (not 'async def'): everything inside is blocking I/O (Postgres, embeddings),
# so FastAPI runs it in a worker thread instead of freezing the event loop.
@app.post("/api/chat")
def chat_endpoint(req: ChatRequest, request: Request):
    import hashlib
    from app.api.issues import redact
    _t_start = time.time()
    _uh = hashlib.sha256(str(getattr(request.state, "user", "")).encode()).hexdigest()[:12]
    if not store.get_setting("chat_enabled", True):
        raise HTTPException(403, "Chat is switched off by an administrator.")
    # 1. Truncate massive pasted logs or attached files immediately
    safe_query = smart_truncate_log(req.query)

    # 1b. Older user turns may also hold full attached files, so truncate them the same way
    safe_history = [
        {"role": m.role,
         "content": smart_truncate_log(m.content) if m.role == "user" else m.content}
        for m in req.history
    ]

    # 1c. Scenario routing: a ticket request never reaches RAG or the model
    incident_info = analyze_chat_for_incident(safe_query, safe_history)
    if incident_info and incident_info["mode"] == "explicit":
        text = ("Sure, I can raise a ticket for that. Check the details below and click **Create incident**."
                if incident_info["description"] else
                "Sure, I can raise a ticket. Click **Create incident**, pick the caller and the affected server, "
                "and describe what is going wrong.")
        store.add_rag_event({"user_hash": _uh, "query": redact(safe_query)[:300], "kind": "canned"})
        return canned_stream(text, incident_info)

    def stream_generator():
        # 2. Runbook knowledge from ServiceNow (or the old local store). A failure here must not kill the chat.
        _t = time.time()
        supa_ctx, raw_chunks, rag_scores, articles, sources = [], [], [], [], []
        source_mode = config.get("knowledge.source")
        if source_mode in ("servicenow", "local"):
            yield _status("Searching runbooks…")
        if source_mode == "servicenow":
            know = knowledge.retrieve(retrieval_query(safe_query, safe_history))
            supa_ctx, sources, articles = know["chunks"], know["ids"], know["articles"]
            rag_scores = [{"rank": i + 1} for i in range(len(sources))]
            if know["error"]:
                log.warning("Knowledge search: %s", know["error"])
        elif source_mode == "local":
            try:
                res = supabase.rpc("hybrid_search", {
                    "query_text": safe_query,
                    "query_embedding": _embedder().encode(safe_query).tolist(),
                    "match_count": 3
                }).execute()
                raw_chunks = [d['content'] for d in res.data] if res.data else []
                rag_scores = [{k: v for k, v in d.items() if k != 'content' and isinstance(v, (int, float))}
                              for d in (res.data or [])]
                supa_ctx = [format_chunk(c) for c in raw_chunks]
            except Exception as e:
                log.warning("Hybrid retrieval failed, continuing without it: %s", e)
            sources = retrieved_doc_ids(raw_chunks)
        if config.get("knowledge.relationships"):
            yield _status("Checking the CMDB…")
        topology = knowledge.relationships(safe_query)       # live CMDB relationships when a server is named
        if topology:
            supa_ctx = supa_ctx + [topology]
        t_vec = time.time() - _t

        # 4. Combine Context and Augment Prompt
        combined = "\n\n".join(supa_ctx)
        rag_block = (f"--- RETRIEVED KNOWLEDGE (RAG) ---\n{combined.strip()}\n---------------------------------"
                     if combined.strip() else "")

        if looks_like_log(safe_query):
            # Incident evidence: the log comes FIRST, knowledge after it, and the mode is stated explicitly
            prompt = (f"INCIDENT EVIDENCE (Mode 1):\n{safe_query}\n\n"
                      + (f"{rag_block}\n\n" if rag_block else "")
                      + "Respond in the Mode 1 format. Your reply must start with 'SEVERITY:'. "
                        "Use the retrieved knowledge only to name matching runbook IDs inside checklist steps; "
                        "do not summarize it.")
        elif rag_block:
            prompt = f"{rag_block}\n\n{safe_query}"
        else:
            prompt = safe_query

        # 5. Construct Payload (Inject System Prompt Backend-Side)
        colab_messages = [{"role": "system", "content": config.get("prompt.system") + "\n\n" + config.get("prompt.grounding")}] + safe_history + [{"role": "user", "content": prompt}]


        # Detect a live issue so the UI can offer the Create incident button (chat never creates tickets)
        incident_info = analyze_chat_for_incident(safe_query, safe_history)

        yield f"data: {json.dumps({'meta': {'sources': sources, 'incident': incident_info, 'articles': [{k: a[k] for k in ('id', 'title', 'url')} for a in articles]}})}\n\n"
        tokens_in = usage.count_messages(colab_messages)
        sent = []                              # text actually streamed to the browser, for token counting
        try:
            yield _status("Waiting for the model…")
            with llm_slot("chat") as waited:
                yield _status("Writing the answer…")
                t0 = time.time()
                with llm_post(colab_messages, timeout=(10, 300), thinking=False) as r:
                    if r.status_code != 200:
                        yield f"data: {json.dumps({'error': f'LLM server returned HTTP {r.status_code}. Is the Colab session alive?'})}\n\n"
                    else:
                        head, decided = "", False
                        for chunk in r.iter_content(chunk_size=512, decode_unicode=True):
                            if not chunk:
                                continue
                            if not decided:
                                head += chunk
                                if len(head) < 40:
                                    continue          # wait until we can tell what kind of reply this is
                                decided = True
                                if OFFTOPIC_RE.match(head):
                                    sent.append(config.get("prompt.offtopic"))
                                    yield f"data: {json.dumps({'text': config.get('prompt.offtopic')})}\n\n"
                                    break             # drop whatever advice the model added
                                chunk = head
                            sent.append(chunk)
                            yield f"data: {json.dumps({'text': chunk})}\n\n"
                        if not decided and head:      # very short reply
                            text = config.get("prompt.offtopic") if OFFTOPIC_RE.match(head) else head
                            sent.append(text)
                            yield f"data: {json.dumps({'text': text})}\n\n"
            log_stream("chat", r, time.time() - t0, waited, _uh)
            _ans = "".join(sent)
            _cited = list(dict.fromkeys(i.upper() for i in DOC_ID_RE.findall(_ans)))
            _kind = ("offtopic" if OFFTOPIC_RE.match(_ans) else
                     "kb" if set(_cited) & set(sources) else "general")
            store.add_rag_event({"user_hash": _uh, "query": redact(safe_query)[:300], "kind": _kind,
                "retrieved": sources, "scores": rag_scores, "graph_hits": 0,
                "cited": _cited, "phantom": [c for c in _cited if c not in sources],
                "t_graph": 0, "t_vec": round(t_vec, 2), "t_total": round(time.time() - _t_start, 2)})
        except LLMBusy:
            yield f"data: {json.dumps({'error': 'The AI model is busy with another task. Please try again in a moment.'})}\n\n"
        except Exception as e:
            log.error("LLM request failed: %s", e)
            # the exception text can carry the model endpoint URL, so it stays in the server log only
            yield f"data: {json.dumps({'error': 'LLM server unreachable. Check the server log for details.'})}\n\n"
        yield "data: [DONE]\n\n"


    def safe_stream():
        # The search and prompt steps now run inside the stream, so a failure there must reach the page as a readable error.
        try:
            yield from stream_generator()
        except Exception as e:
            log.error("Chat failed before the model was reached: %s", e)
            yield f"data: {json.dumps({'error': 'Something went wrong while preparing the answer. Check the server log for details.'})}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(safe_stream(), media_type="text/event-stream")

from app import rbac
rbac.warn_unruled(app)

