# Architecture

Why each piece of this stack exists, how the pieces fit, and what was
deliberately left out. The [README](README.md) is the step-by-step; this is the
reasoning behind it.

## Design goals

1. **Secure by default.** The cluster's public perimeter is locked down by the
   time `terraform apply` returns — not as a follow-up hardening step. A GPU node
   with an open inference endpoint is an expensive thing to leave exposed.
2. **OpenAI-compatible.** The endpoint speaks the OpenAI API so existing
   clients, SDKs, and agent frameworks (LangChain, LlamaIndex, Strands, …) work
   unchanged. No bespoke client.
3. **Cheap to run, cheap to tear down.** One GPU node, one model, block-storage
   model cache so restarts don't re-download. The whole thing is meant to come up
   for a demo and go away after.
4. **Legible.** Every manifest and Terraform resource is small and commented.
   You should be able to read the whole thing in a sitting.

## Who provisions what

The single most important structural fact: provisioning is split across three
tools, on purpose.

| Layer | Tool | Resources |
|---|---|---|
| Cloud infra + bootstrap security | **Terraform** (`terraform/`) | LKE cluster, CPU pool, GPU pool, NodeBalancer Cloud Firewall, cloud-firewall-controller (Helm) |
| In-cluster workload | **kubectl** (`manifests/`) | `llm` namespace, PVC, Secret, Deployment, Service, NetworkPolicy |
| Add-on operators | **Helm**, run by hand | NVIDIA GPU Operator, kube-prometheus-stack |

Two consequences fall out of this split:

- **Terraform does not create the NodeBalancer.** The Linode Cloud Controller
  Manager (CCM) provisions a NodeBalancer when the `LoadBalancer` Service is
  applied — which happens in the kubectl phase, after Terraform is done. That's
  why the NodeBalancer's firewall can only be attached *after* `kubectl apply`,
  via a Service annotation. (Some CCM versions ignore the post-hoc annotation;
  the `linode-cli firewalls device-create` fallback exists for that.)
- **Terraform installs the firewall controller but not the GPU operator.** The
  firewall controller is security-critical and must be running before the cluster
  is usable, so it's part of `terraform apply`. The GPU operator and monitoring
  are workload concerns, kept as explicit `helm install` steps so you can see and
  version them.

## The request path

```
Internet
  │
  ▼  TCP 80/443 from allowed_cidr only
[ Cloud Firewall #1 ]  ── attached to the NodeBalancer
  │
  ▼
[ NodeBalancer ]  ── created by the Linode CCM from the LoadBalancer Service
  │  forwards to a NodePort on the worker nodes
  ▼  NodePort range is public on each node's IP by default…
[ Cloud Firewall #2 ]  ── attached to every worker node; drops that public range
  │  allows only NodeBalancer-subnet + cluster-internal traffic
  ▼
[ vLLM pod ]  :8000  ── bearer-token auth on /v1/*, NetworkPolicy in front
  │
  ▼
[ GPU ]  /dev/nvidia0  ── exposed by the GPU Operator's device plugin
```

## Component rationale

### LKE cluster with two node pools
A small CPU pool runs system pods, the GPU operator's controllers, Prometheus,
and Grafana. A separate GPU pool runs only the model. Keeping them separate means
the (expensive, hourly) GPU node isn't carrying system overhead, and the pools
can be scaled, replaced, or labeled independently. The GPU pool carries the
`pool=gpu` Kubernetes label; the vLLM Deployment's `nodeSelector: pool=gpu` is
how the workload lands there and nowhere else. (Some Linode provider versions
don't propagate that label — apply it by hand if pods stay `Pending`.)

### NVIDIA GPU Operator
A GPU node is just a Linux box with a PCIe card until something installs the
driver, the CUDA runtime hooks, the Kubernetes device plugin, and the DCGM
metrics exporter. The GPU Operator does all of that as a set of DaemonSets and
exposes the GPU to the scheduler as the allocatable resource `nvidia.com/gpu`.
Without it the vLLM pod can't request or see the GPU. The operator also taints
GPU nodes, which is why the Deployment carries a matching toleration.

### vLLM Deployment
The workhorse. A few choices that aren't obvious from the YAML:

- **`enableServiceLinks: false`** — Kubernetes injects env vars for every
  Service (`<NAME>_PORT=tcp://…`). A Service named `vllm` would inject
  `VLLM_PORT=tcp://…:80`, which vLLM parses as its own port config and crashes
  on. Disabling service-link injection avoids the collision.
- **`strategy: Recreate`** — one GPU and a `ReadWriteOnce` PVC mean two pods
  can't run at once; a rolling update would deadlock with both pods wanting the
  GPU and volume. Recreate tears the old pod down first. The cost is a brief
  outage on every config change (and that's the cold-start the demo measures).
- **`securityContext`** — `allowPrivilegeEscalation: false`, all capabilities
  dropped, `seccompProfile: RuntimeDefault`. The container still runs as root
  (CUDA wants it), but it can't escalate or reach unusual syscalls.
- **Probes** — a generous `startupProbe` (up to ~10 min) keeps the liveness
  probe from killing the pod while it downloads and loads ~15 GB of weights.
- **`/dev/shm` `emptyDir`** — vLLM uses shared memory for tensor work; the
  container default of 64 MiB is far too small, so it's backed by an 8 GiB
  memory-medium volume.
- **Tool calling** — `--enable-auto-tool-choice --tool-call-parser hermes` turn
  on OpenAI-style function calling. Qwen2.5-Instruct emits Hermes-format tool
  calls, so `hermes` is the right parser. Without these flags vLLM silently
  ignores the `tools` field and agents (e.g. `examples/chatbot/`) never get a
  tool call back.

### PVC with the `-retain` storage class
Model weights are cached on a Linode Block Volume. Without it, every pod restart
re-downloads ~15 GB from Hugging Face — 5–10 minutes of failed requests. With it,
a new pod mounts the volume and reads weights locally, turning a pod death into a
~60-second blip. The `linode-block-storage-retain` class keeps the volume when
the PVC is deleted, so you don't lose the cache on an accidental `kubectl delete`.
(Flip to `linode-block-storage` if you'd rather it delete with the PVC.)

### Service → NodeBalancer
A `type: LoadBalancer` Service makes the CCM provision a NodeBalancer (Linode's
managed L4 LB) and wire it to the Service's NodePort. This is the public ingress.

### Two Cloud Firewalls
This is the security spine, and the two firewalls are not redundant — they guard
different things:

- **NodeBalancer firewall (#1, Terraform-managed).** Restricts inbound to TCP
  80/443 from `allowed_cidr`. This is the front door. Set `allowed_cidr` to your
  IP to make the endpoint private to you.
- **Worker-node firewall (#2, cloud-firewall-controller).** A LoadBalancer
  Service opens a NodePort (30000–32768) on every node — and on Linode those node
  IPs are public, so without this firewall the NodePort is reachable from the
  internet, bypassing the NodeBalancer firewall entirely. The controller attaches
  a Linode firewall to every worker node that drops that public range while
  allowing NodeBalancer-subnet and cluster-internal traffic, and re-applies it to
  recycled/new nodes automatically.

Firewall #1 without #2 is a locked front door next to an open window.

### NetworkPolicy
`deny-ingress` is a default-deny for the `llm` namespace so pods can't reach each
other freely; `allow-vllm` then permits traffic to `vllm:8000` (the NodeBalancer
source is external, so the rule has no `from` selector). Defense in depth behind
the firewalls.

### Secret + bearer token
`vllm-secrets` holds `VLLM_API_KEY` (and an optional Hugging Face token for gated
models). The same key plays two roles: vLLM reads it via `envFrom` as
`--api-key`, and clients send it as `Authorization: Bearer …`. vLLM rejects
unauthenticated `/v1/*` requests.

### Monitoring
kube-prometheus-stack (Prometheus + Grafana) plus two PodMonitors: one scrapes
vLLM's `/metrics` (TTFT, queue depth, KV-cache usage, throughput), the other
scrapes the GPU operator's DCGM exporter (GPU util, VRAM, power). Both services
are ClusterIP — reached via `kubectl port-forward`, so monitoring adds no public
surface, no firewall, and no cost. It's optional, but the failure-mode demos are
far more legible with the dashboards next to the terminal.

## Tool calling and agents

Because the endpoint is OpenAI-compatible *and* has tool calling enabled, you can
point an agent framework at it and the model will request tool calls in the
standard format. `examples/chatbot/` is a Strands agent (Streamlit UI) that does
exactly this — `http_request`, `calculator`, `current_time`, and a custom tool —
and surfaces each tool call in the UI. The agent is unaware it's talking to a
self-hosted 7B model rather than a hosted frontier model; that's the point of
standardizing on the OpenAI surface.

## What was deliberately left out

These are demo-scoping decisions, each with the production path noted:

- **TLS.** The endpoint is plain HTTP, so the bearer token travels in the clear.
  For production, terminate TLS at an Ingress (cert-manager + Let's Encrypt).
- **Per-user auth / rate limiting.** The bearer token is a shared secret, not an
  identity. Layer `oauth2-proxy` + Ingress for real auth.
- **High availability / autoscaling.** One replica, one GPU. HPA is undermined by
  the cold-start cost (every new replica reloads weights), which is why
  multi-replica is a deliberate decision, not a default — see the cold-start demo.
- **Image pinning.** `vllm/vllm-openai:latest` is trust-on-first-use; pin a
  digest for production.
- **Egress NetworkPolicy.** The vLLM pod can reach anything outbound (it needs
  `huggingface.co`). Tighten with an egress allow-list in production.
- **Logs and tracing.** `kubectl logs` is enough for the demo. Add Loki for
  aggregated logs and Tempo/Jaeger (`--otlp-traces-endpoint`) for traces when you
  build multi-step agents on top.

## Failure modes the demo exercises

The `demo/` scripts deliberately push the system past its limits to show how it
degrades — and recovers — rather than crashes:

| Demo | Bottleneck | Lesson |
|---|---|---|
| `loadgen.py` | compute | continuous batching serves many concurrent users at near-flat latency |
| `kv_cache_pressure.py` | memory | KV cache fills before compute does; the engine queues instead of OOMing |
| `apply_bounded.sh` | — | bounded admission (`--max-num-seqs`, `--max-model-len`) caps both failure modes |
| `cold_start.sh` | startup | the PVC turns a 10-minute re-download into a ~60-second restart |

See [`demo/README.md`](demo/README.md) for the run order and talk narration.
