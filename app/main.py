import os
import io
import csv
import json
import re
import logging
import time
import requests
from typing import Annotated, Literal
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File
from fastapi.concurrency import run_in_threadpool
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from sentence_transformers import SentenceTransformer
from supabase import create_client, Client
from neo4j import GraphDatabase
from app.ports import registry
from app import store
from app.tools.snow import create_incident, add_work_note
from app.api.agent import _active, start_run
from app.api.issues import redact
from app.llm import usage
from app.llm.gate import LLMBusy, llm_slot
from app.api.admin import router as admin_router


# Centralized SRE persona rules
from pathlib import Path
from app.agent.prompts import SYSTEM_PROMPT

BASE_DIR = Path(__file__).resolve().parent.parent

# Load secrets from .env file
load_dotenv()

log = logging.getLogger("uvicorn.error")

app = FastAPI()
from app.auth import AuthMiddleware
app.add_middleware(AuthMiddleware)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

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
    from app.api.auth import router as auth_router
    app.include_router(auth_router)
except Exception as e:
    log.warning("Auth API not loaded: %s", e)

app.include_router(admin_router)



# --- Infrastructure Clients ---
COLAB_API_URL = os.getenv("COLAB_API_URL")
embedder = SentenceTransformer('all-MiniLM-L6-v2')
from app.secret_check import require_secret_key

supabase: Client = create_client(os.getenv("SUPABASE_URL"), require_secret_key(os.getenv("SUPABASE_KEY")))
neo4j_driver = GraphDatabase.driver(
    os.getenv("NEO4J_URI"),
    auth=(os.getenv("NEO4J_USER"), os.getenv("NEO4J_PASS")),
    max_connection_lifetime=180  # AuraDB drops idle connections; recycle before that
)

# --- Log Truncation Globals ---
MAX_FILE_CHARS = 6000
SIGNAL_PATTERN = re.compile(r'(ERROR|CRITICAL|FATAL|WARN|Exception|Traceback)', re.IGNORECASE)

# --- Request Validation Limits ---
MAX_INPUT_CHARS = 200_000        # one chat message / attached file (truncated later by smart_truncate_log)
MAX_HISTORY_MESSAGES = 30        # 15 turns x 2 messages = the frontend slider's maximum
MAX_FIELD_CHARS = 200            # graph entity / relation names (matches the CSV import limit)

# --- Graph Extraction Globals ---
EXTRACT_WINDOW_CHARS = 2500      # characters of document sent to the LLM per call
EXTRACT_MAX_WINDOWS = 4          # max LLM calls per document
MAX_TRIPLES_PER_DOC = 60         # hard cap on edges one document can create
MAX_FIELD_LEN = 80               # entity / relation names longer than this are dropped

EXTRACT_PROMPT = """You extract knowledge-graph triples from SRE documents (runbooks, SOPs, incident reports).
Return ONLY a JSON array of [subject, relation, object] triples. No prose, no markdown, no code fences.
Rules:
- subject and object are short canonical names (1-4 words) of services, components, error messages, alerts, tools, commands, causes or fixes. Keep the exact casing of technical terms (e.g. OOMKilled, kafka-broker, HikariPool).
- relation is a short lowercase verb phrase such as "is caused by", "is resolved by", "depends on", "is diagnosed with", "is a symptom of", "runs on", "triggers", "mitigates".
- Only state facts that are explicitly in the text. Maximum 15 triples.
Example output: [["CrashLoopBackOff","is caused by","OOMKilled"],["OOMKilled","is resolved by","Raising Memory Limit"]]"""


# ======================================================================
# REQUEST MODELS (validated by FastAPI; bad input returns HTTP 422)
# ======================================================================
GraphName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_FIELD_CHARS)]


class ChatMessage(BaseModel):
    """One past turn. The client may only send user/assistant roles, never 'system'."""
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(max_length=MAX_INPUT_CHARS)


class ChatRequest(BaseModel):
    query: str = Field(min_length=1, max_length=MAX_INPUT_CHARS)
    history: list[ChatMessage] = Field(default_factory=list, max_length=MAX_HISTORY_MESSAGES)

    @field_validator("query")
    @classmethod
    def query_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("query must not be blank")
        return v


class GraphEdge(BaseModel):
    src: GraphName
    rel: GraphName
    dst: GraphName


def smart_truncate_log(text: str, max_chars: int = MAX_FILE_CHARS) -> str:
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


def chunk_text(text: str, chunk_size: int = 600, overlap: int = 100) -> list[str]:
    chunks, start = [], 0
    while start < len(text):
        chunks.append(text[start:start + chunk_size].strip())
        start += chunk_size - overlap
    return [c for c in chunks if len(c) > 40]


# ======================================================================
# DOCUMENT <-> VECTOR HELPERS
# ======================================================================
def like_escape(s: str) -> str:
    """Escape LIKE wildcards so filenames like 'RB-001_x.md' match only themselves."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def doc_like_pattern(filename: str) -> str:
    return f"[{like_escape(filename)}]%"


def delete_vectors(filename: str):
    supabase.table("sre_runbooks").delete().like("content", doc_like_pattern(filename)).execute()


# ======================================================================
# LLM HELPERS
# ======================================================================
def call_llm(messages: list, timeout: int = 180) -> str:
    out = ""
    tokens_in = usage.count_messages(messages)
    with llm_slot("ingest", wait_timeout=None) as waited:
        t0 = time.time()
        with requests.post(COLAB_API_URL, json={"messages": messages}, stream=True, timeout=timeout) as r:
            for chunk in r.iter_content(chunk_size=512, decode_unicode=True):
                if chunk:
                    out += chunk
        seconds = time.time() - t0
    usage.log_call("ingest", None, tokens_in, usage.count_tokens(out), seconds, waited)
    return out


def _clean(s: str) -> str:
    s = re.sub(r"\s+", " ", s.strip().strip("\"'`")).strip()
    return s if 0 < len(s) <= MAX_FIELD_LEN else ""


def parse_triples(raw: str) -> list[tuple]:
    """Parses the LLM output defensively. Anything malformed is dropped."""
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:
        return []
    out = []
    for t in data if isinstance(data, list) else []:
        if isinstance(t, (list, tuple)) and len(t) == 3 and all(isinstance(x, str) for x in t):
            s, r, d = _clean(t[0]), _clean(t[1]).lower(), _clean(t[2])
            if s and r and d and s.lower() != d.lower():
                out.append((s, r, d))
    return out


# ======================================================================
# STRUCTURED (DETERMINISTIC) EXTRACTION FROM FRONT MATTER
# ======================================================================
def split_front_matter(text: str):
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        k, _, v = line.partition(":")
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            meta[k.strip()] = [x.strip() for x in v[1:-1].split(",") if x.strip()]
        else:
            meta[k.strip()] = v
    return meta, text[m.end():]


def structured_triples(meta: dict) -> list[tuple]:
    doc_id = meta.get("id")
    if not doc_id:
        return []
    dtype = meta.get("type", "")
    out = []
    title = meta.get("title")
    if title:
        rel = {"runbook": "is a runbook for", "sop": "defines",
               "incident_triage": "is an incident report about"}.get(dtype, "covers")
        out.append((doc_id, rel, title))
    for svc in meta.get("services", []):
        out.append((doc_id, "involves", svc))
    for alert in meta.get("alerts", []):
        out.append((alert, "is handled by" if dtype == "runbook" else "fired during", doc_id))
    for ref in meta.get("related", []):
        out.append((doc_id, "is related to", ref))
    return out


# ======================================================================
# GRAPH INGEST / SAFE REMOVAL (with per-document provenance)
# ======================================================================
# Every edge created from a document stores the filename in r.docs (a list).
#   - Edges with NO r.docs (seed CSV, manual additions) are PROTECTED: a document
#     upload/delete never modifies or removes them.
#   - An edge shared by several documents keeps living until its LAST document is deleted.
#   - Entity nodes are only removed if they were touched by the deleted document AND have
#     no relationships left. Nothing outside that set is ever touched.
UPSERT_EDGES = """
UNWIND $rows AS row
MERGE (a:Entity {name: row.src})
MERGE (b:Entity {name: row.dst})
MERGE (a)-[r:RELATED_TO {type: row.rel}]->(b)
ON CREATE SET r.docs = [$doc]
ON MATCH SET r.docs = CASE
    WHEN r.docs IS NULL THEN NULL
    WHEN $doc IN r.docs THEN r.docs
    ELSE r.docs + $doc END
"""


def extract_all_triples(filename: str, text: str):
    meta, body = split_front_matter(text)
    triples = structured_triples(meta)
    llm_error = None
    try:
        limit = EXTRACT_WINDOW_CHARS * EXTRACT_MAX_WINDOWS
        for i in range(0, min(len(body), limit), EXTRACT_WINDOW_CHARS):
            window = body[i:i + EXTRACT_WINDOW_CHARS]
            if len(window.strip()) < 80:
                continue
            raw = call_llm([
                {"role": "system", "content": EXTRACT_PROMPT},
                {"role": "user", "content": window},
            ])
            triples += parse_triples(raw)
    except Exception as e:
        llm_error = str(e)

    # De-duplicate (case-insensitive) and cap
    seen, unique = set(), []
    for s, r, d in triples:
        key = (s.lower(), r.lower(), d.lower())
        if key not in seen:
            seen.add(key)
            unique.append((s, r, d))
    return unique[:MAX_TRIPLES_PER_DOC], llm_error


def ingest_graph(filename: str, text: str) -> dict:
    triples, llm_error = extract_all_triples(filename, text)
    if not triples:
        return {"edges": 0, "llm_error": llm_error}

    with neo4j_driver.session() as session:
        # Reuse existing entity names (case-insensitive) so we don't create duplicates
        index = {rec["name"].lower(): rec["name"]
                 for rec in session.run("MATCH (e:Entity) RETURN e.name AS name")}
        rows = []
        for s, r, d in triples:
            s = index.setdefault(s.lower(), s)
            d = index.setdefault(d.lower(), d)
            rows.append({"src": s, "rel": r, "dst": d})
        session.run(UPSERT_EDGES, rows=rows, doc=filename)
    return {"edges": len(rows), "llm_error": llm_error}


def remove_doc_graph(filename: str) -> dict:
    """Removes only what belongs exclusively to this document."""
    def _tx(tx):
        touched = tx.run(
            "MATCH (a:Entity)-[r:RELATED_TO]->(b:Entity) WHERE $doc IN r.docs "
            "RETURN a.name AS a, b.name AS b, size(r.docs) AS owners",
            doc=filename).data()
        if not touched:
            return {"edges_removed": 0, "edges_kept_shared": 0, "nodes_removed": 0}

        names = list({x for row in touched for x in (row["a"], row["b"])})
        removed = sum(1 for row in touched if row["owners"] == 1)
        shared = len(touched) - removed

        # Detach this document from every edge it owns; delete edges nobody else owns
        tx.run(
            "MATCH ()-[r:RELATED_TO]->() WHERE $doc IN r.docs "
            "WITH r, [d IN r.docs WHERE d <> $doc] AS remaining "
            "SET r.docs = remaining "
            "WITH r, remaining WHERE size(remaining) = 0 "
            "DELETE r",
            doc=filename)

        # Delete only touched entities that now have no relationships at all
        res = tx.run(
            "MATCH (e:Entity) WHERE e.name IN $names AND NOT (e)--() "
            "WITH e, e.name AS nm DELETE e RETURN count(nm) AS c",
            names=names).single()
        return {"edges_removed": removed, "edges_kept_shared": shared,
                "nodes_removed": res["c"] if res else 0}

    with neo4j_driver.session() as session:
        return session.execute_write(_tx)

@app.get("/admin", response_class=HTMLResponse)
async def serve_admin_ui():
    with open(BASE_DIR / "static" / "admin.html", "r", encoding="utf-8") as f:
        return f.read()

# ---- COLAB PING STATUS ----
@app.get("/api/health")
def health_check():
    try:
        # Extract base ngrok URL (e.g., https://xxxx.ngrok-free.app)
        base_url = COLAB_API_URL.rsplit("/", 1)[0]
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


@app.get("/knowledge", response_class=HTMLResponse)
async def serve_knowledge_ui():
    with open(BASE_DIR / "static" / "knowledge.html", "r", encoding="utf-8") as f:
        return f.read()


@app.get("/agent", response_class=HTMLResponse)
async def serve_agent_ui():
    with open(BASE_DIR / "static" / "agent.html", "r", encoding="utf-8") as f:
        return f.read()


# --- HYBRID RAG (SUPABASE) MANAGEMENT ---
@app.get("/api/rag/documents")
def list_documents():
    """Extracts unique filenames from the database, plus how many graph edges each owns."""
    res = supabase.table("sre_runbooks").select("content").execute()
    docs = {}
    for row in res.data:
        match = re.match(r'^\[(.*?)\]', row['content'])
        if match:
            fname = match.group(1)
            docs[fname] = docs.get(fname, 0) + 1

    edge_counts = {}
    try:
        with neo4j_driver.session() as session:
            for rec in session.run(
                "MATCH ()-[r:RELATED_TO]->() WHERE r.docs IS NOT NULL "
                "UNWIND r.docs AS d RETURN d AS doc, count(r) AS n"):
                edge_counts[rec["doc"]] = rec["n"]
    except Exception:
        pass

    return [{"filename": k, "chunks": v, "edges": edge_counts.get(k, 0)}
            for k, v in sorted(docs.items())]

def retrieval_query(query: str, history: list) -> str:
    q = query.strip()
    if len(q.split()) < 8 and history:          # short follow-up like "How to fix it?"
        prev = [m["content"] for m in history if m["role"] == "user"]
        if prev:
            q = prev[-1][:300] + " " + q        # add the previous user question
    return q

def _index_document(filename: str, text: str) -> dict:
    """Replace-then-ingest: removes the old version completely, then indexes the new one."""
    # 1. Remove previous version (vectors + graph ownership)
    graph_removed = remove_doc_graph(filename)
    delete_vectors(filename)

    # 2. Vectors
    chunks = chunk_text(text)
    records = []
    for chunk in chunks:
        tagged = f"[{filename}] {chunk}"
        records.append({"content": tagged, "embedding": embedder.encode(tagged).tolist()})
    for i in range(0, len(records), 50):
        supabase.table("sre_runbooks").insert(records[i:i + 50]).execute()

    # 3. Graph (LLM + front matter). A failure here never blocks the vector index.
    try:
        graph = ingest_graph(filename, text)
    except Exception as e:
        graph = {"edges": 0, "llm_error": str(e)}

    msg = f"Indexed {len(chunks)} chunks, {graph['edges']} graph edges."
    if graph.get("llm_error"):
        msg += f" (LLM extraction warning: {graph['llm_error']})"
    return {"message": msg, "chunks": len(chunks), "edges": graph["edges"],
            "replaced": graph_removed}


@app.post("/api/rag/upload")
async def upload_document(file: UploadFile = File(...)):
    """Uploads or modifies a document, then auto-builds its graph edges."""
    text = (await file.read()).decode("utf-8", errors="ignore")
    return await run_in_threadpool(_index_document, file.filename, text)


@app.delete("/api/rag/documents/{filename}")
def delete_document(filename: str):
    """Deletes the document's vectors and every graph edge/node that belongs only to it."""
    graph = remove_doc_graph(filename)
    delete_vectors(filename)
    return {"message": "Document deleted.", "graph": graph}


# --- GRAPH RAG (NEO4J) MANAGEMENT ---
@app.get("/api/rag/graph")
def list_graph_edges():
    query = ("MATCH (src:Entity)-[r:RELATED_TO]->(dst:Entity) "
             "RETURN src.name AS src, r.type AS rel, dst.name AS dst, r.docs AS docs "
             "ORDER BY src, rel, dst")
    with neo4j_driver.session() as session:
        return [{"src": rec["src"], "rel": rec["rel"], "dst": rec["dst"],
                 "source": ", ".join(rec["docs"]) if rec["docs"] else "seed / manual"}
                for rec in session.run(query)]


@app.post("/api/rag/graph")
def add_graph_edge(edge: GraphEdge):
    # REMOVE r.docs: a manually added edge becomes protected (owned by no document)
    query = ("MERGE (src:Entity {name: $src}) MERGE (dst:Entity {name: $dst}) "
             "MERGE (src)-[r:RELATED_TO {type: $rel}]->(dst) REMOVE r.docs")
    with neo4j_driver.session() as session:
        session.run(query, src=edge.src, dst=edge.dst, rel=edge.rel)
    return {"message": "Topology added."}


@app.delete("/api/rag/graph")
def delete_graph_edge(src: str, rel: str, dst: str):
    query = "MATCH (src:Entity {name: $src})-[r:RELATED_TO {type: $rel}]->(dst:Entity {name: $dst}) DELETE r"
    with neo4j_driver.session() as session:
        session.run(query, src=src, dst=dst, rel=rel)
    return {"message": "Topology deleted."}


# --- GRAPH CSV IMPORT ---
MAX_CSV_ROWS = 5000
CSV_COLUMN_ALIASES = {
    "src": ["src", "source", "from", "subject", "entity1"],
    "rel": ["rel", "relation", "relationship", "type", "predicate"],
    "dst": ["dst", "destination", "target", "to", "object", "entity2"],
}


def parse_graph_csv(raw: str):
    """Returns (rows, skipped, error). Accepts headers src/rel/dst or common aliases."""
    reader = csv.reader(io.StringIO(raw))
    try:
        header = [h.strip().lower() for h in next(reader)]
    except StopIteration:
        return [], 0, "The file is empty."

    cols = {}
    for key, aliases in CSV_COLUMN_ALIASES.items():
        for a in aliases:
            if a in header:
                cols[key] = header.index(a)
                break
    if len(cols) < 3:
        return [], 0, "CSV needs a header row with columns: src, rel, dst."

    rows, skipped = [], 0
    for line in reader:
        if len(rows) >= MAX_CSV_ROWS:
            skipped += 1
            continue
        try:
            s, r, d = (re.sub(r"\s+", " ", line[cols[k]]).strip() for k in ("src", "rel", "dst"))
        except IndexError:
            skipped += 1
            continue
        if s and r and d and max(len(s), len(r), len(d)) <= MAX_FIELD_CHARS:
            rows.append({"src": s, "rel": r, "dst": d})
        else:
            skipped += 1
    return rows, skipped, None


def _import_graph_rows(rows: list) -> int:
    with neo4j_driver.session() as session:
        # Reuse existing entity names (case-insensitive) to avoid duplicate nodes
        index = {rec["name"].lower(): rec["name"]
                 for rec in session.run("MATCH (e:Entity) RETURN e.name AS name")}
        for row in rows:
            row["src"] = index.setdefault(row["src"].lower(), row["src"])
            row["dst"] = index.setdefault(row["dst"].lower(), row["dst"])
        # REMOVE r.docs: CSV edges are protected (owned by no document), like manual edges
        query = """
        UNWIND $rows AS row
        MERGE (a:Entity {name: row.src})
        MERGE (b:Entity {name: row.dst})
        MERGE (a)-[r:RELATED_TO {type: row.rel}]->(b)
        REMOVE r.docs
        """
        for i in range(0, len(rows), 200):
            session.run(query, rows=rows[i:i + 200])
    return len(rows)


@app.post("/api/rag/graph/upload")
async def upload_graph_csv(file: UploadFile = File(...)):
    raw = (await file.read()).decode("utf-8-sig", errors="ignore")
    rows, skipped, error = parse_graph_csv(raw)
    if error:
        return JSONResponse(status_code=400, content={"message": error})
    loaded = await run_in_threadpool(_import_graph_rows, rows)
    return {"message": f"Imported {loaded} edges from {file.filename}"
                       + (f" ({skipped} rows skipped)." if skipped else "."),
            "loaded": loaded, "skipped": skipped}

# ---- TITLE INFERENCE ----

class TitleRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_INPUT_CHARS)


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

    try:
        full_title = ""
        tokens_in = usage.count_messages(colab_messages)
        with llm_slot("chat", wait_timeout=15) as waited:
            t0 = time.time()
            with requests.post(COLAB_API_URL, json={"messages": colab_messages},
                               stream=True, timeout=(10, 60)) as r:
                if r.status_code != 200:
                    return {"title": "Incident Triage"}
                for chunk in r.iter_content(chunk_size=512, decode_unicode=True):
                    if chunk:
                        full_title += chunk
        usage.log_call("chat", None, tokens_in, usage.count_tokens(full_title), time.time() - t0, waited)

        title = re.sub(r'[\"\'\[\]]', '', full_title).strip()
        # Reject HTML/error pages and anything that isn't a short plain title
        if not title or "<" in title or ">" in title or len(title) > 60:
            return {"title": "Incident Triage"}
        return {"title": title}
    except Exception:
        return {"title": "Incident Triage"}


# --- CHAT HELPERS ---
TIMESTAMP_LINE_RE = re.compile(r'^\s*\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}', re.MULTILINE)
STACK_FRAME_RE = re.compile(r'^\s+at\s+[\w.$<>]+\(', re.MULTILINE)
# (?<!...)/(?!...) instead of \b, because \b does not match before "_" in names like RB-003_pod.md
DOC_ID_RE = re.compile(r'(?<![A-Za-z0-9])(?:RB-\d{3}|SOP-\d{3}|INC-\d{4}-\d{3})(?!\d)', re.IGNORECASE)


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


def retrieved_doc_ids(graph_ctx: list, raw_chunks: list) -> list:
    """IDs of the documents that retrieval actually returned (chunk filenames + graph lines)."""
    ids = []
    for content in raw_chunks:
        m = re.match(r"^\[(.*?)\]", content)
        ids += DOC_ID_RE.findall(m.group(1) if m else "")
    for line in graph_ctx:
        ids += DOC_ID_RE.findall(line)
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

OFFTOPIC_RE = re.compile(r"^\s*I['\u2019]?m a triage assistant", re.IGNORECASE)
OFFTOPIC_REPLY = ("I'm a triage assistant, so I can only help with error logs, stack traces, and specific "
                  "SRE or infrastructure questions. Try pasting a log, or ask something like why nginx "
                  "returns a 502 or what is impacted if redis is down.")
# --- CHAT INFERENCE ---
# Plain 'def' (not 'async def'): everything inside is blocking I/O (Neo4j, Supabase, embeddings),
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

    # 2. Retrieve Subgraph (Graph RAG). A failure here must not kill the chat.
    cypher = """
    MATCH (n:Entity)-[r:RELATED_TO]->(m:Entity)
    WHERE toLower($query_text) CONTAINS toLower(n.name)
       OR toLower($query_text) CONTAINS toLower(m.name)
    RETURN n.name AS src, r.type AS rel, m.name AS dst
    ORDER BY size(n.name) + size(m.name) DESC
    LIMIT 10
    """
    graph_ctx = []
    _t = time.time()
    try:
        with neo4j_driver.session() as session:
            for rec in session.run(cypher, query_text=safe_query):
                graph_ctx.append(f"Topology: {rec['src']} {rec['rel']} {rec['dst']}")
    except Exception as e:
        log.warning("Graph retrieval failed, continuing without it: %s", e)

    # 3. Retrieve Runbooks (Hybrid RAG). Same rule: degrade gracefully.
    t_graph = time.time() - _t
    _t = time.time()
    supa_ctx, raw_chunks, rag_scores = [], [], []
    try:
        res = supabase.rpc("hybrid_search", {
            "query_text": safe_query,
            "query_embedding": embedder.encode(safe_query).tolist(),
            "match_count": 3
        }).execute()
        raw_chunks = [d['content'] for d in res.data] if res.data else []
        rag_scores = [{k: v for k, v in d.items() if k != 'content' and isinstance(v, (int, float))}
                      for d in (res.data or [])]
        supa_ctx = [format_chunk(c) for c in raw_chunks]
    except Exception as e:
        log.warning("Hybrid retrieval failed, continuing without it: %s", e)

    # Document IDs that retrieval really returned (the UI uses these for the source label)
    t_vec = time.time() - _t
    sources = retrieved_doc_ids(graph_ctx, raw_chunks)

    # 4. Combine Context and Augment Prompt
    combined = "\n".join(graph_ctx) + "\n\n" + "\n\n".join(supa_ctx)
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
    colab_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + safe_history + [{"role": "user", "content": prompt}]


    # Detect a live issue so the UI can offer the Create incident button (chat never creates tickets)
    incident_info = analyze_chat_for_incident(safe_query, safe_history)

        # 6. Stream Generator connecting to Colab; failures are sent to the browser as readable errors
    def stream_generator():
        yield f"data: {json.dumps({'meta': {'sources': sources, 'incident': incident_info}})}\n\n"
        tokens_in = usage.count_messages(colab_messages)
        sent = []                              # text actually streamed to the browser, for token counting
        try:
            with llm_slot("chat") as waited:
                t0 = time.time()
                with requests.post(COLAB_API_URL, json={"messages": colab_messages},
                                   stream=True, timeout=(10, 300)) as r:
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
                                    sent.append(OFFTOPIC_REPLY)
                                    yield f"data: {json.dumps({'text': OFFTOPIC_REPLY})}\n\n"
                                    break             # drop whatever advice the model added
                                chunk = head
                            sent.append(chunk)
                            yield f"data: {json.dumps({'text': chunk})}\n\n"
                        if not decided and head:      # very short reply
                            text = OFFTOPIC_REPLY if OFFTOPIC_RE.match(head) else head
                            sent.append(text)
                            yield f"data: {json.dumps({'text': text})}\n\n"
            usage.log_call("chat", None, tokens_in, usage.count_tokens("".join(sent)), time.time() - t0, waited)
            _ans = "".join(sent)
            _cited = list(dict.fromkeys(i.upper() for i in DOC_ID_RE.findall(_ans)))
            _kind = ("offtopic" if OFFTOPIC_RE.match(_ans) else
                     "kb" if set(_cited) & set(sources) else "general")
            store.add_rag_event({"user_hash": _uh, "query": redact(safe_query)[:300], "kind": _kind,
                "retrieved": sources, "scores": rag_scores, "graph_hits": len(graph_ctx),
                "cited": _cited, "phantom": [c for c in _cited if c not in sources],
                "t_graph": round(t_graph, 2), "t_vec": round(t_vec, 2), "t_total": round(time.time() - _t_start, 2)})
        except LLMBusy:
            yield f"data: {json.dumps({'error': 'The AI model is busy with another task. Please try again in a moment.'})}\n\n"
        except Exception as e:
            log.error("LLM request failed: %s", e)
            yield f"data: {json.dumps({'error': f'LLM server unreachable: {e}'})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream_generator(), media_type="text/event-stream")