output "result" {
  description = "Loopback HTTP gateway and Kubernetes references; never synthetic Azure IDs or certificates."
  value = {
    values = {
      host              = "127.0.0.1"
      url               = "http://127.0.0.1:${var.gateway_host_port}"
      gatewayId         = "kubernetes://${local.namespace}/services/gateway"
      apiBackendService = kubernetes_service_v1.backend.metadata[0].name
    }
  }
}
