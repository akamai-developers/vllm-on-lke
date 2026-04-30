#!/usr/bin/env bash
# Apply the "bounded" vLLM config: cap concurrent batched requests and context length.
#
# This is the recovery path for KV cache exhaustion. Instead of letting the
# queue grow without bound when admission outpaces capacity, we tell vLLM to:
#
#   --max-num-seqs=8       ← batch at most 8 sequences concurrently
#   --max-model-len=8192   ← reject prompts longer than 8K tokens up front
#
# Effect under the same load that previously caused unbounded TTFT growth:
# TTFT p50 stays near baseline (the 8 in-flight requests are unaffected);
# TTFT p95 grows but plateaus instead of climbing without bound; the model
# never goes down. That's what bounded admission buys you.
#
# Triggers a Recreate rollout — single GPU + RWO PVC means a brief outage
# while the old pod releases the GPU and the new one mounts the PVC.
#
# Run:
#   ./demo/apply_bounded.sh
#   python demo/kv_cache_pressure.py    # same load — watch bounded behavior
#   ./demo/revert_bounded.sh            # back to naive

set -euo pipefail

NAMESPACE=${NAMESPACE:-llm}

if kubectl -n "$NAMESPACE" get deploy vllm -o jsonpath='{.spec.template.spec.containers[0].args}' \
    | grep -q "max-num-seqs"; then
  echo "Bounded config already applied. Run revert_bounded.sh first."
  exit 0
fi

kubectl patch deploy vllm -n "$NAMESPACE" --type=json -p '[
  {"op": "add", "path": "/spec/template/spec/containers/0/args/-", "value": "--max-num-seqs=8"},
  {"op": "add", "path": "/spec/template/spec/containers/0/args/-", "value": "--max-model-len=8192"}
]'

echo "Patched. Waiting for Recreate rollout (~1-2 min: old pod terminates, GPU+PVC released, new pod loads from PVC)..."
kubectl -n "$NAMESPACE" rollout status deploy/vllm --timeout=10m
echo "Bounded config live. Re-run kv_cache_pressure.py to compare."
