resource "kind_cluster" "child" {
  name            = local.cluster_name
  node_image      = local.node_image
  kubeconfig_path = "${path.root}/child.kubeconfig"
  wait_for_ready  = true

  kind_config {
    kind        = "Cluster"
    api_version = "kind.x-k8s.io/v1alpha4"

    networking {
      api_server_address = "127.0.0.1"
      api_server_port    = 35496
    }

    node {
      role = "control-plane"
      labels = {
        "radplanes.local/slot" = "shared-control"
      }
      kubeadm_config_patches = [yamlencode({
        apiVersion = "kubeadm.k8s.io/v1beta4"
        kind       = "ClusterConfiguration"
        apiServer = {
          certSANs = [local.cluster_name]
        }
      })]

      extra_port_mappings {
        container_port = 31480
        host_port      = 35491
        listen_address = "127.0.0.1"
        protocol       = "TCP"
      }
    }
  }
}

data "external" "child_address" {
  program = ["sh", "${path.module}/node-address.sh", kind_cluster.child.name]
}

resource "kubernetes_secret_v1" "access" {
  metadata {
    name      = "${local.cluster_name}-access"
    namespace = "radplanes-local-access"
    labels = {
      "radplanes.local/slot" = "shared-control"
    }
    annotations = {
      "radplanes.local/radius-resource" = var.context.resource.id
    }
  }

  type = "Opaque"
  data = {
    kubeconfig = jsonencode(local.pod_access)
  }

  lifecycle {
    precondition {
      condition = try(
        length(local.host_access.contexts) == 1 &&
        local.host_access.contexts[0].name == "kind-${local.cluster_name}" &&
        local.host_access["current-context"] == "kind-${local.cluster_name}",
        false
      )
      error_message = "Expected one kind-generated child context before preparing the protected access copy."
    }

    precondition {
      condition     = can(cidrnetmask("${data.external.child_address.result.address}/32"))
      error_message = "The verified kind node must have an IPv4 address on the kind network."
    }
  }
}
