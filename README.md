# Serve an LLM on GPU with LKE

Deploy an OpenAI-compatible LLM endpoint on a GPU in Linode Kubernetes Engine (LKE). The stack uses Terraform and LKE to run vLLM serving `Qwen/Qwen2.5-7B-Instruct` on one NVIDIA RTX 4000 Ada GPU, behind bearer-token auth and two Linode Cloud Firewalls.

A single `terraform apply` brings up the cluster and the worker-node firewall together, so the cluster is never exposed.

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
   │  Cloud Firewall #2: attached to every worker node            │
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
  Terraform     -> cluster, node pools, both firewalls, cloud-firewall-controller
  kubectl/helm  -> GPU operator, vLLM workload, monitoring stack
```

The two firewalls are intentional. Firewall #1 guards the NodeBalancer as public ingress, and Firewall #2 guards each worker node's otherwise-public NodePort range. See [ARCHITECTURE.md](ARCHITECTURE.md) for the per-component rationale.

!!! warning
    The GPU node is [billed hourly](https://www.akamai.com/cloud/pricing). Tear it down when you are done: run `./deploy.sh destroy` or follow [Clean up](docs/deployment.md#clean-up).

## Prerequisites

You need an account and a set of command-line tools before you begin.

**Account:**

- Create an [Akamai Cloud account](http://login.linode.com/signup?promo=akm-dev-git-300-31126-M055) with an API token. The account includes a $300 credit.

**Tools:**

| Tool | Version | Purpose |
|---|---|---|
| `terraform` | >= 1.5 | Provisions the cluster, node pools, and firewalls. |
| `kubectl` | any | Applies workload manifests, inspects pods, port-forwards. |
| `helm` | any | Installs the GPU Operator and the monitoring stack. |
| `linode-cli` | any | Attaches the NodeBalancer firewall and verifies firewall state. |
| `openssl` | any | Generates the random bearer token. |
| `jq` | any | Parses JSON output from `kubectl` and `linode-cli`. |

!!! note
    The Python examples and demo need Python 3.10+ with `openai` and `httpx`. Install these only if you run those clients.

## Quick start

`./deploy.sh` drives the whole lifecycle: Terraform, kubeconfig, GPU Operator, secret, manifests, the NodeBalancer firewall, and the wait for the model to load. It writes `ENDPOINT` and `VLLM_API_KEY` into `.env` for you.

```bash
cp .env.examples .env          # set LINODE_TOKEN and a unique CLUSTER_LABEL
./deploy.sh                    # stand everything up (~20 min; first run pulls ~15GB)
./deploy.sh chatbot            # install deps and run the Strands chatbot locally
./deploy.sh destroy            # tear it all down in the correct order
```

`CLUSTER_LABEL` must be unique in your Linode account. It names the LKE cluster and the `<label>-vllm` Cloud Firewall, both of which are account-global. `deploy.sh` pre-flights the name and refuses to start if it is already taken, so you can stand up a second stack by giving it a different `CLUSTER_LABEL`.

`./deploy.sh env` re-pulls `ENDPOINT` and `VLLM_API_KEY` from a running cluster into `.env` without redeploying. The script is idempotent: re-running reuses the existing `vllm-secrets` token instead of rotating it.

To run each step by hand or to debug, see [docs/deployment.md](docs/deployment.md).

## Verify it works

After `deploy.sh` writes `.env`, source it and send a chat completion. `ENDPOINT` already includes the scheme, so clients build `"$ENDPOINT/v1/..."` directly.

```bash
source .env

curl -s "$ENDPOINT/v1/chat/completions" \
  -H "Authorization: Bearer $VLLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen2.5-7B-Instruct",
    "messages": [{"role": "user", "content": "What is agentic AI?"}]
  }' | jq .
```

The endpoint is OpenAI-compatible and has tool/function calling enabled (`--enable-auto-tool-choice --tool-call-parser hermes`), so agent frameworks work against it.

## Next steps

- [docs/deployment.md](docs/deployment.md): the manual step-by-step walkthrough and teardown.
- [docs/monitoring.md](docs/monitoring.md): Prometheus and Grafana for vLLM and GPU metrics.
- [docs/security.md](docs/security.md): the security model behind the two firewalls and in-cluster controls.
- [docs/reference.md](docs/reference.md): troubleshooting and the command cheat sheet.
- [demo/README.md](demo/README.md): GPU serving failure-mode demos.
- [examples/chatbot/README.md](examples/chatbot/README.md): a Strands tool-using chatbot with a Streamlit UI.
- [examples/openai-client.py](examples/openai-client.py): a single request with the full response.
- [examples/openai_streaming_client.py](examples/openai_streaming_client.py): streams token by token and reports TTFT and throughput.
- [ARCHITECTURE.md](ARCHITECTURE.md): design rationale for every component.

## About the Author

> This project was created by **Du'An Lightfoot**, a developer passionate about AI agents, cloud infrastructure, and teaching in public.
>
> Learn more and connect:
>
> - 🌐 Website: [duanlightfoot.com](https://duanlightfoot.com)
> - 📺 YouTube: [@LabEveryday](https://www.youtube.com/@LabEveryday)
> - 🐙 GitHub: [@labeveryday](https://github.com/labeveryday)

