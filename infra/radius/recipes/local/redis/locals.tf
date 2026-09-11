locals {
  namespace = var.context.runtime.kubernetes.namespace
  host      = "redis.${local.namespace}.svc.cluster.local"
  password  = replace(urlencode(random_password.server.result), "+", "%20")
  image     = "docker.io/library/redis:7.4-alpine@sha256:ff02b58f971e7d7d156a1267e283fcbbeee91773b6aa36c49dac28ecfe28eadf"
  labels = {
    "radplanes.local/component" = "redis"
    "radplanes.local/slot"      = var.context.environment.name
  }
  annotations = {
    "radplanes.local/radius-resource" = var.context.resource.id
  }
}
