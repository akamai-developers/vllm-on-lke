#!/usr/bin/env bash
# Revert to the unbounded vLLM config (the previous deployment revision).
#
# Uses `kubectl rollout undo` so we don't need to know what the original args
# were — kubernetes already kept the previous ReplicaSet template.
#
# Run:
#   ./demo/revert_bounded.sh

set -euo pipefail

NAMESPACE=${NAMESPACE:-llm}

if ! kubectl -n "$NAMESPACE" get deploy vllm -o jsonpath='{.spec.template.spec.containers[0].args}' \
    | grep -q "max-num-seqs"; then
  echo "Bounded config not currently applied. Nothing to revert."
  exit 0
fi

kubectl -n "$NAMESPACE" rollout undo deploy/vllm

echo "Reverted. Waiting for Recreate rollout..."
kubectl -n "$NAMESPACE" rollout status deploy/vllm --timeout=10m
echo "Back to naive config."
