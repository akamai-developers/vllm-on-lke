"""Call your LKE-hosted vLLM endpoint with the OpenAI Python SDK.

The endpoint is OpenAI-compatible — anything that speaks OpenAI works against it,
including LangChain, LlamaIndex, and most agent frameworks (point them at
ENDPOINT/v1 with the bearer token as the API key).

Run:
    pip install openai

    # Pull the bearer token and endpoint straight from the cluster
    export VLLM_API_KEY=$(kubectl -n llm get secret vllm-secrets \\
      -o jsonpath='{.data.VLLM_API_KEY}' | base64 -d)
    export ENDPOINT=http://$(kubectl -n llm get svc vllm \\
      -o jsonpath='{.status.loadBalancer.ingress[0].ip}')

    python examples/openai-client.py
"""

import os

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# ENDPOINT includes the scheme, e.g. http://<loadbalancer-ip> (matches the README
# and demo scripts). The OpenAI SDK wants the full /v1 base URL.
endpoint = os.environ["ENDPOINT"].rstrip("/")
api_key = os.environ["VLLM_API_KEY"]

client = OpenAI(
    base_url=f"{endpoint}/v1",
    api_key=api_key,
)

response = client.chat.completions.create(
    model="Qwen/Qwen2.5-7B-Instruct",
    messages=[
        {"role": "system", "content": "You are a concise assistant."},
        {
            "role": "user",
            "content": "Why is GPU memory the bottleneck for LLM inference?",
        },
    ],
)

print(response.choices[0].message.content)
