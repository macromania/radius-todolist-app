variable "context" {
  description = "Radius-injected custom resource context; this gate admits one fixed child slot."
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
    condition     = var.context.resource.properties.slot == "shared-control"
    error_message = "This feasibility Recipe admits only shared-control, not a full tenant topology."
  }
}
