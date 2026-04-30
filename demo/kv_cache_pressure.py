"""Push concurrent long-context requests until the KV cache fills.

Demonstrates how vLLM behaves when the GPU's KV cache is the bottleneck:

  - vllm:gpu_cache_usage_perc      → climbs toward 1.0
  - vllm:num_requests_running      → plateaus (can't admit more onto the GPU)
  - vllm:num_requests_waiting      → rises (admission queue grows)
  - vllm:num_preemptions_total     → may increment (running requests evicted)
  - TTFT for queued requests       → balloons; they're waiting, not computing

The model stays up — vLLM degrades by queueing rather than OOMing the GPU.
That's the contrast with naive serving: under pressure you get tail latency,
not crashes.

Each request gets a unique prefix to defeat any prefix-cache deduplication,
so every request really does need its own KV cache slots.

Run:
    pip install httpx
    export VLLM_API_KEY=...
    export ENDPOINT=...

    python demo/kv_cache_pressure.py                                  # defaults
    python demo/kv_cache_pressure.py --concurrency 64 --prompt-tokens 6000

Sizing notes for Qwen2.5-7B on a 24GB GPU: roughly 7-8 GB of KV cache
headroom after weights, ~56 KB/token → ~140k tokens of cache space.
Defaults below (32 × 4000 ≈ 128k input tokens) sit just under that, so
you'll see the queue start to grow. Crank --concurrency or --prompt-tokens
to push past it.
"""

import argparse
import asyncio
import json
import os
import time

import httpx


# Plausible-looking English text we tile to hit the target token count.
# Loose: ~0.75 tokens per English word, so we pad to (target * 1.4) words.
FILLER = (
    "GPU memory bandwidth is the bottleneck for transformer inference because "
    "every decode step reads the entire weights matrix and the per-request KV "
    "cache. Continuous batching helps amortize the weight read across requests, "
    "but only if there is KV cache headroom to admit them. When the cache fills, "
    "the scheduler stops admitting new requests. Some serving stacks evict "
    "in-flight requests and recompute them later; vLLM calls this preemption. "
)


def build_prompt(target_tokens: int, idx: int) -> str:
    words = []
    while len(words) < target_tokens * 1.4:
        words.extend(FILLER.split())
    body = " ".join(words[: int(target_tokens * 1.4)])
    return f"Request #{idx}. {body}\n\nSummarize the above in one sentence."


async def one_request(client, endpoint, api_key, model, prompt, idx):
    started = time.perf_counter()
    ttft = None
    try:
        async with client.stream(
            "POST",
            f"{endpoint}/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 64,
                "stream": True,
            },
            timeout=600.0,
        ) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.startswith("data: ") or "[DONE]" in line:
                    continue
                try:
                    chunk = json.loads(line[6:])
                    delta = chunk["choices"][0].get("delta", {})
                    if delta.get("content") and ttft is None:
                        ttft = time.perf_counter() - started
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
        return {"idx": idx, "ok": True, "ttft": ttft, "elapsed": time.perf_counter() - started}
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
    parser.add_argument("--prompt-tokens", type=int, default=4000)
    parser.add_argument("--total", type=int, default=64)
    parser.add_argument("--model", default="Qwen/Qwen2.5-7B-Instruct")
    args = parser.parse_args()

    endpoint = os.environ["ENDPOINT"].rstrip("/")
    api_key = os.environ["VLLM_API_KEY"]

    print(f"Concurrency:   {args.concurrency}")
    print(f"Prompt size:   ~{args.prompt_tokens} tokens each")
    print(f"Total:         {args.total} requests")
    print()
    print("Watch in Grafana:")
    print("  vllm:gpu_cache_usage_perc")
    print("  vllm:num_requests_waiting")
    print("  vllm:num_preemptions_total")
    print()

    sem = asyncio.Semaphore(args.concurrency)

    async with httpx.AsyncClient() as client:
        async def runner(i):
            async with sem:
                return await one_request(
                    client, endpoint, api_key, args.model, build_prompt(args.prompt_tokens, i), i
                )

        wall_start = time.perf_counter()
        results = []
        for coro in asyncio.as_completed([runner(i) for i in range(args.total)]):
            r = await coro
            results.append(r)
            n = len(results)
            if r["ok"]:
                ttft = f"{r['ttft']:.2f}s" if r["ttft"] is not None else "n/a"
                print(f"[{n:>3}/{args.total}] ok    ttft={ttft:>7}  elapsed={r['elapsed']:.2f}s")
            else:
                print(f"[{n:>3}/{args.total}] FAIL  elapsed={r['elapsed']:.2f}s  err={r['error'][:80]}")
        wall = time.perf_counter() - wall_start

    ok = [r for r in results if r["ok"]]
    if not ok:
        return

    ttfts = sorted(r["ttft"] for r in ok if r["ttft"] is not None)
    elapsed = sorted(r["elapsed"] for r in ok)

    def pct(xs, p):
        return xs[min(int(p * len(xs)), len(xs) - 1)]

    print(f"\nWall: {wall:.1f}s   ok={len(ok)}/{args.total}")
    if ttfts:
        print(
            f"TTFT  p50/p95/max: {pct(ttfts, 0.50):.2f}s / {pct(ttfts, 0.95):.2f}s / {ttfts[-1]:.2f}s"
        )
    print(
        f"Total p50/p95/max: {pct(elapsed, 0.50):.2f}s / {pct(elapsed, 0.95):.2f}s / {elapsed[-1]:.2f}s"
    )
    print("\nIf p95 TTFT >> p50 TTFT, the queue formed — that's the failure mode you're showing.")


if __name__ == "__main__":
    asyncio.run(main())
