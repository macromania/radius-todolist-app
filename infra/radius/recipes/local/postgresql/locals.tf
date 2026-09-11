locals {
  database  = var.context.resource.properties.databaseName
  namespace = var.context.runtime.kubernetes.namespace
  username  = "plane_setup"
  image     = "docker.io/library/postgres:17.8-alpine3.23@sha256:3430fe182f5065a6ea505c3d432d2c7fff18fbab954df8f277c1dbf4c70124af"
  labels = {
    "radplanes.local/component" = "postgres"
    "radplanes.local/slot"      = var.context.environment.name
  }
  annotations = {
    "radplanes.local/radius-resource" = var.context.resource.id
  }
}
