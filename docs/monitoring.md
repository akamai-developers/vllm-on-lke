# Monitor vLLM and the GPU

Install Prometheus and Grafana, scrape the vLLM and GPU metrics, and watch GPU utilization, time to first token (TTFT), and KV cache usage in real time.

## Objectives

In this lab you:

- Install Prometheus and Grafana with the `kube-prometheus-stack` Helm chart.
- Scrape the vLLM `/metrics` endpoint and the GPU DCGM exporter.
- Import the DCGM and vLLM dashboards and watch GPU utilization, TTFT, and KV cache usage in real time.

Monitoring is optional. The failure-mode demos are easier to read with it in place. See [../demo/README.md](../demo/README.md) for those demos.

## Before you begin

You need:

- A running deployment. Complete [deployment.md](deployment.md) first.
- `helm` installed locally.

Set `KUBECONFIG` so `kubectl` and `helm` target your cluster:

```bash
export KUBECONFIG=$PWD/kubeconfig
```

## Install the stack

Add the `prometheus-community` Helm repository and install `kube-prometheus-stack` with the values file in this repository:

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

helm install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace \
  -f manifests/monitoring/kube-prometheus-stack-values.yaml \
  --wait --timeout 10m
```

Apply the `PodMonitor` resources that tell Prometheus what to scrape (vLLM `/metrics` and DCGM `/metrics`):

```bash
kubectl apply -f manifests/monitoring/podmonitors.yaml
```

!!! note
    Both Prometheus and Grafana use `ClusterIP` services. You reach them through `kubectl port-forward`, which tunnels over the existing Kubernetes API server connection. Monitoring adds no public IP, no new firewall, and no extra cost.

## Open Grafana and Prometheus

Open Grafana. In one terminal, run:

```bash
kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80
```

Open `http://localhost:3000` and log in with `admin` / `prom-operator`.

Open Prometheus. In a second terminal, run:

```bash
kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090:9090
```

Open `http://localhost:9090`. Go to **Status > Target health** to see what Prometheus is scraping.

## Import dashboards

The chart pre-loads an **NVIDIA DCGM Exporter** dashboard. The `gnetId` in the values file can fall out of sync with current Grafana.com revisions, so importing the current dashboard is the reliable path. If panels are empty, import dashboard `15117`:

1. In Grafana, go to **Dashboards > New > Import**.
2. Paste `15117` (NVIDIA's current DCGM Exporter Dashboard) and select **Load**.
3. Set the datasource to **Prometheus** and select **Import**.

Then import the vLLM dashboard. The Grafana.com ID changes between releases, so point at vLLM's source-of-truth JSON:

1. Go to **Dashboards > New > Import**.
2. Paste the URL `https://raw.githubusercontent.com/vllm-project/vllm/main/examples/production_monitoring/grafana.json`.
3. Set the datasource to **Prometheus** and select **Import**.

## Verify scraping

In the Prometheus UI at `http://localhost:9090/graph`, enter this query:

```promql
DCGM_FI_DEV_GPU_UTIL
```

A returned value means GPU metrics are flowing. An empty result means you need to troubleshoot below.

## Troubleshooting

### "No data" on the DCGM dashboard variable dropdowns

The `instance` and `gpu` dropdowns show no data because dashboard `12239` filters by `job="dcgm-exporter"`, but the `PodMonitor` generates `job="monitoring/nvidia-dcgm-exporter"`. Use dashboard `15117`, which has more permissive variable queries, or edit the existing dashboard's variables to drop the job filter.

### Prometheus has zero DCGM targets

At `http://localhost:9090/targets`, nothing shows for DCGM because the default DCGM port name from the GPU operator is `metrics`, not `gpu-metrics`. Check the actual port name:

```bash
DCGM_POD=$(kubectl -n gpu-operator get pods -l app=nvidia-dcgm-exporter -o name | head -1)
kubectl -n gpu-operator get $DCGM_POD -o jsonpath='{.spec.containers[*].ports}' | jq
```

If the port is `metrics` instead of `gpu-metrics`, edit `manifests/monitoring/podmonitors.yaml` to match, then re-apply:

```bash
kubectl apply -f manifests/monitoring/podmonitors.yaml
```

### Dashboard panels lag terminal output by about 15 seconds

This is the default scrape interval. For the demo, set Grafana auto-refresh to 5s (top-right dropdown) and lower the `PodMonitor` `interval` to `5s`.

### Logs (deferred)

`kubectl -n llm logs -f deploy/vllm` covers the demo. For aggregated logs in the same Grafana, install [Loki](https://grafana.com/oss/loki/), which adds a `Loki` datasource and a Logs Explorer view. That is one additional `helm install` and is not part of this quickstart.

### Tracing (deferred)

vLLM supports OpenTelemetry traces through `--otlp-traces-endpoint`, pointing at [Tempo](https://grafana.com/oss/tempo/) (Grafana-native) or Jaeger. Add the flag to `manifests/vllm-deployment.yaml` and restart the pod. Tracing applies when you build multi-step agents on this endpoint and is not required for raw chat completions.

## What's next

- Run the failure-mode demos in [../demo/README.md](../demo/README.md), which read the dashboards you just imported.
- See [reference.md](reference.md) for the monitoring cheat sheet and general troubleshooting.

