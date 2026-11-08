"""Streaming client for chat, title and ingest calls (vLLM, OpenAI-style). Agent tool calls use ColabChat."""
import json
import os
import time

from app import config
from app.llm import usage
from app.llm.colab_chat import _SESSION, post_with_retry


class _Stream:
    """Acts like the old requests response for main.py: .status_code, .iter_content(), 'with' block."""

    def __init__(self, messages: list, timeout, max_tokens=None, thinking=None):
        self.messages, self.timeout = messages, timeout
        self.max_tokens, self.thinking = max_tokens, thinking
        self.stats = {"tokens_in": usage.count_messages(messages), "tokens_out": 0, "ttft_s": None}
        self.status_code, self._resp, self._text = None, None, []

    def __enter__(self):
        payload = {"model": config.get("model.name"), "messages": self.messages, "stream": True,
                   "temperature": config.get("model.temperature"), "max_tokens": self.max_tokens or config.get("model.max_tokens"),
                   "stream_options": {"include_usage": True}}
        if self.thinking is False:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        self._t0 = time.time()
        self._resp = post_with_retry(os.getenv("COLAB_API_URL"), payload, self.timeout)
        self._resp.encoding = "utf-8"
        self.status_code = self._resp.status_code
        return self

    def __exit__(self, *exc):
        if self._resp is not None:
            self._resp.close()
        if not self.stats["tokens_out"]:
            self.stats["tokens_out"] = usage.count_tokens("".join(self._text))
        return False

    def iter_content(self, chunk_size=512, decode_unicode=True):
        for line in self._resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                continue
            try:
                c = json.loads(data)
            except ValueError:
                continue
            if c.get("usage"):
                self.stats["tokens_in"] = c["usage"].get("prompt_tokens", self.stats["tokens_in"])
                self.stats["tokens_out"] = c["usage"].get("completion_tokens", 0)
            ch = c.get("choices") or []
            piece = (ch[0].get("delta") or {}).get("content") if ch else None
            if piece:
                if self.stats["ttft_s"] is None:
                    self.stats["ttft_s"] = time.time() - self._t0
                self._text.append(piece)
                yield piece


def llm_post(messages: list, timeout=(10, 180), max_tokens=None, thinking=None) -> _Stream:
    """max_tokens caps this call only (default: model.max_tokens); thinking=False turns reasoning off for it."""
    return _Stream(messages, timeout, max_tokens, thinking)


def log_stream(kind: str, s: _Stream, seconds: float, waited: float, user_id: str = None) -> None:
    model = config.get("model.name")
    usage.log_call(
        {"tokens_in": s.stats["tokens_in"], "tokens_out": s.stats["tokens_out"], "gen_s": seconds,
         "wait_s": waited, "ttft_s": s.stats["ttft_s"], "prompt_comp": {}, "wasted_tokens": 0, "model": model},
        ctx={"run_id": None, "kind": kind, "skill": None, "ticket": None, "step": None,
             "user_id": user_id, "model": model})