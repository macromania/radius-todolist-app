variable "context" {
  description = "Radius gateway context; Azure challenge/HTTPS fields do not change this local HTTP implementation."
  type = object({
    application = object({ name = string })
    environment = object({ name = string })
    resource = object({
      id = string
      properties = object({
        apiService = string
        apiPort    = number
      })
    })
    runtime = object({ kubernetes = object({ namespace = string }) })
  })

  validation {
    condition = contains([
      "management", "shared-control", "shared-data", "isolated-1-control", "isolated-1-data"
      ], var.context.environment.name) && (
      var.context.application.name == (var.context.environment.name == "management" ? "management" : endswith(var.context.environment.name, "-control") ? "control" : "data")
      ) && (
      var.context.runtime.kubernetes.namespace == "radplanes-local-${var.context.environment.name}-${var.context.application.name}"
      ) && can(regex(
        "^/planes/radius/local/resourcegroups/radplanes-local/providers/demo\\.platform/gateways/[a-z][a-z0-9-]*$",
        lower(var.context.resource.id)
    ))
    error_message = "Gateway must use an owned slot, Radius group, and matching application namespace."
  }

  validation {
    condition = can(regex("^[a-z][a-z0-9-]{0,62}$", var.context.resource.properties.apiService)) && (
      var.context.resource.properties.apiPort >= 1 && var.context.resource.properties.apiPort <= 65535 &&
      floor(var.context.resource.properties.apiPort) == var.context.resource.properties.apiPort
    )
    error_message = "apiService and apiPort must satisfy the unchanged gateway type schema."
  }
}

variable "gateway_host_port" {
  description = "The slot's pre-reserved loopback host mapping; this Recipe creates no host binding."
  type        = number

  validation {
    condition = var.gateway_host_port == lookup({
      management         = 35490, shared-control = 35491, shared-data = 35492,
      isolated-1-control = 35493, isolated-1-data = 35494
    }, var.context.environment.name, -1)
    error_message = "gateway_host_port must be the exact slot reservation in 35490-35494."
  }
}
