"""LangChain wrapper for the existing Colab /generate endpoint.
Place at: app/llm/colab_chat.py

The Colab server is unchanged: it takes {"messages": [...]} and streams plain text.
Tool calling is done through the prompt: the model is told to answer with one JSON
object {"tool": "...", "args": {...}} when it wants a tool, and plain text when done.
"""
import json
import uuid
from typing import Any, List, Optional

import requests
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field

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
                out.append({"role": "user",
                            "content": f"[TOOL RESULT from {m.name}]: {m.content}\n{follow_up}"})
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
                pass
            i = text.find("{", i + 1)
        return AIMessage(content=text.strip())

    def _generate(self, messages: List[BaseMessage], stop: Optional[List[str]] = None,
                  run_manager: Any = None, **kwargs: Any) -> ChatResult:
        text = ""
        with requests.post(self.url, json={"messages": self._to_colab(messages)},
                           stream=True, timeout=self.timeout) as r:
            r.raise_for_status()
            for chunk in r.iter_content(chunk_size=512, decode_unicode=True):
                if chunk:
                    text += chunk
        return ChatResult(generations=[ChatGeneration(message=self._parse(text))])