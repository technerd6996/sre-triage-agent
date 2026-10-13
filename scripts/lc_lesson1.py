"""LangChain lesson 1 - one agent, two tools, one approval gate.
Run:  python scripts/lc_lesson1.py
Uses fake tools, so it is safe. Needs COLAB_API_URL in .env and app/llm/colab_chat.py
"""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from langchain.tools import tool
from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

load_dotenv()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # project root
from app.llm.colab_chat import ColabChat


# --- CONCEPT 2: a tool is a plain function. The docstring is what the LLM reads. ---
@tool
def get_disk_usage(host: str) -> str:
    """Return the disk usage percentage of a server."""
    return f"{host}: / is 97% full"


@tool
def clean_temp_files(host: str) -> str:
    """Delete temporary files on a server to free disk space. HIGH RISK."""
    return f"{host}: freed 12 GB in /tmp"


# --- CONCEPT 1: the model. Our wrapper around the existing Colab /generate endpoint. ---
llm = ColabChat(url=os.getenv("COLAB_API_URL"))

# --- CONCEPT 3 + 4 + 5: the agent loop, middleware (approval gate), checkpointer ---
agent = create_agent(
    model=llm,
    tools=[get_disk_usage, clean_temp_files],
    system_prompt="You are an SRE L1 agent. Always diagnose first, then fix.",
    middleware=[HumanInTheLoopMiddleware(interrupt_on={"clean_temp_files": True})],
    checkpointer=InMemorySaver(),  # remembers the paused state
)

config = {"configurable": {"thread_id": "demo-1"}}  # one thread_id = one ticket
result = agent.invoke(
    {"messages": [{"role": "user", "content": "Disk alert on linux-01. Please fix it."}]},
    config,
)

if "__interrupt__" in result:  # agent paused before the risky tool
    print("\nPAUSED FOR APPROVAL:", result["__interrupt__"])
    if input("Approve? (y/n): ").strip().lower() == "y":
        decision = {"type": "approve"}
    else:
        decision = {"type": "reject", "message": "Not approved by the engineer."}
    result = agent.invoke(Command(resume={"decisions": [decision]}), config)

print("\nFINAL ANSWER:", result["messages"][-1].content)
