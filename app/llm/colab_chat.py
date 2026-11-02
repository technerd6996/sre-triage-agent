"""LangChain wrapper for the existing Colab /generate endpoint.
Place at: app/llm/colab_chat.py

The Colab server is unchanged: it takes {"messages": [...]} and streams plain text.
Tool calling is done through the prompt: the model is told to answer with one JSON
object {"tool": "...", "args": {...}} when it wants a tool, and plain text when done.
"""
import json
import logging
import time
import uuid
from typing import Any, List, Optional

import requests
from app.llm import usage
from app.llm.gate import llm_slot
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field

log = logging.getLogger("uvicorn.error")

TOOL_INSTRUCTIONS = """You can use tools. Available tools:
{tool_list}

RULES:
- To use a tool, reply with ONLY one JSON object and nothing else:
  {{"tool": "<tool_name>", "args": {{<arguments>}}}}
- Call at most ONE tool per reply. Wait for its result before continuing.
- Use only the tools and argument names listed above.
- Once resolve_incident or escalate_incident succeeds, DO NOT call any more tools.
- When you have finished, reply in plain text with no JSON."""

TERMINAL_TOOLS = {"resolve_incident", "escalate_incident"}

_SESSION = requests.Session()      # one keep-alive connection, reused by every call

RETRY_STATUS = {429, 502, 503, 504}
BACKOFF = (0, 3, 10, 25)

def post_with_retry(url, payload, timeout):
    """POST to the model server. Retries dropped connections and busy responses
    (429/502/503/504) with backoff. Only the response status is retried, never a reply
    that has already started streaming."""
    last = None
    for wait in BACKOFF:
        if wait:
            time.sleep(wait)
        try:
            r = _SESSION.post(url, json=payload, stream=True, timeout=timeout)
        except requests.exceptions.ConnectionError as e:
            last = e
            _SESSION.close()           # drop the dead connection so the retry opens a fresh one
            continue
        if r.status_code in RETRY_STATUS:
            last = requests.exceptions.HTTPError(f"Model server busy: HTTP {r.status_code}")
            r.close()
            continue
        return r
    raise last

class ColabChat(BaseChatModel):
    url: str                                   # COLAB_API_URL (the /generate endpoint)
    timeout: int = 180
    tools_spec: List[dict] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "colab-generate"

    # ---- LangChain calls this so create_agent can "bind" tools to the model ----
    def bind_tools(self, tools: list, **kwargs: Any) -> "ColabChat":
        specs = [convert_to_openai_tool(t)["function"] for t in tools]
        return self.model_copy(update={"tools_spec": specs})

    # ---- message conversion ----
    def _tool_prompt(self) -> str:
        lines = []
        for s in self.tools_spec:
            props = s.get("parameters", {}).get("properties", {})
            args = ", ".join(f"{k}: {v.get('type', 'any')}" for k, v in props.items())
            lines.append(f"- {s['name']}({args}): {s.get('description', '')}")
        return TOOL_INSTRUCTIONS.format(tool_list="\n".join(lines))

    def _to_colab(self, messages: List[BaseMessage]) -> list:
        out, system_parts = [], []
        for m in messages:
            if m.type == "system":
                system_parts.append(m.content)
            elif m.type == "human":
                out.append({"role": "user", "content": m.content})
            elif m.type == "ai":
                if m.tool_calls:
                    tc = m.tool_calls[0]
                    content = json.dumps({"tool": tc["name"], "args": tc["args"]})
                else:
                    content = m.content
                out.append({"role": "assistant", "content": content})
            elif isinstance(m, ToolMessage):
                if m.name in TERMINAL_TOOLS and '"error"' not in str(m.content):
                    follow_up = ("The incident is now closed or escalated. Do NOT call any more tools. "
                                 "Reply with a two-sentence plain-text summary and no JSON.")
                else:
                    follow_up = "Continue with the next step in the playbook. Call ONE tool or give the final answer."
                body = str(m.content)
                if len(body) > 800:
                    body = body[:800] + " ...[truncated]"
                out.append({"role": "user",
                            "content": f"[TOOL RESULT from {m.name}]: {body}\n{follow_up}"})
        if self.tools_spec:
            system_parts.append(self._tool_prompt())
        if system_parts:
            out.insert(0, {"role": "system", "content": "\n\n".join(system_parts)})
        return out

    # ---- parse the model's text into a tool call or a normal answer ----
    def _parse(self, text: str) -> AIMessage:
        names = {s["name"] for s in self.tools_spec}
        decoder = json.JSONDecoder()
        i = text.find("{")
        
        json_failed = False
        
        while self.tools_spec and i != -1:
            try:
                obj, _ = decoder.raw_decode(text[i:])
                if isinstance(obj, dict) and obj.get("tool") in names:
                    args = obj.get("args") or {}
                    if isinstance(args, dict):
                        return AIMessage(content="", tool_calls=[{
                            "name": obj["tool"], "args": args,
                            "id": f"call_{uuid.uuid4().hex[:8]}", "type": "tool_call"}])
            except ValueError:
                json_failed = True
                pass
            i = text.find("{", i + 1)
            
        # If it tried to output JSON but failed, flag it in the active context
        if json_failed:
            ctx = usage.agent_context()
            ctx["wasted_tokens"] = usage.count_tokens(text)
            
        return AIMessage(content=text.strip())

    def _open(self, payload: dict, timeout):
        """POST over a reused connection, with retry on dropped connections and busy responses."""
        return post_with_retry(self.url, payload, timeout)

    def _generate(self, messages: List[BaseMessage], stop: Optional[List[str]] = None,
                  run_manager: Any = None, **kwargs: Any) -> ChatResult:
        msgs = self._to_colab(messages)
        ctx = usage.agent_context()
        
        # Calculate prompt composition (System vs History vs Latest Action)
        comp = {"system": 0, "history": 0, "latest": 0}
        for i, m in enumerate(msgs):
            toks = usage.count_tokens(m.get("content", ""))
            if m.get("role") == "system":
                comp["system"] += toks
            elif i == len(msgs) - 1:
                comp["latest"] += toks
            else:
                comp["history"] += toks
        
        # Initial estimates in case the stream breaks early
        tokens_in = usage.count_messages(msgs)
        tokens_out = 0
        text = ""
        ttft = None
        
        from app import config
        model_name = config.get("model.name")
        usage_exact = False
        payload = {
            "model": model_name,
            "messages": msgs,
            "stream": True,
            "temperature": config.get("model.temperature"),
            "max_tokens": config.get("model.max_tokens"),
            "stream_options": {"include_usage": True},
            "chat_template_kwargs": {"enable_thinking": False},
        }
        
        with llm_slot(ctx["kind"]) as waited:
            t0 = time.time()
            with self._open(payload, config.get("model.timeout_seconds")) as r:
                r.raise_for_status()
                for line in r.iter_lines(decode_unicode=True):
                    if line and line.startswith("data: "):
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            continue          # keep reading to the end so the connection can be reused
                        try:
                            chunk = json.loads(data_str)
                            # Capture exact token usage from the final vLLM chunk
                            if chunk.get("usage"):
                                usage_exact = True
                                tokens_in = chunk["usage"].get("prompt_tokens", tokens_in)
                                tokens_out = chunk["usage"].get("completion_tokens", tokens_out)
                            
                            delta = chunk["choices"][0].get("delta", {}) if chunk.get("choices") else {}
                            if "content" in delta and delta["content"]:
                                if ttft is None:
                                    ttft = time.time() - t0
                                text += delta["content"]
                        except (json.JSONDecodeError, IndexError):
                            continue
            seconds = time.time() - t0
        
        if not usage_exact:
            log.warning("LLM server sent no usage block: token counts for this call are estimates")
        if not tokens_out:
            tokens_out = usage.count_tokens(text)
            
        wasted = ctx.get("wasted_tokens", 0)
        if wasted > 0:
            ctx["wasted_tokens"] = 0  # Reset for next call
            
        metrics = {
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "gen_s": seconds,
            "wait_s": waited,
            "ttft_s": ttft,
            "prompt_comp": comp,
            "wasted_tokens": wasted,
            "model": model_name
        }
        
        usage.log_call(metrics)
        return ChatResult(generations=[ChatGeneration(message=self._parse(text))])