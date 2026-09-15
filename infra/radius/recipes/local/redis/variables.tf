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
      var.context.runtime.kubernetes.namespace == "${var.resource_prefix}-${var.context.environment.name}-data"
      ) && can(regex(
        "^/planes/radius/local/resourcegroups/${var.radius_group}/providers/applications\\.datastores/rediscaches/[a-z][a-z0-9-]*$",
        lower(var.context.resource.id)
    ))
    error_message = "Redis must use an owned data slot, Radius group, and matching application namespace."
  }
}

variable "resource_prefix" {
  description = "Selected project-deployment-local physical resource prefix."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,23}[a-z0-9]$", var.resource_prefix))
    error_message = "resource_prefix must be a validated lowercase deployment stem."
  }
}

variable "radius_group" {
  description = "Radius group that owns Redis."
  type        = string
  validation {
    condition     = var.radius_group == var.resource_prefix
    error_message = "radius_group must match the selected deployment."
  }
}
