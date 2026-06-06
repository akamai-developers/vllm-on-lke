# Deploy vLLM on LKE step by step

This lab walks you through standing up an OpenAI-compatible vLLM endpoint on a GPU in Linode Kubernetes Engine (LKE) by hand. It serves `Qwen/Qwen2.5-7B-Instruct` on a single NVIDIA RTX 4000 Ada GPU behind bearer-token auth and two layers of Linode Cloud Firewall.

## Objectives

You provision the LKE cluster and both Cloud Firewalls with Terraform, install the NVIDIA GPU Operator, create the Secret that holds the bearer token, deploy vLLM, attach the NodeBalancer firewall, and test the endpoint. By the end you have a live, OpenAI-compatible endpoint with tool calling enabled.

This is the same sequence that `./deploy.sh` runs for you. Running it by hand shows you each piece and where to debug when a step fails. The numbered steps below are canonical: `deploy.sh` runs exactly them, in order.

## Before you begin

Review the [prerequisites in the README](../README.md#prerequisites) and confirm you have an Akamai Cloud account with an API token. You need `terraform`, `kubectl`, `helm`, `linode-cli`, `openssl`, and `jq` installed locally.

You set `KUBECONFIG` to the kubeconfig you pull in [step 3](#3-pull-kubeconfig). Every `kubectl` and `helm` command after that point reads it.

!!! warning
    The GPU node is billed hourly. Tear the stack down when you finish: see [Clean up](#clean-up).

## 1. Set your Linode API token

Export your token into the environment:

```bash
export LINODE_TOKEN=<your-token>
```

The Terraform provider reads the token from this environment variable. Do not put it in `tfvars`.

## 2. Provision the cluster and the worker node firewall

Move into the Terraform directory, create a `terraform.tfvars` from the example, name the stack, and apply:

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars   # edit if you want different region or instance types

# Name the stack. cluster_label must be UNIQUE in your Linode account: it names
# the LKE cluster and the "<label>-vllm" firewall. (deploy.sh reads this from
# CLUSTER_LABEL in .env; on the manual path, set it here.)
export TF_VAR_cluster_label="my-vllm-stack"

terraform init
terraform plan                                  # review what is about to be created
terraform apply
```

`cluster_label` must be unique within your Linode account, because it names both the LKE cluster and the `<label>-vllm` Cloud Firewall. To run a second stack alongside an existing one, give it a different label.

This single `terraform apply` provisions:

- The LKE cluster, a CPU pool, and a GPU pool (labeled `pool=gpu`).
- A Linode Cloud Firewall for the NodeBalancer, which allows TCP 80/443 from `allowed_cidr`.
- The **cloud-firewall-controller** (installed via Helm), which creates a second Cloud Firewall and attaches it to every worker node, closing the otherwise-open NodePort range on the nodes' public IPs.

By the time `terraform apply` returns, the cluster's perimeter is locked down.

## 3. Pull kubeconfig

Still in the `terraform/` directory, write the kubeconfig and point `KUBECONFIG` at it:

```bash
terraform output -raw kubeconfig | base64 -d > ../kubeconfig
cd ..
export KUBECONFIG=$PWD/kubeconfig
kubectl get nodes
```

You see the CPU and GPU nodes listed. Verify the firewall controller is healthy and the per-node Cloud Firewall is in place:

```bash
kubectl -n kube-system get pods -l app.kubernetes.io/name=cloud-firewall-controller
kubectl get cloudfirewalls -A
linode-cli firewalls list
```

You see **two** Cloud Firewalls in your Linode account:

- `lke-<cluster-id>`, created by the controller and attached to all 3 worker nodes.
- `<your-cluster-label>-vllm`, the name you chose (`CLUSTER_LABEL` in `.env`, or `TF_VAR_cluster_label` on the manual path). Terraform creates it, and it attaches to the NodeBalancer once the vLLM Service exists in [step 6](#6-deploy-vllm).

## 4. Install the NVIDIA GPU Operator

The GPU Operator installs NVIDIA drivers on the GPU node and exposes the GPU to Kubernetes as a schedulable resource (`nvidia.com/gpu`). Without it, your vLLM pod cannot claim the GPU.

```bash
helm repo add nvidia https://helm.ngc.nvidia.com/nvidia
helm repo update
helm install gpu-operator nvidia/gpu-operator \
  --namespace gpu-operator --create-namespace \
  --wait --timeout 10m
```

Driver install takes 2 to 3 minutes. Watch progress and press Ctrl-C when everything is `Running` or `Completed`:

```bash
kubectl -n gpu-operator get pods -w   # ctrl-c when everything is Running or Completed
```

Confirm the GPU node has the `pool=gpu` label and that the scheduler can see the GPU:

```bash
kubectl get nodes -L pool
kubectl get nodes -o json | jq '.items[].status.allocatable | select(."nvidia.com/gpu")'
```

You see `pool=gpu` on the GPU node, and `"nvidia.com/gpu": "1"` in the allocatable output.

!!! note
    Some Linode provider versions skip propagation of the `pool=gpu` label. If it is missing, apply it manually:

        kubectl label node <gpu-node-name> pool=gpu

## 5. Create the namespace and secrets

Create the `llm` namespace where the workload lives:

```bash
kubectl apply -f manifests/namespace.yaml
kubectl -n llm get all       # the namespace is empty until you deploy the workload
```

Generate a random API key and store it, along with a placeholder for the Hugging Face token, in a Kubernetes Secret. The vLLM Deployment reads this Secret via `envFrom` and uses `VLLM_API_KEY` as the bearer token that clients must send to call `/v1/*`:

```bash
VLLM_API_KEY=$(openssl rand -hex 32)
kubectl -n llm create secret generic vllm-secrets \
  --from-literal=VLLM_API_KEY=$VLLM_API_KEY \
  --from-literal=HUGGING_FACE_HUB_TOKEN=

echo "Save this: export VLLM_API_KEY=$VLLM_API_KEY"
```

`Qwen/Qwen2.5-7B-Instruct` (the default) is ungated, so an empty `HUGGING_FACE_HUB_TOKEN` is fine. For gated models (Llama, Mistral), pass your Hugging Face token instead of leaving it empty: `--from-literal=HUGGING_FACE_HUB_TOKEN=hf_yourtoken`.

## 6. Deploy vLLM

Apply the workload manifests, then attach the NodeBalancer Cloud Firewall to the Service:

```bash
kubectl apply -f manifests/

# Attach the NodeBalancer Cloud Firewall (created by Terraform) to the Service.
# Some CCM versions do not honor this annotation post-hoc; if the firewall
# is not attached after this, fall back to the linode-cli command in the
# Security section below.
FIREWALL_ID=$(cd terraform && terraform output -raw firewall_id)
kubectl -n llm annotate svc vllm \
  service.beta.kubernetes.io/linode-loadbalancer-firewall-id=$FIREWALL_ID --overwrite
```

Verify the NodeBalancer was added to the firewall:

```bash
linode-cli firewalls devices-list $FIREWALL_ID
```

!!! note
    Some Cloud Controller Manager (CCM) versions ignore the post-hoc annotation, so the NodeBalancer may not show in the devices list. Attach it manually with the following commands. The same fallback is documented in [security.md](security.md).

If the device list is empty, attach the NodeBalancer to the firewall directly:

```bash
LB_IP=$(kubectl -n llm get svc vllm -o jsonpath='{.status.loadBalancer.ingress[0].ip}')

NB_ID=$(linode-cli nodebalancers list --json | jq -r ".[] | select(.ipv4 == \"$LB_IP\") | .id")

linode-cli firewalls device-create $FIREWALL_ID --type nodebalancer --id $NB_ID
```

Wait for the model to load:

```bash
kubectl -n llm wait --for=condition=ready pod -l app=vllm --timeout=15m
```

!!! note
    The first run downloads ~15 GB of model weights from Hugging Face, which takes 5 to 10 minutes. Tail the logs to watch progress:

        kubectl -n llm logs -f deploy/vllm

## 7. Test the endpoint

Pull the bearer token straight from the Secret and the IP from the Service, so you do not copy-paste from earlier output. Set `ENDPOINT` to include the scheme (`http://`):

```bash
export VLLM_API_KEY=$(kubectl -n llm get secret vllm-secrets -o jsonpath='{.data.VLLM_API_KEY}' | base64 -d)
export ENDPOINT=http://$(kubectl -n llm get svc vllm -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
```

Send a chat completion. `ENDPOINT` already includes the scheme, so do not prepend `http://` again:

```bash
curl -s "$ENDPOINT/v1/chat/completions" \
  -H "Authorization: Bearer $VLLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen2.5-7B-Instruct",
    "messages": [{"role": "user", "content": "What is agentic AI?"}]
  }' | jq .
```

Your endpoint is live and OpenAI-compatible. The Deployment also enables OpenAI-style tool and function calling (`--enable-auto-tool-choice --tool-call-parser hermes`), so agent frameworks work against it as-is. The following examples are ready to run:

- [`examples/openai-client.py`](../examples/openai-client.py): sends a single request and prints the full response. This is the simplest client.
- [`examples/openai_streaming_client.py`](../examples/openai_streaming_client.py): streams the response token by token and reports TTFT, total elapsed, and tok/s.
- [`examples/chatbot/`](../examples/chatbot/README.md): a Streamlit chatbot built on a [Strands](https://strandsagents.com/) agent with tools. You see each tool call (name, args, result) inline.

## Clean up

!!! warning
    Delete the CloudFirewall custom resources before `terraform destroy`. `terraform destroy` uninstalls the cloud-firewall-controller, and if the controller is gone before its CloudFirewall CRs are deleted, it cannot clean up its Linode-side firewall, which then leaks and keeps billing.

The GPU node bills hourly, so teardown matters. Run the commands in this order:

```bash
# Workload first
kubectl delete -f manifests/ --ignore-not-found

# Monitoring stack (if installed in monitoring.md)
kubectl delete -f manifests/monitoring/podmonitors.yaml --ignore-not-found
helm uninstall kube-prometheus-stack -n monitoring 2>/dev/null
kubectl delete namespace monitoring --ignore-not-found

# Delete CloudFirewall CRs so the controller cleans up its Linode-side firewall
# BEFORE terraform destroy uninstalls the controller
kubectl delete cloudfirewalls --all --ignore-not-found

# GPU operator (still a manual helm release)
helm uninstall gpu-operator -n gpu-operator

# Cluster, NodeBalancer firewall, and cloud-firewall-controller (all Terraform-managed)
cd terraform && terraform destroy
```

Confirm in the Linode console that the cluster, NodeBalancer, block volume, and **both** Cloud Firewalls (NodeBalancer-side and node-side) are gone.

## What's next

- Set up Prometheus and Grafana to watch GPU utilization, TTFT, and KV cache usage: see [monitoring.md](monitoring.md).
- Review the security model (two Cloud Firewalls, bearer token, NetworkPolicy, pod securityContext) and the manual firewall-attach fallback: see [security.md](security.md).
- Run the failure-mode demos (load generator, KV cache pressure, bounded admission, cold start): see [demo/README.md](../demo/README.md).
- Build an agent on the endpoint: see [examples/chatbot/README.md](../examples/chatbot/README.md).

