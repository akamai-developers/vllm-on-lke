"""Stream responses from your LKE-hosted vLLM endpoint with the OpenAI Python SDK.

Streaming returns tokens as they're generated instead of waiting for the full
response. This is what real chat UIs do — first word in ~250ms feels instant;
waiting 30s for the full answer feels broken. It's also the only way to measure
TTFT (time to first token) accurately.

Run:
    pip install openai

    # Pull the bearer token and endpoint straight from the cluster
    export VLLM_API_KEY=$(kubectl -n llm get secret vllm-secrets \\
      -o jsonpath='{.data.VLLM_API_KEY}' | base64 -d)
    export ENDPOINT=http://$(kubectl -n llm get svc vllm \\
      -o jsonpath='{.status.loadBalancer.ingress[0].ip}')

    python examples/openai_streaming_client.py
"""

import os
import time

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

base_url=f"{os.environ['ENDPOINT']}/v1"
api_key=os.environ["VLLM_API_KEY"]


client = OpenAI(
    base_url="http://" + base_url,
    api_key=api_key,
)

started = time.perf_counter()
ttft = None
tokens = 0

stream = client.chat.completions.create(
    model="Qwen/Qwen2.5-7B-Instruct",
    messages=[
        {"role": "system", "content": "You are a concise assistant."},
        {
            "role": "user",
            "content": "Explain continuous batching in three sentences.",
        },
    ],
    stream=True,
)

for chunk in stream:
    delta = chunk.choices[0].delta.content or ""
    if delta:
        if ttft is None:
            ttft = time.perf_counter() - started
        tokens += 1
        print(delta, end="", flush=True)

elapsed = time.perf_counter() - started
print(
    f"\n\n--- TTFT: {ttft:.2f}s | tokens: {tokens} | "
    f"elapsed: {elapsed:.2f}s | tok/s: {tokens / elapsed:.1f} ---"
)
