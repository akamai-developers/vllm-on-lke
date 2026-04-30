# Demo scripts

Four scripts that walk through the failure modes of GPU inference serving — and the recovery patterns that fix them.

| Script | What it teaches |
|---|---|
| `loadgen.py` | **Continuous batching** — why concurrent users don't pay linear latency cost |
| `kv_cache_pressure.py` | **KV cache exhaustion** — what happens when memory, not compute, becomes the bottleneck |
| `apply_bounded.sh` / `revert_bounded.sh` | **Bounded admission** — the recovery pattern that keeps both failure modes from cascading |
| `cold_start.sh` | **Pod death recovery** — how the PVC turns a 10-minute outage into a 60-second blip |

Each script's docstring has the gory details. This file is the index, the run order, and the talk narration.

---

## Setup (once per terminal)

```bash
# Python deps
pip install httpx

# Pull token + endpoint straight from the cluster (no copy-paste)
export VLLM_API_KEY=$(kubectl -n llm get secret vllm-secrets \
  -o jsonpath='{.data.VLLM_API_KEY}' | base64 -d)
export ENDPOINT=http://$(kubectl -n llm get svc vllm \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
```

Open Grafana with two dashboards visible: **vLLM** on top (TTFT, num_running, num_waiting, gpu_cache_usage_perc) and **NVIDIA DCGM** on bottom (GPU util, VRAM, power). The terminal tells one story, the dashboards tell another — they should agree.

`cold_start.sh` reads token + endpoint from the cluster itself, so it works without the env vars above.

---

## Demo 1 — Baseline: one request, instant response

**What you're showing:** the happy path. One user, one prompt, fast answer. This is the laptop-feeling experience — the bar everything that follows is measured against.

```bash
curl -s $ENDPOINT/v1/chat/completions \
  -H "Authorization: Bearer $VLLM_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen2.5-7B-Instruct","messages":[{"role":"user","content":"Hello!"}]}' | jq .
```

**What you should see:**
- Response in 1–2 seconds.
- TTFT around 100–300ms.
- GPU util barely registers in Grafana.

**What it means:** the model and the cluster work. Now we're going to scale it up and watch what breaks.

---

## Demo 2 — Continuous batching: 32 users for the price of ~3

**What you're showing:** vLLM doesn't run 32 requests sequentially or even in parallel — it *batches* them onto the same forward pass. Every step the GPU generates one token for *every* in-flight request at once. Reading the model weights from VRAM costs the same whether you're serving 1 request or 32, so batching multiplies throughput at near-zero cost in latency.

This is the single most important optimization in modern LLM serving. Without it, your $5K GPU serves ~34 tokens/sec. With it, ~1,000+.

```bash
python demo/loadgen.py --concurrency 32 --total 200
```

**What you should see:**
- `vllm:num_requests_running` climbs to ~32 in seconds (Grafana).
- DCGM `GPU-Util` pegs near 100%.
- Aggregate throughput rises to **~500–1000 tok/s** — far more than the 34 tok/s a single request gets.
- TTFT p50 stays sub-second; p95 starts to creep up.

What it looks like:

```
step 1:  [A B C D]  ← 4 requests, each generates token 1
step 2:  [A B C D]  ← all 4 generate token 2                                        
step 3:  [A   C D E]  ← B finished, E joined from the queue
step 4:  [A   C D E F]  ← F joined                                                  
step 5:  [A     D E F G]  ← C finished, G joined 
```

**The narration:**

> "If I served these requests one at a time, this run would take 30+ minutes. Continuous batching does it in under a minute. The GPU doesn't run faster — it runs more efficiently, because reading the model weights once amortizes across every request in the batch."

**Push past the saturation point** to find this card's real ceiling:

```bash
python demo/loadgen.py --concurrency 64 --total 200
python demo/loadgen.py --concurrency 128 --total 200
```

Throughput will rise, plateau, and eventually start hurting per-request latency. Where the plateau lands = your real-world serving ceiling for this model on this GPU. Update slide 9 with the measured number after rehearsal.

---

## Demo 3 — KV cache exhaustion: a different limit, same symptom

**What you're showing:** Demo 2 saturated *compute*. This demo saturates *memory*. Each in-flight request needs its own KV cache (the running attention state) — and that cache lives in the same VRAM pool as the model weights. When prompts are long and concurrency is high, the cache pool runs out, and vLLM has to either queue new requests or evict in-flight ones.

For your card and model:
- Total KV cache pool: **~38,800 tokens** (visible in `vllm:cache_config_info` → `num_gpu_blocks × block_size`).
- One 4,000-token request grabs ~10% of the pool.
- 32 concurrent × 4,000 tokens demands 128,000 tokens — **3× more than fits**.

```bash
python demo/kv_cache_pressure.py --concurrency 32 --prompt-tokens 4000
```

**What you should see:**
- `vllm:gpu_cache_usage_perc` ramps toward **1.0** (100% of pool used).
- `vllm:num_requests_waiting` climbs to ~20+. The engine is full.
- Per-request output prints live: first ~9 land in 2–17s, then TTFT climbs steadily through the 10s, 20s, 30s mark, and plateaus around the 50–60s range as the queue drains in waves.
- Final TTFT p50 ≈ p95. **Everyone is slow** — there's no "lucky" tail of fast requests, because everyone past request #9 is queued.

**The narration:**

> "Demo 2 was 'the engine is overloaded.' This is 'the engine is over-committed.' Different bottleneck — memory instead of compute — but the user-visible symptom is the same: TTFT degrades. And notice the distribution: this isn't a long tail of slow requests. The whole endpoint is slow. Every user sees a 50-second wait. In production, that's a P0 incident."

**If the queue doesn't form on the defaults** (smaller model, bigger GPU, or more cache headroom than expected), push harder:

```bash
python demo/kv_cache_pressure.py --concurrency 64 --prompt-tokens 6000
```

---

## Demo 3a — The fix: bounded admission

**What you're showing:** the recovery pattern that solves *both* failure modes — Demo 2's compute saturation and Demo 3's memory exhaustion. Same lever, two problems.

The fix has two parts:
- `--max-num-seqs=8` — admit at most 8 requests into the engine at once. The 9th, 10th, 11th waits at the door, not in the engine's internal queue.
- `--max-model-len=8192` — reject any single request longer than 8K tokens up front (instead of letting it monopolize the cache).

```bash
./demo/apply_bounded.sh
# wait for the Recreate rollout (~90s — old pod terminates, GPU + PVC release,
# new pod mounts PVC and reloads weights from cache)

python demo/kv_cache_pressure.py --concurrency 32 --prompt-tokens 4000   # same load
```

**What you should see, compared to the un-bounded run:**
- `vllm:gpu_cache_usage_perc` plateaus around 80–90% instead of pegging at 100%.
- `vllm:num_requests_waiting` reflects the *gateway* queue (controlled by us), not an unbounded internal one.
- TTFT for the 8 admitted requests stays around 2–4s — close to baseline.
- The other 24 requests wait at the admission boundary. Their wait time is *predictable* (8 admit → ~10s decode → next 8 admit), not the runaway pile-up from Demo 3.
- p95 still grows under heavy load, but **plateaus instead of climbing without bound**. The model never goes down.

**The narration:**

> "Same load. Same model. Same hardware. The only thing that changed is *where* requests queue. Without bounded admission, the engine accepts everything and degrades for everyone. With bounded admission, the engine accepts only what it can serve well — and what's beyond capacity queues outside the engine, where you can shed it, prioritize it, or rate-limit it. In production, you cap what you accept and queue at your gateway. Letting the in-process queue grow without bound is how you turn a slow request into a dead pod."

**Revert when done:**

```bash
./demo/revert_bounded.sh    # kubectl rollout undo — restores original args
```

---

## Demo 4 — Cold start: pod death and the PVC payoff

**What you're showing:** what happens when your single GPU pod dies — and why the architecture survives it. Without a PVC, every restart triggers a fresh 15GB Hugging Face download, which is 5–10 minutes of 502s. With the PVC, weights stay cached on a Linode block volume; the new pod just mounts and reads.

```bash
./demo/cold_start.sh
```

The script kills the running pod and prints timestamps for:
1. **Replacement scheduled** — Kubernetes detected the death.
2. **`/health` returns 200** — vLLM finished loading weights and is accepting requests.
3. **First inference completes** — end-to-end recovery time.

**What you should see:**
- Total time **30–90 seconds**.
- VRAM in DCGM drops to 0 during pod transition, then climbs back as weights reload.
- Audience reaction: "wait, that's it?"

**The narration:**

> "Without the PVC, that 'model load' line would be a 15-gigabyte Hugging Face download — 5 to 10 minutes of errors before the first request succeeds. The PVC plus a 10-minute startupProbe budget is what bridges that gap. HPA can't save you if every new pod cold-starts; the cold-start cost is the constraint behind every multi-replica decision in slide 18."

---

## When something goes wrong on stage

| Symptom | Likely cause | What to do |
|---|---|---|
| `loadgen.py` throughput stays flat with concurrency | GPU already saturated at low concurrency, or network is the bottleneck | Lower the model size, or accept it — the saturation point is part of the story. |
| `kv_cache_pressure.py` doesn't queue | More cache headroom than expected (smaller prompt, larger pool) | Raise `--concurrency 64 --prompt-tokens 6000`. |
| `apply_bounded.sh` rollout times out | New pod waiting for old pod to release GPU + PVC | Almost always self-resolves. `kubectl -n llm describe pod` to confirm. |
| `cold_start.sh` exceeds 5 minutes | PVC didn't reattach, or volume is empty (first run after redeploy) | Don't pretend — narrate it. "This is what cold-start cost looks like in the wild." |
| Grafana panels lag terminal output | Scrape interval is 15s | Set Grafana refresh to 5s; lower PodMonitor `interval: 5s` for the demo. |

---

## Order of operations for the talk

```bash
# 1. Baseline
curl ...                                                              # ~5s

# 2. Continuous batching
python demo/loadgen.py --concurrency 32 --total 200                   # ~45s
python demo/loadgen.py --concurrency 64 --total 200                   # ~25s

# 3. KV cache exhaustion (un-bounded — this is the failure)
python demo/kv_cache_pressure.py --concurrency 32 --prompt-tokens 4000  # ~2 min

# 3a. Recovery (bounded admission)
./demo/apply_bounded.sh                                                # ~90s rollout
python demo/kv_cache_pressure.py --concurrency 32 --prompt-tokens 4000  # ~90s, much cleaner
./demo/revert_bounded.sh                                               # ~90s rollout

# 4. Cold start
./demo/cold_start.sh                                                   # 30-90s
```

Total demo time: ~10 minutes if everything cooperates. Build a 2-minute buffer into the talk for rollout waits.
