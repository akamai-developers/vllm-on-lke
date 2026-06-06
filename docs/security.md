# Security model

This endpoint is protected by two layers of Linode Cloud Firewall plus in-cluster controls: bearer-token auth, a NetworkPolicy, and a hardened pod `securityContext`. For the per-component rationale, see [../ARCHITECTURE.md](../ARCHITECTURE.md).

## Controls in place

The following controls work together as defense in depth, from the public perimeter inward to the pod.

### NodeBalancer firewall (Terraform-managed)

Terraform creates a Linode Cloud Firewall and attaches it to the NodeBalancer, which is the public ingress. This firewall allows only TCP 80/443 inbound, from `allowed_cidr`.

The default `allowed_cidr` is open. To make the endpoint private to you, set it to your own IP in `terraform.tfvars` and re-apply.

```hcl
allowed_cidr = "203.0.113.10/32"
```

```bash
cd terraform
terraform apply
```

### Worker node firewall (cloud-firewall-controller)

A `LoadBalancer` Service opens a NodePort (30000-32768) on every worker node, and on Linode those node IPs are public. Without protection, the NodePort is reachable from the internet, which bypasses the NodeBalancer firewall.

Terraform installs the **cloud-firewall-controller**, which attaches a second Cloud Firewall to every worker node. This firewall drops all public traffic except cluster-internal and NodeBalancer ranges, closing the NodePort gap. The controller re-applies the firewall to recycled and new nodes automatically.

### Bearer token

vLLM runs with `--api-key`, so it rejects requests to `/v1/*` that do not carry an `Authorization: Bearer <token>` header. The token lives in the `vllm-secrets` Secret, and the same value serves both as vLLM's `--api-key` and as the bearer token clients send.

### NetworkPolicy

`manifests/networkpolicy.yaml` defines a default-deny ingress policy in the `llm` namespace, plus an allow rule that permits traffic to `vllm:8000`. This is defense in depth behind the firewalls.

### Pod securityContext

The vLLM pod runs with `allowPrivilegeEscalation: false`, all Linux capabilities dropped, and `seccompProfile: RuntimeDefault`. The container still runs as root because CUDA requires it, but it cannot escalate privileges or use unusual syscalls.

## Controls deferred by design

This is a demo, so the following controls are deferred. Each entry notes the production path.

- **TLS.** The endpoint is HTTP, so the bearer token travels in the clear over the network. For production, terminate TLS at an Ingress. cert-manager with Let's Encrypt is the usual path.
- **Per-user auth and rate limiting.** The bearer token is a shared secret, not an identity. For internal-tool deployments, layer `oauth2-proxy` and an Ingress on top.
- **Image pinning and scanning.** `vllm/vllm-openai:latest` is trust-on-first-use. Pin a digest for production.
- **Egress NetworkPolicy.** The vLLM pod can reach any outbound address because it needs to reach `huggingface.co`. Tighten this with an egress allow-list in production.

## Attach the NodeBalancer firewall manually

!!! note
    Some Linode CCM versions ignore the post-hoc firewall annotation. If the firewall is not attached after deployment, attach it manually.

```bash
FIREWALL_ID=$(cd terraform && terraform output -raw firewall_id)
LB_IP=$(kubectl -n llm get svc vllm -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
NB_ID=$(linode-cli nodebalancers list --json | jq -r ".[] | select(.ipv4 == \"$LB_IP\") | .id")
linode-cli firewalls device-create $FIREWALL_ID --type nodebalancer --id $NB_ID
```

