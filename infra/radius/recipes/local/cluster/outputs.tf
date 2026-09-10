output "result" {
  description = "Public custom-resource properties only; credentials remain in protected state and a Secret."
  value = {
    values = {
      clusterId          = "kind://${kind_cluster.child.name}"
      clusterName        = kind_cluster.child.name
      bootstrapAccessRef = "kubernetes://radplanes-local-access/${kubernetes_secret_v1.access.metadata[0].name}#kubeconfig"
    }
  }
}
