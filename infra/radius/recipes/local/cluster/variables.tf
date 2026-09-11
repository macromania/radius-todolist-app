variable "context" {
  description = "Radius-injected custom resource context for one of four reserved child slots."
  type = object({
    resource = object({
      id = string
      properties = object({
        environment = string
        slot        = string
      })
    })
  })

  validation {
    condition = contains([
      "shared-control", "shared-data", "isolated-1-control", "isolated-1-data"
    ], var.context.resource.properties.slot)
    error_message = "Only the four reserved local child slots are permitted."
  }
}

variable "images" {
  description = "Parent-inspected, immutable native application image references; empty preserves the gate."
  type        = list(string)
  default     = []

  validation {
    condition = length(var.images) <= 2 && length(distinct(var.images)) == length(var.images) && alltrue([
      for image in var.images : can(regex("^localhost/radplanes-plane-(api|provisioner):[0-9a-f]{40,64}$", image))
    ])
    error_message = "At most two distinct, source-tagged localhost/radplanes-plane-api or provisioner images are allowed."
  }
}
