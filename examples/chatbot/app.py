"""Streamlit chatbot: a Strands agent that runs against your LKE-hosted vLLM endpoint.

The agent speaks to vLLM through Strands' OpenAI-compatible model provider, and is
given five tools — `web_search` (live web data), `calculator`, `current_time`,
`list_lke_clusters` (the user's Linode clusters), and a custom
`convert_temperature`. Ask it something that needs a tool ("was there an NBA game
today?", "convert 100F to C", "what clusters do I have?") and you'll see the tool
call — name, arguments, and result — rendered inline before the final answer.

Prerequisites:
  - The vLLM Deployment must run with `--enable-auto-tool-choice
    --tool-call-parser hermes` (already set in manifests/vllm-deployment.yaml).
    Without those flags vLLM ignores tools and nothing gets called.

Run:
    pip install -r examples/chatbot/requirements.txt

    export VLLM_API_KEY=$(kubectl -n llm get secret vllm-secrets \
      -o jsonpath='{.data.VLLM_API_KEY}' | base64 -d)
    export ENDPOINT=http://$(kubectl -n llm get svc vllm \
      -o jsonpath='{.status.loadBalancer.ingress[0].ip}')

    streamlit run examples/chatbot/app.py
"""
import json
import os

import streamlit as st
from dotenv import load_dotenv

from agent_runtime import (
    DEFAULT_MODEL,
    TurnState,
    apply_event,
    build_agent,
    run_async,
)

load_dotenv()


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render_tool_call(tc: dict, expanded: bool) -> None:
    """Render one tool invocation: name, input args, and (if present) result."""
    icon = "⚠️" if tc.get("status") == "error" else "🔧"
    with st.expander(f"{icon} tool · `{tc['name']}`", expanded=expanded):
        st.caption("Arguments")
        st.code(json.dumps(tc.get("input", {}), indent=2, default=str), language="json")
        if tc.get("result") is not None:
            st.caption(f"Result ({tc.get('status') or 'success'})")
            st.code(tc["result"])


async def stream_turn(agent, prompt, tools_container, text_ph):
    """Stream one agent turn, rendering tool calls and text as they arrive.

    Returns (final_text, [tool_call, ...]) so the turn can be re-rendered from
    session history on the next Streamlit run.
    """
    state = TurnState()
    tool_phs: dict = {}   # toolUseId -> st.empty() placeholder

    # Always close the stream. If a turn is interrupted (render error, or the
    # user sends another message mid-stream), an abandoned async generator
    # leaves the Strands agent's "busy" flag set — and every later turn then
    # fails with "Agent is already processing a request." aclose() runs the
    # generator's cleanup and releases that lock.
    stream = agent.stream_async(prompt)
    try:
        async for event in stream:
            changed, text_changed = apply_event(state, event)
            for tid in changed:
                if tid not in tool_phs:
                    tool_phs[tid] = tools_container.empty()
                with tool_phs[tid].container():
                    render_tool_call(state.tool_calls[tid], expanded=True)
            if text_changed:
                text_ph.markdown(state.text + "▌")
    finally:
        await stream.aclose()

    text_ph.markdown(state.text or "_(no text response)_")
    return state.text, [state.tool_calls[t] for t in state.order]


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
st.set_page_config(page_title="vLLM on LKE · Strands chatbot", page_icon="🤖")
st.title("🤖 Strands agent on vLLM")
st.caption("A tool-using agent talking to your LKE-hosted vLLM endpoint.")

with st.sidebar:
    st.header("Connection")
    endpoint = st.text_input(
        "Endpoint (with scheme)",
        value=os.environ.get("ENDPOINT", ""),
        placeholder="http://<loadbalancer-ip>",
        help="The vLLM Service IP. Pre-filled from $ENDPOINT if set.",
    )
    api_key = st.text_input(
        "API key",
        value=os.environ.get("VLLM_API_KEY", ""),
        type="password",
        help="Bearer token from the vllm-secrets Secret. Pre-filled from $VLLM_API_KEY if set.",
    )
    model_id = st.text_input("Model", value=os.environ.get("VLLM_MODEL", DEFAULT_MODEL))
    temperature = st.slider("Temperature", 0.0, 1.5, 0.7, 0.1)
    max_tokens = int(st.number_input("Max tokens", 64, 4096, 1024, 64))

    st.divider()
    st.subheader("Tools")
    st.markdown(
        "- `web_search` — live web data (news, scores, prices)\n"
        "- `calculator` — exact arithmetic\n"
        "- `current_time` — current date/time\n"
        "- `list_lke_clusters` — your Linode LKE clusters\n"
        "- `convert_temperature` — custom tool"
    )
    if st.button("🗑️ New chat", use_container_width=True):
        for k in ("agent", "messages", "config_sig"):
            st.session_state.pop(k, None)
        st.rerun()

# Rebuild the agent only when config changes; otherwise reuse it so conversation
# history (held inside the Agent) survives Streamlit's top-to-bottom reruns.
config_sig = (endpoint, api_key, model_id, temperature, max_tokens)
if endpoint and st.session_state.get("config_sig") != config_sig:
    st.session_state.agent = build_agent(
        endpoint, api_key, model_id, temperature, max_tokens
    )
    st.session_state.config_sig = config_sig

st.session_state.setdefault("messages", [])

# Redraw the transcript on every rerun.
for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        for tc in m.get("tools", []):
            render_tool_call(tc, expanded=False)
        if m.get("text"):
            st.markdown(m["text"])

if not endpoint:
    st.info("Set the endpoint in the sidebar (or export `$ENDPOINT`) to start chatting.")
    st.stop()

if prompt := st.chat_input("Ask something that needs a tool…"):
    st.session_state.messages.append({"role": "user", "text": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        tools_container = st.container()
        text_ph = st.empty()
        try:
            text, tools = run_async(
                stream_turn(st.session_state.agent, prompt, tools_container, text_ph)
            )
        except Exception as e:  # surface connection / model errors in the UI
            text, tools = "", []
            # Recovery: if the agent is wedged in a "busy" state from an earlier
            # interrupted turn, rebuild a fresh one so the user isn't stuck.
            if "already processing" in str(e).lower():
                st.session_state.agent = build_agent(
                    endpoint, api_key, model_id, temperature, max_tokens
                )
                text_ph.warning("Agent was reset after an interrupted request — send that again.")
            else:
                text_ph.error(f"Request failed: {e}")

    st.session_state.messages.append(
        {"role": "assistant", "text": text, "tools": tools}
    )
