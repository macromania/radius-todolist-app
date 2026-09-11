variable "context" {
  description = "Radius context for management or control PostgreSQL in its existing application namespace."
  type = object({
    application = object({ name = string })
    environment = object({ name = string })
    resource = object({
      id         = string
      properties = object({ databaseName = string })
    })
    runtime = object({ kubernetes = object({ namespace = string }) })
  })

  validation {
    condition = contains(["management", "shared-control", "isolated-1-control"], var.context.environment.name) && (
      var.context.application.name == (var.context.environment.name == "management" ? "management" : "control")
      ) && (
      var.context.runtime.kubernetes.namespace == "radplanes-local-${var.context.environment.name}-${var.context.application.name}"
      ) && can(regex(
        "^/planes/radius/local/resourcegroups/radplanes-local/providers/demo\\.platform/postgresqldatabases/[a-z][a-z0-9-]*$",
        lower(var.context.resource.id)
    ))
    error_message = "PostgreSQL must use an owned management/control slot, Radius group, and matching application namespace."
  }

  validation {
    condition     = can(regex("^[a-z][a-z0-9_]{0,62}$", var.context.resource.properties.databaseName))
    error_message = "databaseName must satisfy the unchanged PostgreSQL type schema."
  }
}

variable "node_address" {
  description = "The provider-verified kind node InternalIP, reachable privately from sibling clusters."
  type        = string

  validation {
    condition = can(cidrnetmask("${var.node_address}/32")) && can(regex(
      "^(10\\.|172\\.(1[6-9]|2[0-9]|3[01])\\.|192\\.168\\.)", var.node_address
    ))
    error_message = "node_address must be an RFC1918 IPv4 address, never a host loopback/public endpoint."
  }
}
