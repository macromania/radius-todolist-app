data "external" "prepared_images" {
  program = concat(
    ["sh", "${path.module}/check-images.sh", var.resource_prefix],
    flatten([for image in local.prepared_images : [image.reference, image.image_id]])
  )
}

resource "kind_cluster" "child" {
  depends_on      = [data.external.prepared_images]
  name            = local.cluster_name
  node_image      = local.node_image
  kubeconfig_path = "${path.root}/child.kubeconfig"
  wait_for_ready  = true

  kind_config {
    kind        = "Cluster"
    api_version = "kind.x-k8s.io/v1alpha4"

    networking {
      api_server_address = "127.0.0.1"
      api_server_port    = local.ports.api
    }

    node {
      role = "control-plane"
      labels = {
        "radplanes.local/slot"       = var.context.resource.properties.slot
        "plane-demo/resource-prefix" = var.resource_prefix
      }
      kubeadm_config_patches = [yamlencode({
        apiVersion = "kubeadm.k8s.io/v1beta3"
        kind       = "ClusterConfiguration"
        apiServer = {
          certSANs = ["localhost", "127.0.0.1", local.cluster_name]
        }
      })]

      extra_port_mappings {
        container_port = 31480
        host_port      = local.ports.gateway
        listen_address = "127.0.0.1"
        protocol       = "TCP"
      }

    }
  }

  lifecycle {
    precondition {
      condition     = contains([for image in var.dependency_images : image.reference], local.node_image)
      error_message = "The pinned kind node image must be present in the inspected preparation set."
    }
  }
}

data "external" "child_address" {
  depends_on = [kind_cluster.child]
  program    = ["sh", "${path.module}/node-address.sh", var.resource_prefix, var.context.resource.properties.slot]
}

resource "terraform_data" "images" {
  triggers_replace = {
    cluster_id = kind_cluster.child.id
    images     = local.prepared_images
  }

  provisioner "local-exec" {
    command = "sh \"${path.module}/load-images.sh\""
    environment = {
      LOCAL_CLUSTER         = kind_cluster.child.name
      LOCAL_RESOURCE_PREFIX = var.resource_prefix
      LOCAL_SLOT            = var.context.resource.properties.slot
      LOCAL_IMAGES          = join("\n", [for image in local.prepared_images : image.reference])
      LOCAL_IMAGE_IDS       = join("\n", [for image in local.prepared_images : image.image_id])
    }
  }
}

resource "kubernetes_secret_v1" "access" {
  depends_on = [terraform_data.images]

  metadata {
    name      = "${local.cluster_name}-access"
    namespace = var.access_namespace
    labels = {
      "radplanes.local/slot"       = var.context.resource.properties.slot
      "plane-demo/resource-prefix" = var.resource_prefix
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
