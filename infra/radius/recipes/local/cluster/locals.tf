locals {
  cluster_name = "radplanes-local-${var.context.resource.properties.slot}"
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
