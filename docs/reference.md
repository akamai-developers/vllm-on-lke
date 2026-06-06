# Troubleshooting and command reference

This page lists common failure modes and the commands you use most often when you operate the vLLM endpoint on LKE.

## Troubleshooting

| Symptom | What to check or do |
|---|---|
| `terraform apply` fails on `helm_release.cloud_firewall_*` | The cluster API server may not have been ready when Helm tried to connect. Re-run `terraform apply`. The Helm provider is idempotent and retries. |
| Pod stuck in `Pending` | Run `kubectl -n llm describe pod`. The GPU node is missing the `pool=gpu` label, or the GPU Operator drivers are not ready yet (wait 2 to 3 minutes). |
| Pod in `CrashLoopBackOff` | Run `kubectl -n llm logs deploy/vllm`. Hugging Face download progress is expected, so wait. CUDA errors usually mean the GPU Operator has not bound the GPU yet, and the pod recovers on the next restart. |
| `401 Unauthorized` | Wrong bearer token, or a missing `Bearer ` prefix on the `Authorization` header. |
| The deploy step timed out | The model is still downloading. Tail the logs with `kubectl -n llm logs -f deploy/vllm`. |
| `kubectl get cloudfirewalls` shows nothing | The controller has not reconciled yet. Wait 30 seconds and re-check, or run `kubectl -n kube-system logs deploy/cloud-firewall-controller` for errors. |

## Command reference

### Endpoint and token

```bash
export VLLM_API_KEY=$(kubectl -n llm get secret vllm-secrets -o jsonpath='{.data.VLLM_API_KEY}' | base64 -d)
export ENDPOINT=http://$(kubectl -n llm get svc vllm -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
```

### Cluster and pod state

```bash
kubectl get nodes -L pool                                         # nodes with pool labels
kubectl -n llm get pod -l app=vllm                                # check whether vLLM is ready
kubectl -n llm logs -f deploy/vllm                                # tail vLLM logs
kubectl -n llm exec -it deploy/vllm -- nvidia-smi                 # GPU usage
kubectl -n llm exec -it deploy/vllm -- watch -n 1 nvidia-smi      # GPU usage, live
```

### vLLM config

```bash
# What the engine reports about itself: max_model_len and other fields
curl -s $ENDPOINT/v1/models -H "Authorization: Bearer $VLLM_API_KEY" | jq

# Live metrics: cache config, request stats, throughput counters
curl -s $ENDPOINT/metrics | grep -E "vllm:cache_config_info|num_requests|tokens_total"
```

### Monitoring

```bash
# Port-forwards: run in separate terminals
kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80
kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090:9090

# Check whether Prometheus picked up the PodMonitors
curl -s http://localhost:9090/api/v1/targets | jq -r '.data.activeTargets[].labels.job' | sort -u

# Force Prometheus to re-render scrape config after a PodMonitor change
kubectl -n monitoring rollout restart sts/prometheus-kube-prometheus-stack-prometheus

# Inspect the labels DCGM exposes, which helps when fixing dashboard variables
curl -s 'http://localhost:9090/api/v1/query?query=DCGM_FI_DEV_GPU_UTIL' | jq '.data.result[0].metric'
```

### Firewall verification

```bash
# List both firewalls and their attached devices
linode-cli firewalls list
linode-cli firewalls devices-list <FIREWALL_ID>

# Confirm the NodeBalancer firewall caught the NodeBalancer
FIREWALL_ID=$(cd terraform && terraform output -raw firewall_id)
linode-cli firewalls devices-list $FIREWALL_ID
```

For the full deploy and teardown walkthrough, see [deployment.md](deployment.md). For the monitoring stack and dashboards, see [monitoring.md](monitoring.md).

