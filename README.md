# Serve an LLM on GPU with LKE (vllm-on-lke)

Deploy an OpenAI-compatible LLM endpoint on a GPU in Linode Kubernetes Engine.

**Stack:** Terraform + LKE + vLLM + Qwen2.5-7B-Instruct + 1× NVIDIA RTX 4000 Ada + bearer-token auth

The Terraform brings up the cluster *and* installs the worker-node firewall in one step, so the cluster never sits exposed.

---

## Architecture

```
                  Internet
                     │
                     ▼
     ┌────────────────────────────────────┐
     │  Cloud Firewall #1                 │  only allows :80 / :443
     │  attached to the NodeBalancer      │  from your allowed_cidr
     └──────────────┬─────────────────────┘
                    │
                    ▼
              [ NodeBalancer ]            (provisioned by Linode CCM
                    │                      when the vLLM Service applies)
                    │  forwards to NodePort on workers
                    ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  Cloud Firewall #2 — attached to every worker node           │
   │  drops public NodePort range; only NB subnet + cluster traffic│
   │ ──────────────────────────────────────────────────────────── │
   │                                                                │
   │   CPU node 1         CPU node 2          GPU node              │
   │   ┌──────────┐       ┌──────────┐        ┌────────────────┐   │
   │   │ system   │       │ system   │        │  vLLM pod      │   │
   │   │ pods     │       │ pods     │        │  ─ Qwen 7B     │   │
   │   │ Grafana  │       │Prometheus│        │  ─ KV cache    │   │
   │   └──────────┘       └──────────┘        │  ─ /dev/nvidia0│   │
   │                                          │       ▲        │   │
   │                                          │       │        │   │
   │                                          │  GPU operator  │   │
   │                                          │  daemonset     │   │
   │                                          │  loads NVIDIA  │   │
   │                                          │  driver into   │   │
   │                                          │  host kernel ──┘   │
   │                                          └────────┬───────┘   │
   │                                                   │           │
   │                                          ┌────────▼────────┐  │
   │                                          │  Block Volume   │  │
   │                                          │  PVC (50 GB)    │  │
   │                                          │  model cache    │  │
   │                                          └─────────────────┘  │
   └──────────────────────────────────────────────────────────────┘

Provisioned by:
  Terraform     → cluster, node pools, both firewalls, cloud-firewall-controller
  kubectl/helm  → GPU operator, vLLM workload, monitoring stack
```

Two firewalls on purpose: #1 protects the NodeBalancer (the public ingress), #2 protects every worker node's public IP from the otherwise-open NodePort range. See `ARCHITECTURE.md` for the deeper "why each component exists" writeup.

---

## Heads up on cost

The GPU node is [billed](https://www.akamai.com/cloud/pricing) **hourly**. Don't forget to tear it down when you're done — see [Step 9](#9-tear-down).

---

## Prerequisites

**Account:**
- Create an [Akamai Cloud account](http://login.linode.com/signup?promo=akm-dev-git-300-31126-M055) with an API token (includes a $300 credit).

**CLIs installed locally:**
- `terraform` (>= 1.5) — provisions the cluster, node pools, and firewalls.
- `kubectl` — applies workload manifests, inspects pods, port-forwards.
- `helm` — installs the GPU Operator and the monitoring stack.
- `linode-cli` — used as the fallback to attach the NodeBalancer firewall (Step 6) and to verify firewall state.
- `openssl` — generates the random bearer token in Step 5.
- `jq` — parses JSON output from `kubectl` and `linode-cli`.

**Optional (only if you'll run the Python examples or demo load tests):**
- Python 3.10+ with `pip`. `examples/openai-client.py` needs `openai`; `demo/loadgen.py` and `demo/kv_cache_pressure.py` need `httpx`.

---

## 1. Set your Linode API token

```bash
export LINODE_TOKEN=<your-token>
```

The Terraform provider reads it from this env var. **Don't** put it in `tfvars`.

---

## 2. Provision the cluster (and the worker node firewall)

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars   # edit if you want different region/instance types
terraform init
terraform plan                                  # review what's about to be created
terraform apply
```

This single `terraform apply` provisions:

- The LKE cluster, a CPU pool, and a GPU pool (labeled `pool=gpu`)
- A Linode Cloud Firewall for the NodeBalancer (allows TCP 80/443 from `allowed_cidr`)
- The **cloud-firewall-controller** (installed via Helm), which creates a second Cloud Firewall and attaches it to every worker node — closing the otherwise-open NodePort range on the nodes' public IPs

By the time `terraform apply` returns, the cluster's perimeter is locked down.

---

## 3. Pull kubeconfig

Still in the `terraform/` directory:

```bash
terraform output -raw kubeconfig | base64 -d > ../kubeconfig
cd ..
export KUBECONFIG=$PWD/kubeconfig
kubectl get nodes
```

You should see CPU + GPU nodes listed. Verify the firewall controller is healthy and the per-node Cloud Firewall is in place:

```bash
kubectl -n kube-system get pods -l app.kubernetes.io/name=cloud-firewall-controller
kubectl get cloudfirewalls -A
linode-cli firewalls list
```

You should see **two** Cloud Firewalls in your Linode account:

- `lke-<cluster-id>` — created by the controller, attached to all 3 worker nodes.
- `lke-gpu-demo-vllm` (or whatever you set `cluster_label` to) — created by Terraform, attached to the NodeBalancer once the vLLM Service exists in Step 6.

---

## 4. Install the NVIDIA GPU Operator

The GPU Operator installs NVIDIA drivers on the GPU node and exposes the GPU to Kubernetes as a schedulable resource (`nvidia.com/gpu`). Without this, your vLLM pod can't claim the GPU.

```bash
helm repo add nvidia https://helm.ngc.nvidia.com/nvidia
helm repo update
helm install gpu-operator nvidia/gpu-operator \
  --namespace gpu-operator --create-namespace \
  --wait --timeout 10m
```

Driver install takes 2–3 minutes. Watch progress:

```bash
kubectl -n gpu-operator get pods -w   # ctrl-c when everything is Running/Completed
```

Confirm the GPU node has the `pool=gpu` label and that the scheduler can see the GPU:

```bash
kubectl get nodes -L pool
kubectl get nodes -o json | jq '.items[].status.allocatable | select(."nvidia.com/gpu")'
```

You should see `pool=gpu` on the GPU node, and `"nvidia.com/gpu": "1"` in the allocatable output. If the label is missing (some Linode provider versions skip propagation), apply it manually:

```bash
kubectl label node <gpu-node-name> pool=gpu
```

---

## 5. Create the namespace and secrets

Create the `llm` namespace where the workload lives:

```bash
kubectl apply -f manifests/namespace.yaml
kubectl -n llm get all       # should be empty — namespace is just a folder
```

Generate a random API key and store it (along with a placeholder for the Hugging Face token) in a Kubernetes Secret. The vLLM Deployment reads this Secret via `envFrom` and uses `VLLM_API_KEY` as the bearer token clients must send to call `/v1/*`.

```bash
VLLM_API_KEY=$(openssl rand -hex 32)
kubectl -n llm create secret generic vllm-secrets \
  --from-literal=VLLM_API_KEY=$VLLM_API_KEY \
  --from-literal=HUGGING_FACE_HUB_TOKEN=

echo "Save this: export VLLM_API_KEY=$VLLM_API_KEY"
```

For gated models (Llama, Mistral), pass your Hugging Face token instead of empty: `--from-literal=HUGGING_FACE_HUB_TOKEN=hf_...`. Qwen2.5-7B-Instruct (the default) is ungated, so empty is fine.

---

## 6. Deploy vLLM

```bash
kubectl apply -f manifests/

# Attach the NodeBalancer Cloud Firewall (created by Terraform) to the Service.
# Some CCM versions don't honor this annotation post-hoc; if the firewall
# isn't attached after this, fall back to the linode-cli command in the
# Security section below.
FIREWALL_ID=$(cd terraform && terraform output -raw firewall_id)
kubectl -n llm annotate svc vllm \
  service.beta.kubernetes.io/linode-loadbalancer-firewall-id=$FIREWALL_ID --overwrite
```

Verify the NodeBalancer was added to the firewall

```bash
linode-cli firewalls devices-list $FIREWALL_ID
```

If unsuccessful run:

```bash
LB_IP=$(kubectl -n llm get svc vllm -o jsonpath='{.status.loadBalancer.ingress[0].ip}')               

NB_ID=$(linode-cli nodebalancers list --json | jq -r ".[] | select(.ipv4 == \"$LB_IP\") | .id")       

linode-cli firewalls device-create $FIREWALL_ID --type nodebalancer --id $NB_ID
```

Wait for the model to load.

```bash
kubectl -n llm wait --for=condition=ready pod -l app=vllm --timeout=15m
```

The first run downloads ~15 GB of model weights from Hugging Face. **This takes 5-10 minutes.** Watch progress:

```bash
kubectl -n llm logs -f deploy/vllm
```

---

## 7. Test the endpoint

Pull the bearer token straight from the Secret and the IP from the Service — no copy-paste from earlier output needed:

```bash
export VLLM_API_KEY=$(kubectl -n llm get secret vllm-secrets -o jsonpath='{.data.VLLM_API_KEY}' | base64 -d)
ENDPOINT=$(kubectl -n llm get svc vllm -o jsonpath='{.status.loadBalancer.ingress[0].ip}')

curl -s http://$ENDPOINT/v1/chat/completions \
  -H "Authorization: Bearer $VLLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen2.5-7B-Instruct",
    "messages": [{"role": "user", "content": "What is agentic AI?"}]
  }' | jq .
```

Done — your endpoint is live and OpenAI-compatible. Two Python examples ready to run:

- `examples/openai-client.py` — single request, full response (simplest).
- `examples/openai_streaming_client.py` — streams token-by-token and reports TTFT, total elapsed, tok/s.

---

## 8. Set up monitoring (optional, but the demo is way better with it)

Install Prometheus + Grafana, scrape vLLM and the GPU's DCGM exporter, and visualize both. With this you can hit the endpoint and watch GPU utilization, TTFT, and KV cache usage move in real time.

### Install the stack

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

helm install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace \
  -f manifests/monitoring/kube-prometheus-stack-values.yaml \
  --wait --timeout 10m

# Tell Prometheus what to scrape (vLLM /metrics + DCGM /metrics)
kubectl apply -f manifests/monitoring/podmonitors.yaml
```

Both Prometheus and Grafana use `ClusterIP` services — no public IPs, no new firewalls, no extra cost. You reach them via `kubectl port-forward`, which tunnels through the existing Kubernetes API server connection.

### Open Grafana and Prometheus

In one terminal:
```bash
kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80
```
Open http://localhost:3000 — log in with `admin` / `prom-operator`.

In a second terminal (handy for debugging scrape targets):
```bash
kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090:9090
```
Open http://localhost:9090 — go to **Status → Target health** to see what's being scraped.

### Import dashboards

The chart pre-loads an *NVIDIA DCGM Exporter* dashboard, but the gnetId in the values file occasionally falls out of sync with current Grafana.com revisions. If panels are empty, import the current one:

1. Grafana → Dashboards → New → Import
2. Paste: `15117` (NVIDIA's current "DCGM Exporter Dashboard") → Load
3. Datasource: Prometheus → Import

Then import the vLLM dashboard (Grafana.com ID drifts between releases — point at vLLM's source-of-truth JSON):

1. Dashboards → New → Import
2. Paste URL: `https://raw.githubusercontent.com/vllm-project/vllm/main/examples/production_monitoring/grafana.json`
3. Datasource: Prometheus → Import

### Verify scraping is working

Quick check from the Prometheus UI (http://localhost:9090/graph), enter:
```
DCGM_FI_DEV_GPU_UTIL
```
If you get a value, GPU metrics are flowing. Empty = troubleshoot below.

### Troubleshooting

**"No data" on the DCGM dashboard's variable dropdowns (instance, gpu).** Dashboard 12239 filters by `job="dcgm-exporter"` but the PodMonitor generates `job="monitoring/nvidia-dcgm-exporter"`. Use dashboard `15117` (more permissive variable queries) or edit the existing dashboard's variables to drop the job filter.

**Prometheus has zero DCGM targets** (`http://localhost:9090/targets` shows nothing for dcgm). The default DCGM port name from the GPU operator is `metrics`, not `gpu-metrics`. Check the actual port name and update the PodMonitor:

```bash
DCGM_POD=$(kubectl -n gpu-operator get pods -l app=nvidia-dcgm-exporter -o name | head -1)
kubectl -n gpu-operator get $DCGM_POD -o jsonpath='{.spec.containers[*].ports}' | jq
```

If port is `metrics` instead of `gpu-metrics`, edit `manifests/monitoring/podmonitors.yaml` to match, then `kubectl apply -f manifests/monitoring/podmonitors.yaml`.

**Dashboard panels lag terminal output by 15 seconds.** Default scrape interval. For the demo, set Grafana auto-refresh to 5s (top-right dropdown) and lower PodMonitor `interval: 5s`.

### For the demo

Open Grafana in one tab, run the curl from Step 7 in another, watch GPU utilization spike to ~100% during inference. That's the picture worth showing. The full demo flow (load generator, KV cache pressure, bounded admission, cold start) is in `demo/README.md`.

### Logs (deferred)

`kubectl -n llm logs -f deploy/vllm` is sufficient for the demo. For aggregated logs in the same Grafana, install [Loki](https://grafana.com/oss/loki/) — it adds a `Loki` datasource and a Logs Explorer view. One additional helm install, not in this quickstart.

### Tracing (deferred)

vLLM supports OpenTelemetry traces via `--otlp-traces-endpoint=http://tempo:4318`. To wire it up you'd deploy [Tempo](https://grafana.com/oss/tempo/) (Grafana-native) or Jaeger, then add the flag to `manifests/vllm-deployment.yaml` and restart the pod. Useful when you build multi-step agents on top of this endpoint; overkill for raw chat completions.

---

## 9. Tear down

```bash
# Workload first
kubectl delete -f manifests/ --ignore-not-found

# Monitoring stack (if installed in Step 8)
kubectl delete -f manifests/monitoring/podmonitors.yaml --ignore-not-found
helm uninstall kube-prometheus-stack -n monitoring 2>/dev/null
kubectl delete namespace monitoring --ignore-not-found

# Delete CloudFirewall CRs so the controller cleans up its Linode-side firewall
# BEFORE terraform destroy uninstalls the controller
kubectl delete cloudfirewalls --all --ignore-not-found

# GPU operator (still a manual helm release)
helm uninstall gpu-operator -n gpu-operator

# Cluster + NodeBalancer firewall + cloud-firewall-controller (all Terraform-managed)
cd terraform && terraform destroy
```

Confirm in the Linode console that the cluster, NodeBalancer, block volume, and **both** Cloud Firewalls (NodeBalancer-side + node-side) are gone.

---

## Security

Two layers of Linode Cloud Firewall plus in-cluster controls:

- **NodeBalancer firewall** (Terraform-managed) — only TCP 80/443 inbound, from `allowed_cidr`. Restrict to your IP by setting `allowed_cidr = "203.0.113.10/32"` in `terraform.tfvars` and re-applying.
- **Worker node firewall** (Terraform-installed cloud-firewall-controller) — drops all public traffic except cluster-internal and NodeBalancer ranges. Closes the NodePort gap. Auto-applies to recycled/new nodes.
- **Bearer token** — vLLM's `--api-key` rejects requests to `/v1/*` without `Authorization: Bearer <token>`. Token is in the `vllm-secrets` Secret.
- **NetworkPolicy** — `manifests/networkpolicy.yaml` defines a default-deny ingress in the `llm` namespace plus an allow rule to `vllm:8000`.
- **Pod `securityContext`** — `allowPrivilegeEscalation: false`, all Linux capabilities dropped, `seccompProfile: RuntimeDefault`. Container still runs as root because CUDA needs it, but it can't escalate or use unusual syscalls.

What's not done (by design, for a demo):

- **TLS** — endpoint is HTTP. The bearer token is in the clear over the network. For prod, terminate TLS at an Ingress (cert-manager + Let's Encrypt is the usual path).
- **Per-user auth / rate limiting** — bearer token is a shared secret, not an identity. For internal-tool deployments, layer `oauth2-proxy` + Ingress on top.
- **Image pinning / scanning** — `vllm/vllm-openai:latest` is trust-on-first-use. Pin a digest for prod.
- **Egress NetworkPolicy** — the vLLM pod can talk outbound to anything (needs to reach `huggingface.co`). Tighten with allowed-egress rules in prod.

### If the NodeBalancer firewall annotation didn't take

Some CCM versions ignore the post-hoc annotation. Attach manually:

```bash
FIREWALL_ID=$(cd terraform && terraform output -raw firewall_id)
LB_IP=$(kubectl -n llm get svc vllm -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
NB_ID=$(linode-cli nodebalancers list --json | jq -r ".[] | select(.ipv4 == \"$LB_IP\") | .id")
linode-cli firewalls device-create $FIREWALL_ID --type nodebalancer --id $NB_ID
```

---

## Troubleshooting

**`terraform apply` fails on `helm_release.cloud_firewall_*`** — the cluster's API server may not have been ready when helm tried to connect. Re-run `terraform apply`; helm provider is idempotent and will retry.

**Pod stuck in `Pending`** — `kubectl -n llm describe pod`. GPU node missing the `pool=gpu` label, or GPU Operator drivers not ready yet (wait 2-3 minutes).

**Pod in `CrashLoopBackOff`** — `kubectl -n llm logs deploy/vllm`. If you see Hugging Face download progress, wait. CUDA errors usually mean the GPU Operator hasn't bound the GPU yet — the pod will recover on the next restart.

**`401 Unauthorized`** — wrong bearer token, or missing `Bearer ` prefix.

**Step 6 timed out** — the model is still downloading. `kubectl -n llm logs -f deploy/vllm` to watch.

**`kubectl get cloudfirewalls` shows nothing** — the controller may not have reconciled yet. Wait 30 seconds and re-check, or `kubectl -n kube-system logs deploy/cloud-firewall-controller` for errors.

---

## What's next

- **Run the failure-mode demos** — see `demo/README.md` for `loadgen.py`, `kv_cache_pressure.py`, `apply_bounded.sh`, and `cold_start.sh`.
- **Swap the model** — edit `--model=` in `manifests/vllm-deployment.yaml`. For gated models, set the HF token (see Step 5).
- **OpenAI Python SDK** — see `examples/openai-client.py` (simple) and `examples/openai_streaming_client.py` (streams + reports TTFT).
- **Architecture deep-dive** — see `ARCHITECTURE.md` for what each component does, why it exists, and what was deliberately left out.

---

## Cheat sheet

Common commands you'll reach for repeatedly.

### Endpoint + token (export once per terminal)

```bash
export VLLM_API_KEY=$(kubectl -n llm get secret vllm-secrets -o jsonpath='{.data.VLLM_API_KEY}' | base64 -d)
export ENDPOINT=http://$(kubectl -n llm get svc vllm -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
```

### Cluster + pod state

```bash
kubectl get nodes -L pool                                         # nodes + pool labels
kubectl -n llm get pod -l app=vllm                                # is vLLM ready?
kubectl -n llm logs -f deploy/vllm                                # tail vLLM logs
kubectl -n llm exec -it deploy/vllm -- nvidia-smi                 # GPU usage
kubectl -n llm exec -it deploy/vllm -- watch -n 1 nvidia-smi      # GPU usage, live
```

### vLLM config

```bash
# What the engine reports about itself (max_model_len, etc.)
curl -s $ENDPOINT/v1/models -H "Authorization: Bearer $VLLM_API_KEY" | jq

# Live metrics (cache config, request stats, throughput counters)
curl -s $ENDPOINT/metrics | grep -E "vllm:cache_config_info|num_requests|tokens_total"
```

### Monitoring

```bash
# Port-forwards (run in separate terminals)
kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80
kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090:9090

# Did Prometheus pick up our PodMonitors?
curl -s http://localhost:9090/api/v1/targets | jq -r '.data.activeTargets[].labels.job' | sort -u

# Force Prometheus to re-render scrape config after a PodMonitor change
kubectl -n monitoring rollout restart sts/prometheus-kube-prometheus-stack-prometheus

# What labels does DCGM expose? (useful for fixing dashboard variables)
curl -s 'http://localhost:9090/api/v1/query?query=DCGM_FI_DEV_GPU_UTIL' | jq '.data.result[0].metric'
```

### Firewall verification

```bash
# Both firewalls and what's attached
linode-cli firewalls list
linode-cli firewalls devices-list <FIREWALL_ID>

# Confirm the NodeBalancer firewall caught the NodeBalancer
FIREWALL_ID=$(cd terraform && terraform output -raw firewall_id)
linode-cli firewalls devices-list $FIREWALL_ID
```