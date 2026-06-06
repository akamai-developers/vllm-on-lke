# Architecture

This page explains why each piece of the stack exists, how the pieces fit together, and what was left out. For the quick start, see [README.md](README.md). For the manual step-by-step walkthrough, see [docs/deployment.md](docs/deployment.md).

## Design goals

1. **Secure by default.** The cluster's public perimeter is locked down by the time `terraform apply` returns, not as a follow-up hardening step. A GPU node with an open inference endpoint is expensive to leave exposed.
2. **OpenAI-compatible.** The endpoint speaks the OpenAI API, so existing clients, SDKs, and agent frameworks (LangChain, LlamaIndex, Strands, and others) work unchanged. You do not need a bespoke client.
3. **Cheap to run and tear down.** The stack uses one GPU node, one model, and a block-storage model cache so restarts do not re-download weights. It is designed to come up for a demo and go away afterward.
4. **Legible.** Every manifest and Terraform resource is small and commented. You can read the whole stack in one sitting.

## Who provisions what

The most important structural fact is that provisioning is split across three tools on purpose.

| Layer | Tool | Resources |
|---|---|---|
| Cloud infra + bootstrap security | **Terraform** (`terraform/`) | LKE cluster, CPU pool, GPU pool, NodeBalancer Cloud Firewall, cloud-firewall-controller (Helm) |
| In-cluster workload | **kubectl** (`manifests/`) | `llm` namespace, PVC, Secret, Deployment, Service, NetworkPolicy |
| Add-on operators | **Helm**, run by hand | NVIDIA GPU Operator, kube-prometheus-stack |

Two consequences follow from this split:

- **Terraform does not create the NodeBalancer.** The Linode Cloud Controller Manager (CCM) provisions a NodeBalancer when the `LoadBalancer` Service is applied, which happens in the kubectl phase after Terraform is done. As a result, the NodeBalancer's firewall can only be attached after `kubectl apply`, using a Service annotation. Some CCM versions ignore the post-hoc annotation, so the `linode-cli firewalls device-create` fallback exists for that case.
- **Terraform installs the firewall controller but not the GPU operator.** The firewall controller is security-critical and must be running before the cluster is usable, so it is part of `terraform apply`. The GPU operator and monitoring are workload concerns, kept as explicit `helm install` steps so you can see and version them.

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
  ▼  NodePort range is public on each node's IP by default
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

A small CPU pool runs system pods, the GPU operator's controllers, Prometheus, and Grafana. A separate GPU pool runs only the model. Keeping them separate means the expensive, hourly GPU node does not carry system overhead, and the pools can be scaled, replaced, or labeled independently.

The GPU pool carries the `pool=gpu` Kubernetes label. The vLLM Deployment's `nodeSelector: pool=gpu` is how the workload lands there and nowhere else.

!!! note
    Some Linode provider versions do not propagate the `pool=gpu` label. Apply it by hand if pods stay `Pending`.

### NVIDIA GPU Operator

A GPU node is a Linux box with a PCIe card until something installs the driver, the CUDA runtime hooks, the Kubernetes device plugin, and the DCGM metrics exporter. The GPU Operator installs all of that as a set of DaemonSets and exposes the GPU to the scheduler as the allocatable resource `nvidia.com/gpu`.

Without the operator, the vLLM pod cannot request or see the GPU. The operator also taints GPU nodes, which is why the Deployment carries a matching toleration.

### vLLM Deployment

The vLLM Deployment is the workhorse. Several choices are not obvious from the YAML:

- **`enableServiceLinks: false`** disables the env vars Kubernetes injects for every Service (`<NAME>_PORT=tcp://...`). A Service named `vllm` would inject `VLLM_PORT=tcp://...:80`, which vLLM parses as its own port config and crashes on. Disabling service-link injection avoids the collision.
- **`strategy: Recreate`** is required because one GPU and a `ReadWriteOnce` PVC mean two pods cannot run at once. A rolling update would deadlock with both pods wanting the GPU and volume. Recreate tears the old pod down first. The cost is a brief outage on every config change, which is the cold start the demo measures.
- **`securityContext`** sets `allowPrivilegeEscalation: false`, drops all capabilities, and sets `seccompProfile: RuntimeDefault`. The container still runs as root because CUDA requires it, but it cannot escalate or reach unusual syscalls.
- **Probes** include a generous `startupProbe` (up to ~10 min) that keeps the liveness probe from killing the pod while it downloads and loads ~15 GB of weights.
- **`/dev/shm` `emptyDir`** backs vLLM's shared memory for tensor work. The container default of 64 MiB is far too small, so it is backed by an 8 GiB memory-medium volume.
- **Tool calling** uses `--enable-auto-tool-choice --tool-call-parser hermes` to turn on OpenAI-style function calling. Qwen2.5-Instruct emits Hermes-format tool calls, so `hermes` is the correct parser. Without these flags, vLLM silently ignores the `tools` field and agents (for example, `examples/chatbot/`) never get a tool call back.

### PVC with the `-retain` storage class

Model weights are cached on a Linode Block Volume. Without the cache, every pod restart re-downloads ~15 GB from Hugging Face, which is 5 to 10 minutes of failed requests. With it, a new pod mounts the volume and reads weights locally, turning a pod death into a ~60-second restart.

The `linode-block-storage-retain` class keeps the volume when the PVC is deleted, so you do not lose the cache on an accidental `kubectl delete`. Switch to `linode-block-storage` if you want the volume to delete with the PVC.

### Service to NodeBalancer

A `type: LoadBalancer` Service makes the CCM provision a NodeBalancer (Linode's managed L4 load balancer) and wire it to the Service's NodePort. This is the public ingress.

### Two Cloud Firewalls

The two firewalls form the security spine. They are not redundant, because they guard different things:

- **NodeBalancer firewall (#1, Terraform-managed)** restricts inbound to TCP 80/443 from `allowed_cidr`. This is the front door. Set `allowed_cidr` to your IP to make the endpoint private to you.
- **Worker-node firewall (#2, cloud-firewall-controller)** closes the NodePort range that a LoadBalancer Service opens. A LoadBalancer Service opens a NodePort (30000-32768) on every node, and on Linode those node IPs are public. The controller attaches a Linode firewall to every worker node that drops that public range while allowing NodeBalancer-subnet and cluster-internal traffic, and it re-applies the firewall to recycled and new nodes automatically.

Firewall #1 alone leaves the NodePort range on each node's public IP reachable, bypassing the NodeBalancer firewall. Firewall #2 closes that gap.

### NetworkPolicy

`deny-ingress` is a default-deny for the `llm` namespace, so pods cannot reach each other freely. `allow-vllm` then permits traffic to `vllm:8000`. The NodeBalancer source is external, so the rule has no `from` selector. This provides defense in depth behind the firewalls.

### Secret + bearer token

`vllm-secrets` holds `VLLM_API_KEY` and an optional Hugging Face token for gated models. The same key plays two roles: vLLM reads it via `envFrom` as `--api-key`, and clients send it as `Authorization: Bearer ...`. vLLM rejects unauthenticated `/v1/*` requests.

### Monitoring

kube-prometheus-stack (Prometheus and Grafana) runs with two PodMonitors. One scrapes vLLM's `/metrics` (TTFT, queue depth, KV-cache usage, throughput), and the other scrapes the GPU operator's DCGM exporter (GPU util, VRAM, power).

Both services are ClusterIP and are reached via `kubectl port-forward`, so monitoring adds no public surface, no firewall, and no cost. It is optional, but the failure-mode demos are easier to read with the dashboards next to the terminal.

## Tool calling and agents

Because the endpoint is OpenAI-compatible and has tool calling enabled, you can point an agent framework at it and the model requests tool calls in the standard format. `examples/chatbot/` is a Strands agent with a Streamlit UI that does exactly this, using `http_request`, `calculator`, `current_time`, and a custom tool, and surfaces each tool call in the UI. See [examples/chatbot/README.md](examples/chatbot/README.md) for details.

The agent is unaware it is talking to a self-hosted 7B model rather than a hosted frontier model. That is the point of standardizing on the OpenAI surface.

## Left out by design

These are demo-scoping decisions, each with the production path noted:

- **TLS.** The endpoint is plain HTTP, so the bearer token travels in the clear. For production, terminate TLS at an Ingress (cert-manager with Let's Encrypt).
- **Per-user auth and rate limiting.** The bearer token is a shared secret, not an identity. Layer `oauth2-proxy` and Ingress for real auth.
- **High availability and autoscaling.** The stack runs one replica on one GPU. HPA is undermined by the cold-start cost, because every new replica reloads weights. Multi-replica is therefore a deliberate decision, not a default. See the cold-start demo.
- **Image pinning.** `vllm/vllm-openai:latest` is trust-on-first-use. Pin a digest for production.
- **Egress NetworkPolicy.** The vLLM pod can reach anything outbound, because it needs `huggingface.co`. Tighten this with an egress allow-list in production.
- **Logs and tracing.** `kubectl logs` is enough for the demo. Add Loki for aggregated logs and Tempo or Jaeger (`--otlp-traces-endpoint`) for traces when you build multi-step agents on top.

## Failure modes the demo exercises

The `demo/` scripts deliberately push the system past its limits to show how it degrades and recovers rather than crashes:

| Demo | Bottleneck | Lesson |
|---|---|---|
| `loadgen.py` | compute | continuous batching serves many concurrent users at near-flat latency |
| `kv_cache_pressure.py` | memory | KV cache fills before compute does; the engine queues instead of OOMing |
| `apply_bounded.sh` | admission | bounded admission (`--max-num-seqs`, `--max-model-len`) caps both failure modes |
| `cold_start.sh` | startup | the PVC turns a 10-minute re-download into a ~60-second restart |

See [demo/README.md](demo/README.md) for the run order and talk narration.

