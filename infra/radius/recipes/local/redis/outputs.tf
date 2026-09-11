output "result" {
  description = "Built-in lowercase redis connection contract; encode the password exactly once."
  sensitive   = true
  value = {
    values = {
      host     = local.host
      port     = 6379
      username = ""
      tls      = false
    }
    secrets = {
      password = local.password
      url      = "redis://:${local.password}@${local.host}:6379"
    }
  }
}
