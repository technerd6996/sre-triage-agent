"""Token accounting for every LLM call (P0).

The Colab endpoint streams plain text and returns no token counts, so tokens are counted here
with the model's own tokenizer (loaded once in a background thread; falls back to chars / 3.5
if it cannot be downloaded). Each call is logged and saved to the llm_calls table.
"""
import contextvars
import logging
import os
import threading

log = logging.getLogger("uvicorn.error")

_TOKENIZER_NAME = os.getenv("TOKENIZER_NAME", "Qwen/Qwen2.5-7B-Instruct")
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
_active = {"run_id": None, "kind": "triage"}   # fallback: only one agent run is active at a time


def bind_run(run_id: str, kind: str = "triage") -> None:
    _run_ctx.set((run_id, kind))
    _active.update(run_id=run_id, kind=kind)


def unbind_run(run_id: str) -> None:
    _run_ctx.set(None)
    if _active["run_id"] == run_id:
        _active.update(run_id=None, kind="triage")


def agent_context() -> tuple:
    """(run_id, kind) for a model call made by the agent. kind is 'triage' or 'resume'."""
    ctx = _run_ctx.get()
    if ctx:
        return ctx
    return _active["run_id"], _active["kind"]


def log_call(kind: str, run_id, tokens_in: int, tokens_out: int, seconds: float, waited: float = 0.0) -> None:
    log.info("LLM %-7s run=%s in=%d out=%d gen=%.1fs wait=%.1fs",
             kind, run_id or "-", tokens_in, tokens_out, seconds, waited)
    try:
        from app import store
        store.add_llm_call(kind, run_id, tokens_in, tokens_out, seconds, waited)
    except Exception as e:
        log.warning("usage: could not save the LLM call: %s", str(e)[:150])