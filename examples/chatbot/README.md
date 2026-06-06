# Strands agent chatbot (Streamlit) on vLLM

A tool-using chat agent that talks to your LKE-hosted vLLM endpoint and **shows
its tool calls inline** — the tool name, the arguments the model chose, and the
result it got back — before the final answer.

Built with [Strands Agents](https://strandsagents.com/) pointed at vLLM through
Strands' OpenAI-compatible model provider. Because vLLM speaks the OpenAI API,
the agent doesn't know (or care) that it's running against a self-hosted 7B model
on a single GPU instead of a hosted frontier model.

## What the agent can do

Five tools are wired in (`agent_runtime.py`):

| Tool | Source | Use it for |
|---|---|---|
| `web_search` | custom (`agent_tools.py`) | live web data via DuckDuckGo (no API key) — "was there an NBA Finals game today?", "current price of bitcoin" |
| `calculator` | `strands-agents-tools` | exact arithmetic the model shouldn't eyeball |
| `current_time` | `strands-agents-tools` | the current date/time |
| `list_lke_clusters` | custom (`agent_tools.py`) | the user's own Linode LKE clusters — "what clusters do I have running?" (read-only Linode API call) |
| `convert_temperature` | custom (`agent_tools.py`) | example of a hand-written `@tool` |

Add your own: drop an `@tool`-decorated function into `agent_tools.py`'s
`CUSTOM_TOOLS` list and it shows up automatically.

> **`list_lke_clusters` uses your `LINODE_TOKEN`** (already in `.env`) and is
> deliberately scoped to a single read-only `GET`. Don't hand a model a generic
> HTTP tool *plus* a cloud token — that lets it construct arbitrary API calls,
> including destructive ones. Ship narrow, purpose-built tools instead.

## Prerequisite: tool calling must be enabled on the endpoint

The agent calls tools via OpenAI-style function calling, which vLLM only emits
when started with `--enable-auto-tool-choice --tool-call-parser hermes`. Those
flags are **already set** in `manifests/vllm-deployment.yaml`. If you deployed an
older revision, re-apply the deployment (or add the flags and let it roll) or the
agent will get plain-text answers and no tool will ever fire.

## Run it (locally, against your endpoint)

The easiest path is `./deploy.sh chatbot` from the repo root — it creates a
`.venv`, installs the deps, and launches Streamlit, reading `ENDPOINT` +
`VLLM_API_KEY` from `.env`. To do it by hand:

```bash
# from the repo root — use a venv (Debian/Ubuntu block a system-wide pip; PEP 668)
python3 -m venv .venv
.venv/bin/pip install -r examples/chatbot/requirements.txt

# Pull the endpoint + bearer token straight from the cluster (no copy-paste).
# ENDPOINT must include the scheme (http://).
export VLLM_API_KEY=$(kubectl -n llm get secret vllm-secrets \
  -o jsonpath='{.data.VLLM_API_KEY}' | base64 -d)
export ENDPOINT=http://$(kubectl -n llm get svc vllm \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}')

.venv/bin/streamlit run examples/chatbot/app.py
```

Streamlit opens at http://localhost:8501. The endpoint and key are pre-filled
from those env vars; you can also paste them into the sidebar.

**Questions to try (one per tool):**

- *"Was there an NBA Finals game today? Search the web."* → `web_search`
- *"What Kubernetes clusters do I have running?"* → `list_lke_clusters`
- *"What's 3847 × 2913?"* → `calculator`
- *"Convert 100°F to Celsius."* → `convert_temperature`
- *"What time is it right now in UTC?"* → `current_time`

Each call renders as a `🔧 tool` panel showing the arguments and result, so you
can see the agent reason → call a tool → use the result. If the model answers
from memory instead of calling a tool, nudge it ("search the web", "use a tool") —
a 7B model picks tools less reliably than a frontier model.

## How it's wired

```
Streamlit UI (app.py)
   │  stream_async events  →  apply_event()/TurnState  →  render tool panels + text
   ▼
Strands Agent (agent_runtime.py)
   │  OpenAIModel(base_url=$ENDPOINT/v1, api_key=$VLLM_API_KEY)
   ▼
vLLM /v1/chat/completions   (--enable-auto-tool-choice --tool-call-parser hermes)
```

- `agent_runtime.py` — model wiring, tool list, system prompt, and the
  stream-event parser (`apply_event` / `TurnState`). UI-independent, so it's
  unit-testable without a browser.
- `app.py` — Streamlit chat UI: renders the transcript, streams each turn, and
  draws a panel per tool call.
- `agent_tools.py` — custom `@tool` functions.

Conversation history lives inside the Strands `Agent`, which is cached in
Streamlit session state and reused across reruns. Changing any connection
setting (or hitting **New chat**) rebuilds the agent and clears the thread.

> `BYPASS_TOOL_CONSENT=true` is set at import time so any community tool that
> would otherwise prompt for confirmation runs headless — there's no TTY under
> Streamlit. If you add filesystem/shell tools, reconsider this.

## Notes

- **Smaller models call tools less reliably than frontier models.** Qwen2.5-7B is
  decent but will occasionally answer from memory instead of calling a tool.
  Phrasing the request to clearly need live/exact data helps; so does the system
  prompt in `agent_runtime.py`, which you can tune.
- **Running it in-cluster** instead of locally means containerizing this folder
  and pointing `ENDPOINT` at the in-cluster Service DNS
  (`http://vllm.llm.svc.cluster.local`) — not covered here; the local flow above
  is the intended path.
