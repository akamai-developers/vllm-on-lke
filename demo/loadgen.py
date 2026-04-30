"""Fire N concurrent OpenAI-compatible requests at the vLLM endpoint.

Demonstrates continuous batching: as concurrency rises, vLLM merges requests
onto the same forward pass instead of serving them one at a time. Watch in
Grafana while this runs:

  - vllm:num_requests_running     ← rises to N quickly
  - vllm:num_requests_waiting     ← stays near 0 until KV cache pressure
  - vllm:time_to_first_token_seconds
  - DCGM dashboard GPU util       ← pegs near 100% under sustained load

Throughput should rise sublinearly with concurrency until the GPU is saturated,
then plateau. That plateau is the model + GPU's real serving ceiling.

Run:
    pip install httpx
    export VLLM_API_KEY=$(kubectl -n llm get secret vllm-secrets \\
        -o jsonpath='{.data.VLLM_API_KEY}' | base64 -d)
    export ENDPOINT=http://$(kubectl -n llm get svc vllm \\
        -o jsonpath='{.status.loadBalancer.ingress[0].ip}')

    python demo/loadgen.py                                # 32 concurrent, 200 total
    python demo/loadgen.py --concurrency 64 --total 500   # crank it
"""

import argparse
import asyncio
import json
import os
import statistics
import time

import httpx


PROMPTS = [
    "Explain GPU memory bandwidth in two sentences.",
    "What is continuous batching in LLM inference?",
    "List three reasons KV cache fills faster than weights.",
    "Why does TTFT differ from inter-token latency?",
    "Describe the difference between prefill and decode.",
    "How does PagedAttention reduce memory fragmentation?",
    "What does it mean for an endpoint to be OpenAI-compatible?",
    "Why is GPU utilization a misleading metric for inference?",
]


async def one_request(client, endpoint, api_key, model, prompt, idx):
    started = time.perf_counter()
    ttft = None
    tokens = 0
    try:
        async with client.stream(
            "POST",
            f"{endpoint}/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 128,
                "stream": True,
            },
            timeout=180.0,
        ) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.startswith("data: ") or "[DONE]" in line:
                    continue
                try:
                    chunk = json.loads(line[6:])
                    delta = chunk["choices"][0].get("delta", {})
                    if delta.get("content"):
                        if ttft is None:
                            ttft = time.perf_counter() - started
                        tokens += 1
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
        return {
            "idx": idx,
            "ok": True,
            "ttft": ttft,
            "elapsed": time.perf_counter() - started,
            "tokens": tokens,
        }
    except Exception as e:
        return {
            "idx": idx,
            "ok": False,
            "elapsed": time.perf_counter() - started,
            "error": str(e),
        }


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--total", type=int, default=200)
    parser.add_argument("--model", default="Qwen/Qwen2.5-7B-Instruct")
    args = parser.parse_args()

    endpoint = os.environ["ENDPOINT"].rstrip("/")
    api_key = os.environ["VLLM_API_KEY"]

    print(f"Concurrency: {args.concurrency}   Total: {args.total}   Model: {args.model}")
    print(f"Endpoint:    {endpoint}\n")

    sem = asyncio.Semaphore(args.concurrency)

    async with httpx.AsyncClient() as client:
        async def runner(i):
            async with sem:
                return await one_request(
                    client, endpoint, api_key, args.model, PROMPTS[i % len(PROMPTS)], i
                )

        wall_start = time.perf_counter()
        results = await asyncio.gather(*[runner(i) for i in range(args.total)])
        wall = time.perf_counter() - wall_start

    ok = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]

    if not ok:
        print("All requests failed.")
        if failed:
            print(f"First error: {failed[0]['error']}")
        return

    ttfts = sorted(r["ttft"] for r in ok if r["ttft"] is not None)
    elapsed = sorted(r["elapsed"] for r in ok)
    completion_tokens = sum(r["tokens"] for r in ok)

    def pct(xs, p):
        return xs[min(int(p * len(xs)), len(xs) - 1)]

    print(f"Wall clock:        {wall:.2f}s")
    print(f"Requests ok/fail:  {len(ok)} / {len(failed)}")
    print(f"Throughput:        {len(ok) / wall:.2f} req/s")
    print(f"Aggregate tok/s:   {completion_tokens / wall:.1f}")
    if ttfts:
        print(
            f"TTFT  p50/p95/max: {pct(ttfts, 0.50):.2f}s / {pct(ttfts, 0.95):.2f}s / {ttfts[-1]:.2f}s"
        )
    print(
        f"Total p50/p95/max: {pct(elapsed, 0.50):.2f}s / {pct(elapsed, 0.95):.2f}s / {elapsed[-1]:.2f}s"
    )
    if failed:
        print(f"\nFirst failure: {failed[0]['error'][:200]}")


if __name__ == "__main__":
    asyncio.run(main())
