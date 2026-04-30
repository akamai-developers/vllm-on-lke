variable "cluster_label" {
  description = "Label for the LKE cluster."
  type        = string
  default     = "lke-gpu-demo"
}

variable "region" {
  description = "Linode region. Must support GPU plans — check `linode-cli regions list`."
  type        = string
  default     = "us-sea"
}

variable "k8s_version" {
  # LKE rotates supported versions over time. Verify the default is still
  # valid: `linode-cli lke versions-list`.
  description = "Kubernetes version supported by LKE."
  type        = string
  default     = "1.35"
}

variable "cpu_node_type" {
  description = "Linode plan for the CPU node pool (system workloads, gpu-operator daemonsets)."
  type        = string
  default     = "g6-standard-2"
}

variable "cpu_node_count" {
  description = "Number of CPU nodes."
  type        = number
  default     = 2
}

variable "gpu_node_type" {
  # 1x RTX 4000 Ada (24GB VRAM) tiers (see `linode-cli linodes types | grep gpu`):
  #   g2-gpu-rtx4000a1-s   4 vCPU / 16 GB  / $0.52/hr  (too small — vLLM requests 24Gi memory)
  #   g2-gpu-rtx4000a1-m   8 vCPU / 32 GB  / $0.67/hr  ← default, fits the workload
  #   g2-gpu-rtx4000a1-l  16 vCPU / 64 GB  / $0.96/hr
  description = "Linode plan for the GPU node pool."
  type        = string
  default     = "g2-gpu-rtx4000a1-m"
}

variable "gpu_node_count" {
  description = "Number of GPU nodes."
  type        = number
  default     = 1
}

# Used only if the optional Cloud Firewall block in main.tf is uncommented.
variable "allowed_cidr" {
  description = "CIDR allowed to reach the NodeBalancer when the firewall is enabled."
  type        = string
  default     = "0.0.0.0/0"
}
