#!/usr/bin/env bash
#
# One-command lifecycle for the vLLM-on-LKE quickstart.
#
#   ./deploy.sh            # stand the whole thing up (alias: ./deploy.sh deploy)
#   ./deploy.sh env        # re-pull ENDPOINT + VLLM_API_KEY into .env from a live cluster
#   ./deploy.sh chatbot    # install deps and run the Streamlit chatbot locally
#   ./deploy.sh destroy    # tear it all down in the correct order
#
# Config comes from a gitignored `.env` in the repo root. You fill the INPUTS;
# this script writes the OUTPUTS. Copy `.env.examples` to `.env` and set
# LINODE_TOKEN (and HUGGING_FACE_HUB_TOKEN if you switch to a gated model).
#
# The script is idempotent: re-running reuses the existing vllm-secrets token
# instead of rotating it, so it won't break a chatbot you already have open.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

export KUBECONFIG="$ROOT/kubeconfig"

# ---- pretty output -------------------------------------------------------
log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mxxx\033[0m %s\n' "$*" >&2; exit 1; }

# ---- load .env (inputs) --------------------------------------------------
if [ -f "$ROOT/.env" ]; then
  set -a; source "$ROOT/.env"; set +a
fi

# ---- helpers -------------------------------------------------------------

# Upsert KEY="VALUE" in .env without disturbing the other lines.
set_env_var() {
  local key="$1" val="$2" file="$ROOT/.env"
  touch "$file"
  if grep -q "^${key}=" "$file"; then
    grep -v "^${key}=" "$file" > "$file.tmp" && mv "$file.tmp" "$file"
  fi
  printf '%s="%s"\n' "$key" "$val" >> "$file"
}

require() { command -v "$1" >/dev/null 2>&1 || die "missing required tool: $1"; }

# The GPU plan, read from tfvars so node labeling matches what Terraform built.
gpu_node_type() {
  local t
  t="$(grep -E '^\s*gpu_node_type' "$ROOT/terraform/terraform.tfvars" 2>/dev/null \
        | sed 's/.*=\s*"\(.*\)".*/\1/' || true)"
  printf '%s' "${t:-g2-gpu-rtx4000a1-m}"
}

# The pool=gpu label sometimes fails to propagate from Linode (see CLAUDE.md).
# If nothing carries it, label the nodes of the configured GPU plan ourselves.
ensure_gpu_label() {
  if [ -n "$(kubectl get nodes -l pool=gpu -o name 2>/dev/null)" ]; then return; fi
  local type; type="$(gpu_node_type)"
  warn "No node labeled pool=gpu; labeling nodes of type $type"
  local n
  for n in $(kubectl get nodes -l "node.kubernetes.io/instance-type=$type" -o name); do
    kubectl label "$n" pool=gpu --overwrite
  done
}

# Block until the LoadBalancer Service gets an external IP from the Linode CCM.
wait_for_lb_ip() {
  local ip
  for _ in $(seq 1 60); do
    ip="$(kubectl -n llm get svc vllm \
            -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)"
    [ -n "$ip" ] && { printf '%s' "$ip"; return 0; }
    sleep 5
  done
  return 1
}

# Pull ENDPOINT + VLLM_API_KEY from the live cluster into .env.
write_env_from_cluster() {
  local ip key
  ip="$(wait_for_lb_ip)" || die "LoadBalancer never got an external IP — check 'kubectl -n llm get svc vllm'"
  key="$(kubectl -n llm get secret vllm-secrets -o jsonpath='{.data.VLLM_API_KEY}' | base64 -d)"
  set_env_var ENDPOINT "http://$ip"
  set_env_var VLLM_API_KEY "$key"
  ok "Wrote ENDPOINT=http://$ip and VLLM_API_KEY to .env"
}

# Fail BEFORE creating anything if the cluster/firewall names are already taken.
# Both names derive from CLUSTER_LABEL (cluster = <label>, firewall = <label>-vllm),
# and both must be unique within the Linode account.
preflight_label_free() {
  local label="$1"
  if ! command -v curl >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
    warn "curl/python3 missing — skipping name pre-flight (Terraform will still error on a clash)"
    return 0
  fi
  log "Checking that '$label' (cluster) and '$label-vllm' (firewall) are free"
  local clusters firewalls
  clusters="$(curl -s -H "Authorization: Bearer $LINODE_TOKEN" https://api.linode.com/v4/lke/clusters)"
  firewalls="$(curl -s -H "Authorization: Bearer $LINODE_TOKEN" https://api.linode.com/v4/networking/firewalls)"
  if LABEL="$label" python3 -c 'import sys,json,os; d=json.load(sys.stdin); sys.exit(0 if any(c.get("label")==os.environ["LABEL"] for c in d.get("data",[])) else 1)' <<<"$clusters"; then
    die "An LKE cluster named '$label' already exists. Pick a unique CLUSTER_LABEL in .env."
  fi
  if LABEL="${label}-vllm" python3 -c 'import sys,json,os; d=json.load(sys.stdin); sys.exit(0 if any(f.get("label")==os.environ["LABEL"] for f in d.get("data",[])) else 1)' <<<"$firewalls"; then
    die "A firewall named '${label}-vllm' already exists. Pick a unique CLUSTER_LABEL in .env."
  fi
  ok "Names are free"
}

# ---- subcommands ---------------------------------------------------------

cmd_deploy() {
  require terraform; require kubectl; require helm; require openssl

  [ -f "$ROOT/.env" ] || die "No .env — copy .env.examples to .env and set LINODE_TOKEN first."
  [ -n "${LINODE_TOKEN:-}" ] || die "LINODE_TOKEN is not set in .env (or the environment)."
  export LINODE_TOKEN   # the Linode Terraform provider reads it from the env

  # Unique naming. CLUSTER_LABEL (from .env) is the single knob: it names the LKE
  # cluster and, as "<label>-vllm", the Cloud Firewall — both account-global, so
  # they must be unique. Passed to Terraform via TF_VAR_ (NOT tfvars, which would
  # override it). This is what lets you stand up a second stack without clashing.
  : "${CLUSTER_LABEL:?Set CLUSTER_LABEL in .env — must be unique in your Linode account}"
  export TF_VAR_cluster_label="$CLUSTER_LABEL"
  preflight_label_free "$CLUSTER_LABEL"

  # 1. Cluster + both Cloud Firewalls (one apply).
  log "Terraform apply (cluster + firewalls) as '$CLUSTER_LABEL'"
  if [ ! -f "$ROOT/terraform/terraform.tfvars" ]; then
    cp "$ROOT/terraform/terraform.tfvars.example" "$ROOT/terraform/terraform.tfvars"
    warn "Created terraform/terraform.tfvars from the example — edit it to change region/plan."
  fi
  terraform -chdir="$ROOT/terraform" init -input=false
  terraform -chdir="$ROOT/terraform" apply -auto-approve

  # 2. Kubeconfig.
  log "Writing kubeconfig"
  terraform -chdir="$ROOT/terraform" output -raw kubeconfig | base64 -d > "$ROOT/kubeconfig"
  ok "KUBECONFIG=$ROOT/kubeconfig"

  # 3. GPU Operator (NVIDIA drivers + nvidia.com/gpu on the scheduler).
  log "Installing GPU Operator (helm)"
  helm repo add nvidia https://helm.ngc.nvidia.com/nvidia >/dev/null 2>&1 || true
  helm repo update >/dev/null
  helm upgrade --install gpu-operator nvidia/gpu-operator \
    -n gpu-operator --create-namespace --wait --timeout 10m

  ensure_gpu_label

  # 4. Namespace + Secret. Reuse the existing token if the Secret already exists,
  #    so a re-run doesn't rotate the key out from under a running client.
  log "Namespace + Secret"
  kubectl apply -f "$ROOT/manifests/namespace.yaml"
  if kubectl -n llm get secret vllm-secrets >/dev/null 2>&1; then
    ok "vllm-secrets already exists — keeping its token"
  else
    local token; token="$(openssl rand -hex 32)"
    kubectl -n llm create secret generic vllm-secrets \
      --from-literal=VLLM_API_KEY="$token" \
      --from-literal=HUGGING_FACE_HUB_TOKEN="${HUGGING_FACE_HUB_TOKEN:-}"
    ok "Created vllm-secrets with a fresh random token"
  fi

  # 5. Workload, then attach the NodeBalancer firewall (only possible post-apply,
  #    once the CCM has provisioned the NodeBalancer for the LoadBalancer Service).
  log "Applying workload manifests"
  kubectl apply -f "$ROOT/manifests/"

  local fw_id; fw_id="$(terraform -chdir="$ROOT/terraform" output -raw firewall_id)"
  kubectl -n llm annotate svc vllm \
    "service.beta.kubernetes.io/linode-loadbalancer-firewall-id=$fw_id" --overwrite
  ok "Attached NodeBalancer firewall $fw_id"

  # 6. Wait for the model to load (first run downloads ~15GB).
  log "Waiting for the vLLM pod (first run pulls ~15GB — up to 15 min)"
  kubectl -n llm wait --for=condition=ready pod -l app=vllm --timeout=15m

  # 7. Write the chatbot/examples config.
  write_env_from_cluster

  printf '\n'
  ok "Deployed. Smoke-test it:"
  printf '    source .env && curl -s "$ENDPOINT/v1/models" -H "Authorization: Bearer $VLLM_API_KEY" | jq .\n'
  ok "Then run the agent locally:"
  printf '    ./deploy.sh chatbot\n'
}

cmd_env() {
  require kubectl
  [ -f "$ROOT/kubeconfig" ] || die "No kubeconfig — run ./deploy.sh deploy first."
  write_env_from_cluster
}

cmd_chatbot() {
  require python3
  [ -f "$ROOT/.env" ] || die "No .env — run ./deploy.sh deploy (or env) first."

  # Use a project-local virtualenv (.venv, gitignored). Installing into the
  # system Python fails on PEP 668 "externally-managed" distros (Debian/Ubuntu).
  local venv="$ROOT/.venv"
  if [ ! -d "$venv" ]; then
    log "Creating virtualenv at .venv"
    python3 -m venv "$venv" \
      || die "Could not create a venv. Install it first: sudo apt install python3-venv"
  fi

  log "Installing chatbot deps into .venv"
  "$venv/bin/pip" install -q --upgrade pip
  "$venv/bin/pip" install -q -r "$ROOT/examples/chatbot/requirements.txt"

  log "Starting Streamlit (Ctrl-C to stop)"
  # exec from the repo root so app.py's load_dotenv() picks up ./.env.
  exec "$venv/bin/streamlit" run "$ROOT/examples/chatbot/app.py"
}

cmd_destroy() {
  require terraform; require kubectl; require helm
  export LINODE_TOKEN="${LINODE_TOKEN:-}"
  # The firewall name interpolates cluster_label, so destroy needs it too.
  : "${CLUSTER_LABEL:?Set CLUSTER_LABEL in .env to the label this stack was deployed with}"
  export TF_VAR_cluster_label="$CLUSTER_LABEL"

  # Ordering matters: the CloudFirewall CRs must be deleted BEFORE terraform
  # destroy, or the controller is gone before it can clean up its Linode-side
  # firewall — which then leaks (keeps billing).
  log "Deleting workload + CloudFirewall CRs (must precede terraform destroy)"
  kubectl delete -f "$ROOT/manifests/" --ignore-not-found || true
  kubectl delete cloudfirewalls --all --ignore-not-found || true
  helm uninstall gpu-operator -n gpu-operator || true

  log "Terraform destroy"
  terraform -chdir="$ROOT/terraform" destroy -auto-approve

  warn "Done. The GPU node billed hourly — confirm it's gone in the Linode console."
}

# ---- dispatch ------------------------------------------------------------
case "${1:-deploy}" in
  deploy)  cmd_deploy  ;;
  env)     cmd_env     ;;
  chatbot) cmd_chatbot ;;
  destroy) cmd_destroy ;;
  *) die "unknown command: $1  (use: deploy | env | chatbot | destroy)" ;;
esac
