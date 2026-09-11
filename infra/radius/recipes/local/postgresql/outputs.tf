output "result" {
  description = "PostgreSQL values follow the shared custom type; only the setup password is secret."
  sensitive   = true
  value = {
    values = {
      host            = var.node_address
      port            = 31543
      database        = local.database
      username        = local.username
      tlsRequired     = false
      serverId        = "kubernetes://${local.namespace}/statefulsets/postgres"
      setupSecretName = kubernetes_secret_v1.setup.metadata[0].name
    }
    secrets = {
      password = random_password.server.result
    }
  }
}
