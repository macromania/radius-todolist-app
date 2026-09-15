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

variable "runtime_images" {
  description = "Prepared API, provisioner, and operator images published by the management environment."
  type = map(object({
    reference = string
    image_id  = string
  }))

  validation {
    condition = toset(keys(var.runtime_images)) == toset(["api", "provisioner", "operator"]) && alltrue([
      for role, image in var.runtime_images :
      can(regex("^localhost/${var.resource_prefix}-${role}:[0-9a-f]{40}$", image.reference)) &&
      can(regex("^sha256:[0-9a-f]{64}$", image.image_id))
    ])
    error_message = "runtime_images must contain exactly the selected, source-tagged api, provisioner, and operator images and their Docker IDs."
  }
}

variable "dependency_images" {
  description = "Prepared kind node, Radius, and datastore images with inspected Docker IDs."
  type = list(object({
    reference = string
    image_id  = string
  }))

  validation {
    condition = length(var.dependency_images) >= 1 && length(var.dependency_images) <= 61 && (
      length(distinct([for image in var.dependency_images : image.reference])) == length(var.dependency_images)
      ) && alltrue([
        for image in var.dependency_images : can(regex("^sha256:[0-9a-f]{64}$", image.image_id)) && can(regex(
          "^(ghcr\\.io/radius-project|docker\\.io/(library|envoyproxy)|kindest)/[a-zA-Z0-9._/-]+(:[a-zA-Z0-9._-]+)?(@sha256:[0-9a-f]{64})?$",
          image.reference
        ))
    ])
    error_message = "Supply uniquely named, inspected public dependency images; management executor images are not child inputs."
  }
}

variable "resource_prefix" {
  description = "Selected project-deployment-local physical resource prefix."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,23}[a-z0-9]$", var.resource_prefix))
    error_message = "resource_prefix must be a validated lowercase deployment stem, at most 25 characters."
  }
}

variable "radius_group" {
  description = "Selected management Radius resource group."
  type        = string
  validation {
    condition = var.radius_group == var.resource_prefix && lower(var.context.resource.id) == (
      "/planes/radius/local/resourcegroups/${var.radius_group}/providers/demo.platform/clusters/${var.context.resource.properties.slot}"
    )
    error_message = "The cluster must belong to the selected Radius group and logical slot."
  }
}

variable "access_namespace" {
  description = "Management namespace that owns child access Secrets."
  type        = string
  validation {
    condition     = var.access_namespace == "${var.resource_prefix}-access"
    error_message = "access_namespace must belong to the selected deployment."
  }
}
