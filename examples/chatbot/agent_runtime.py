"""Agent wiring and stream parsing — the UI-independent core of the chatbot.

Kept separate from app.py so the agent loop and event handling can be tested
without a running Streamlit server (see the project's verification notes). app.py
imports from here and only owns the Streamlit rendering.
"""
import asyncio
import json
import os

# Must precede the strands_tools import: lets any community tool that would
# otherwise prompt for confirmation run headless (no TTY under Streamlit).
os.environ.setdefault("BYPASS_TOOL_CONSENT", "true")

from strands import Agent
from strands.models.openai import OpenAIModel
from strands_tools import calculator, current_time

from agent_tools import CUSTOM_TOOLS

DEFAULT_MODEL = "Qwen/Qwen2.5-7B-Instruct"

# calculator → exact math, current_time → now, plus the hand-written tools in
# agent_tools.py (web_search, list_lke_clusters, convert_temperature).
TOOLS = [calculator, current_time, *CUSTOM_TOOLS]

SYSTEM_PROMPT = (
    "You are a helpful assistant running on a self-hosted vLLM endpoint on Akamai "
    "Cloud. Tools available: web_search (search the web for current or real-time "
    "information — news, scores, prices, anything 'today' or 'latest'), calculator "
    "(exact arithmetic), current_time (the current date/time), list_lke_clusters "
    "(list the user's Linode Kubernetes clusters), and convert_temperature. Use a "
    "tool whenever it gives a more accurate or up-to-date answer than reasoning "
    "alone — especially for math, current events, live data, and the time. If a "
    "question needs information you don't have, call web_search instead of "
    "guessing. After a tool returns, answer concisely in plain language."
)


def build_agent(endpoint: str, api_key: str, model_id: str = DEFAULT_MODEL,
                temperature: float = 0.7, max_tokens: int = 1024) -> Agent:
    """Create a Strands Agent backed by the vLLM OpenAI-compatible endpoint."""
    model = OpenAIModel(
        client_args={
            "api_key": api_key or "not-needed",
            "base_url": f"{endpoint.rstrip('/')}/v1",
        },
        model_id=model_id,
        params={"max_tokens": max_tokens, "temperature": temperature},
    )
    # callback_handler=None: events are consumed via stream_async so the caller
    # can render tool calls instead of printing them to stdout.
    return Agent(
        model=model,
        tools=TOOLS,
        system_prompt=SYSTEM_PROMPT,
        callback_handler=None,
    )


def run_async(coro):
    """Run a coroutine to completion on a fresh event loop (Streamlit is sync)."""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        # Drain httpx's streaming async generators before closing, otherwise the
        # loop is torn down mid-cleanup ("Task was destroyed but it is pending").
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            asyncio.set_event_loop(None)
            loop.close()


def tool_result_text(tool_result: dict) -> str:
    """Flatten a Strands toolResult content list into displayable text."""
    parts = []
    for item in tool_result.get("content", []) or []:
        if isinstance(item, dict):
            if "text" in item:
                parts.append(item["text"])
            elif "json" in item:
                parts.append(json.dumps(item["json"], indent=2, default=str))
            else:
                parts.append(json.dumps(item, default=str))
        else:
            parts.append(str(item))
    return "\n".join(parts).strip()


class TurnState:
    """Accumulates streamed text and tool calls for a single agent turn."""

    def __init__(self):
        self.text = ""
        self.tool_calls: dict = {}   # toolUseId -> {name, input, result, status}
        self.order: list = []        # toolUseIds in first-seen order


def apply_event(state: TurnState, event: dict):
    """Fold one stream_async event into `state`.

    Returns (changed_tool_ids, text_changed) so a UI can update only what moved.

    Event shapes (verified against vLLM's tool-calling output):
      - {"data": "..."}                              streamed assistant text
      - {"message": {"content": [{"toolUse": {...}}]}}    a tool call (full input)
      - {"message": {"content": [{"toolResult": {...}}]}} a tool's return value
    """
    changed = []
    text_changed = False

    msg = event.get("message")
    if isinstance(msg, dict):
        for item in msg.get("content") or []:
            if not isinstance(item, dict):
                continue
            if "toolUse" in item:
                tu = item["toolUse"]
                tid = tu.get("toolUseId")
                if tid:
                    if tid not in state.tool_calls:
                        state.order.append(tid)
                    state.tool_calls[tid] = {
                        "name": tu.get("name", "tool"),
                        "input": tu.get("input", {}),
                        "result": None,
                        "status": None,
                    }
                    changed.append(tid)
            elif "toolResult" in item:
                tr = item["toolResult"]
                tid = tr.get("toolUseId")
                if tid in state.tool_calls:
                    state.tool_calls[tid]["result"] = tool_result_text(tr)
                    state.tool_calls[tid]["status"] = tr.get("status", "success")
                    changed.append(tid)

    if "data" in event:
        state.text += event["data"]
        text_changed = True

    return changed, text_changed
