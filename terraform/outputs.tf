output "cluster_id" {
  description = "LKE cluster ID."
  value       = linode_lke_cluster.main.id
}

output "cluster_endpoints" {
  description = "Kubernetes API endpoints."
  value       = linode_lke_cluster.main.api_endpoints
}

output "kubeconfig" {
  description = "Base64-encoded kubeconfig. Decode with `terraform output -raw kubeconfig | base64 -d`."
  value       = linode_lke_cluster.main.kubeconfig
  sensitive   = true
}

output "firewall_id" {
  description = "Cloud Firewall ID — attach to the NodeBalancer via Service annotation."
  value       = linode_firewall.vllm.id
}
