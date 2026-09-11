variable "context" {
  description = "Radius context for a data plane's built-in Redis resource."
  type = object({
    application = object({ name = string })
    environment = object({ name = string })
    resource    = object({ id = string })
    runtime     = object({ kubernetes = object({ namespace = string }) })
  })

  validation {
    condition = contains(["shared-data", "isolated-1-data"], var.context.environment.name) && (
      var.context.application.name == "data"
      ) && (
      var.context.runtime.kubernetes.namespace == "radplanes-local-${var.context.environment.name}-data"
      ) && can(regex(
        "^/planes/radius/local/resourceGroups/radplanes-local/providers/Applications\\.Datastores/redisCaches/[a-z][a-z0-9-]*$",
        var.context.resource.id
    ))
    error_message = "Redis must use an owned data slot, Radius group, and matching application namespace."
  }
}
