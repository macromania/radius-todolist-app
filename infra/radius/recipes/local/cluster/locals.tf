locals {
  prepared_images = concat(
    [for role in ["api", "provisioner", "operator"] : var.runtime_images[role]],
    var.dependency_images
  )
  slots = {
    shared-control     = { api = 35496, gateway = 35491 }
    shared-data        = { api = 35497, gateway = 35492 }
    isolated-1-control = { api = 35498, gateway = 35493 }
    isolated-1-data    = { api = 35499, gateway = 35494 }
  }
  ports        = lookup(local.slots, var.context.resource.properties.slot, local.slots.shared-control)
  cluster_name = "${var.resource_prefix}-${var.context.resource.properties.slot}"
  node_image   = "kindest/node:v1.35.0@sha256:452d707d4862f52530247495d180205e029056831160e22870e37e3f6c1ac31f"

  # The provider does not mark these computed credentials sensitive.
  host_access = yamldecode(sensitive(kind_cluster.child.kubeconfig))
  pod_access = merge(local.host_access, {
    "current-context" = local.cluster_name
    contexts = [for entry in local.host_access.contexts : merge(entry, {
      name = local.cluster_name
    })]
    clusters = [for entry in local.host_access.clusters : merge(entry, {
      cluster = merge(entry.cluster, {
        server            = "https://${data.external.child_address.result.address}:6443"
        "tls-server-name" = local.cluster_name
      })
    })]
  })
}
