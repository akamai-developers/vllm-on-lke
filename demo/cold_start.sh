#!/usr/bin/env bash
# Time recovery from a vLLM pod death.
#
# What you're watching:
#   - Pod scheduled    → a few seconds (k8s reschedules onto the GPU node)
#   - GPU/PVC released → depends on graceful shutdown of the old pod
#   - /health 200      → 30-90s warm (PVC has weights),
#                        5-10 min cold (fresh ~15GB Hugging Face download)
#   - First inference  → +1-2s after /health (KV cache cold for the first prompt)
#
# The PVC + 10-minute startupProbe budget is what bridges cold starts.
# Without the PVC, every restart re-downloads the model.
#
# Run:
#   ./demo/cold_start.sh

set -euo pipefail

NAMESPACE=${NAMESPACE:-llm}

if ! command -v kubectl >/dev/null; then
  echo "kubectl not found"; exit 1
fi

API_KEY=$(kubectl -n "$NAMESPACE" get secret vllm-secrets -o jsonpath='{.data.VLLM_API_KEY}' | base64 -d)
ENDPOINT=http://$(kubectl -n "$NAMESPACE" get svc vllm -o jsonpath='{.status.loadBalancer.ingress[0].ip}')

if [ -z "$API_KEY" ] || [ "$ENDPOINT" = "http://" ]; then
  echo "Couldn't read vllm-secrets or vllm Service IP from namespace $NAMESPACE"
  exit 1
fi

OLD_POD=$(kubectl -n "$NAMESPACE" get pod -l app=vllm -o jsonpath='{.items[0].metadata.name}')
if [ -z "$OLD_POD" ]; then
  echo "No vllm pod found in namespace $NAMESPACE"
  exit 1
fi

T0=$(date +%s)
echo "[ +0s] killing pod $OLD_POD"
kubectl -n "$NAMESPACE" delete pod "$OLD_POD" --wait=false >/dev/null

NEW_POD=""
while [ -z "$NEW_POD" ]; do
  NEW_POD=$(kubectl -n "$NAMESPACE" get pod -l app=vllm -o jsonpath='{.items[*].metadata.name}' 2>/dev/null \
    | tr ' ' '\n' | grep -v "^$OLD_POD\$" | head -1 || true)
  sleep 1
done
echo "[+$(($(date +%s) - T0))s] replacement pod scheduled: $NEW_POD"

while ! curl -fsS -m 2 "$ENDPOINT/health" >/dev/null 2>&1; do
  sleep 2
done
echo "[+$(($(date +%s) - T0))s] /health 200 (model loaded, server accepting)"

curl -fsS -m 60 "$ENDPOINT/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen2.5-7B-Instruct","messages":[{"role":"user","content":"hi"}],"max_tokens":4}' \
  >/dev/null
TOTAL=$(($(date +%s) - T0))
echo "[+${TOTAL}s] first inference returned"

echo
echo "Total recovery: ${TOTAL}s"
if   [ "$TOTAL" -lt 120 ]; then echo "→ Weights came from the PVC. This is the warm-restart path."
elif [ "$TOTAL" -lt 600 ]; then echo "→ Slower than expected for a warm restart. Check 'kubectl -n $NAMESPACE describe pod $NEW_POD' (image pull? GPU rebind?)"
else                              echo "→ Looks like a cold start (fresh model download). Was the PVC empty or unbound?"
fi
