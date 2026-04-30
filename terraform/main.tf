# Cloud infra for the LKE GPU LLM quickstart.
#
# What this provisions:
#   - An LKE cluster with a small CPU node pool for system workloads.
#   - A separate GPU node pool, labeled `pool=gpu` so the workload's nodeSelector matches.
#   - A Linode Cloud Firewall to restrict ingress to the NodeBalancer (ports 80/443).
#   - The cloud-firewall-controller (via helm), which firewalls the worker nodes
#     themselves so their public IPs aren't reachable on NodePort etc.
#
# What this does NOT provision:
#   - In-cluster workload resources (namespace, PVC, Deployment, Service, Secret).
#     Those live in ../manifests/ and are applied with kubectl after the cluster is up.
#   - The NodeBalancer itself. It's created by the Linode Cloud Controller Manager
#     when the LoadBalancer Service is applied.

provider "linode" {
  # Auth: set LINODE_TOKEN in your environment.
}

# Pull the cluster's kubeconfig for use by the helm provider below.
locals {
  kubeconfig = yamldecode(base64decode(linode_lke_cluster.main.kubeconfig))
}

# Helm provider talks to the LKE cluster's API server using the kubeconfig
# extracted from the LKE resource above. Only used to install bootstrap
# operators (security-critical infra), not workloads.
provider "helm" {
  kubernetes {
    host                   = local.kubeconfig.clusters[0].cluster.server
    cluster_ca_certificate = base64decode(local.kubeconfig.clusters[0].cluster["certificate-authority-data"])
    token                  = local.kubeconfig.users[0].user.token
  }
}

resource "linode_lke_cluster" "main" {
  label       = var.cluster_label
  region      = var.region
  k8s_version = var.k8s_version

  # System pool. vLLM does NOT land here (no GPU). Keeping it small and cheap
  # is intentional — gpu-operator daemonsets and any non-GPU workloads run here.
  pool {
    type  = var.cpu_node_type
    count = var.cpu_node_count
  }
}

# GPU pool, defined separately so it can be scaled, replaced, or labeled
# without touching the cluster resource.
resource "linode_lke_node_pool" "gpu" {
  cluster_id = linode_lke_cluster.main.id
  type       = var.gpu_node_type
  node_count = var.gpu_node_count

  # Kubernetes node label propagated from the Linode side.
  # The vLLM Deployment uses `nodeSelector: pool=gpu` to schedule onto these nodes.
  labels = {
    pool = "gpu"
  }
}

# Cloud Firewall — only allows ports 80 and 443 inbound. Drops everything else.
# Attach to the NodeBalancer after `kubectl apply` via the Service annotation
# `service.beta.kubernetes.io/linode-loadbalancer-firewall-id` (see README step 7).
#
# To restrict by source IP, set `allowed_cidr` in terraform.tfvars (default 0.0.0.0/0).
resource "linode_firewall" "vllm" {
  label           = "${var.cluster_label}-vllm"
  inbound_policy  = "DROP"
  outbound_policy = "ACCEPT"

  inbound {
    label    = "allow-http"
    action   = "ACCEPT"
    protocol = "TCP"
    ports    = "80"
    ipv4     = [var.allowed_cidr]
  }

  inbound {
    label    = "allow-https"
    action   = "ACCEPT"
    protocol = "TCP"
    ports    = "443"
    ipv4     = [var.allowed_cidr]
  }
}

# Cloud Firewall Controller — installs a Linode Cloud Firewall on every worker
# node with the LKE-recommended ruleset (allows control-plane + NodeBalancer
# traffic, drops everything else, including the public-facing NodePort range).
# Without this, the worker nodes' public IPs expose 30000-32768 on the internet.
#
# CRDs first, then the controller — the controller's CRD must exist before its
# Deployment starts.
resource "helm_release" "cloud_firewall_crd" {
  name             = "cloud-firewall-crd"
  repository       = "https://linode.github.io/cloud-firewall-controller"
  chart            = "cloud-firewall-crd"
  namespace        = "kube-system"
  create_namespace = false
  wait             = true
  timeout          = 300

  depends_on = [linode_lke_cluster.main, linode_lke_node_pool.gpu]
}

resource "helm_release" "cloud_firewall_controller" {
  name             = "cloud-firewall"
  repository       = "https://linode.github.io/cloud-firewall-controller"
  chart            = "cloud-firewall-controller"
  namespace        = "kube-system"
  create_namespace = false
  wait             = true
  timeout          = 300

  depends_on = [helm_release.cloud_firewall_crd]
}
