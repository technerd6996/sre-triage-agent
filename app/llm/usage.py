"""Token accounting for every LLM call (P0).

The Colab endpoint streams plain text and returns no token counts, so tokens are counted here
with the model's own tokenizer (loaded once in a background thread; falls back to chars / 3.5
if it cannot be downloaded). Each call is logged and saved to the llm_calls table.
"""
import contextvars
from app import config
import logging
import os
import threading

log = logging.getLogger("uvicorn.error")

_TOKENIZER_NAME = config.get("limits.tokenizer_name")
_tok = None
_tok_state = "pending"          # pending | ok | failed
_load_lock = threading.Lock()


def warm() -> str:
    """Loads the tokenizer once and returns 'ok' or 'failed'."""
    global _tok, _tok_state
    with _load_lock:
        if _tok_state != "pending":
            return _tok_state
        try:
            from transformers import AutoTokenizer
            _tok = AutoTokenizer.from_pretrained(_TOKENIZER_NAME)
            _tok_state = "ok"
        except Exception as e:
            _tok_state = "failed"
            log.warning("Tokenizer %s unavailable (%s). Estimating tokens as chars / 3.5.",
                        _TOKENIZER_NAME, str(e)[:120])
    return _tok_state


threading.Thread(target=warm, daemon=True, name="tokenizer-warmup").start()


def count_tokens(text: str) -> int:
    if not text:
        return 0
    if _tok is not None:
        try:
            return len(_tok.encode(text, add_special_tokens=False))
        except Exception:
            pass
    return int(len(text) / 3.5) + 1


def count_messages(messages: list) -> int:
    """Prompt size for a list of {"role", "content"} dicts (about 4 tokens of framing per message)."""
    return sum(count_tokens(str(m.get("content", ""))) + 4 for m in messages) + 3


# ---- which agent run a model call belongs to ----
_run_ctx = contextvars.ContextVar("triage_run", default=None)
_active = {
    "run_id": None, "kind": "triage", "skill": None, 
    "ticket": None, "step": None, "user_id": None, "model": None
}

def bind_run(run_id: str, kind: str = "triage", skill: str = None, ticket: str = None, step: str = None, user_id: str = None, model: str = None) -> None:
    ctx_data = {
        "run_id": run_id, "kind": kind, "skill": skill, 
        "ticket": ticket, "step": step, "user_id": user_id, "model": model
    }
    _run_ctx.set(ctx_data)
    _active.update(ctx_data)

def unbind_run(run_id: str) -> None:
    _run_ctx.set(None)
    _totals.pop(run_id, None)
    if _active["run_id"] == run_id:
        _active.update(run_id=None, kind="triage", skill=None, ticket=None, step=None, user_id=None, model=None)

def agent_context() -> dict:
    """Returns the full dictionary of run context for a model call."""
    ctx = _run_ctx.get()
    return ctx if ctx else _active

_totals: dict = {}      # run_id -> running token totals


def run_totals(run_id: str) -> dict:
    return dict(_totals.get(run_id) or {"tokens_in": 0, "tokens_out": 0, "calls": 0})


def log_call(metrics: dict, ctx: dict = None) -> None:
    """
    metrics expects: tokens_in, tokens_out, gen_s, wait_s, ttft_s, prompt_comp, wasted_tokens, trace_id
    """
    ctx = ctx or agent_context()
    if ctx.get("run_id"):
        t = _totals.setdefault(ctx["run_id"], {"tokens_in": 0, "tokens_out": 0, "calls": 0})
        t["tokens_in"] += metrics.get("tokens_in", 0)
        t["tokens_out"] += metrics.get("tokens_out", 0)
        t["calls"] += 1
    log.info("LLM %-7s run=%s in=%d out=%d gen=%.1fs wait=%.1fs",
             ctx["kind"], ctx["run_id"] or "-", 
             metrics.get("tokens_in", 0), metrics.get("tokens_out", 0), 
             metrics.get("gen_s", 0.0), metrics.get("wait_s", 0.0))
    try:
        from app import store
        store.add_llm_call(ctx, metrics)
    except Exception as e:
        log.warning("usage: could not save the LLM call: %s", str(e)[:150])