resource "kubernetes_service_v1" "backend" {
  metadata {
    name        = "gateway-api"
    namespace   = local.namespace
    labels      = local.labels
    annotations = local.annotations
  }
  spec {
    type = "ClusterIP"
    selector = {
      "radapp.io/application" = var.context.application.name
      "radapp.io/resource"    = var.context.resource.properties.apiService
    }
    port {
      name        = "http"
      port        = var.context.resource.properties.apiPort
      target_port = var.context.resource.properties.apiPort
    }
  }
}

resource "kubernetes_config_map_v1" "envoy" {
  metadata {
    name        = "gateway-envoy"
    namespace   = local.namespace
    labels      = local.labels
    annotations = local.annotations
  }
  data = { "envoy.yaml" = local.envoy }
}

resource "kubernetes_deployment_v1" "envoy" {
  metadata {
    name        = "gateway"
    namespace   = local.namespace
    labels      = local.labels
    annotations = local.annotations
  }
  spec {
    replicas = 1
    selector {
      match_labels = local.labels
    }
    template {
      metadata {
        labels      = local.labels
        annotations = { "radplanes.local/config-sha256" = sha256(local.envoy) }
      }
      spec {
        automount_service_account_token = false
        security_context {
          run_as_user     = 101
          run_as_group    = 101
          run_as_non_root = true
        }
        container {
          name    = "envoy"
          image   = local.image
          command = ["envoy", "-c", "/etc/envoy/envoy.yaml", "--log-level", "warning", "--concurrency", "1"]
          port {
            name           = "http"
            container_port = 18088
          }
          readiness_probe {
            tcp_socket {
              port = 18088
            }
            initial_delay_seconds = 2
            period_seconds        = 5
          }
          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            capabilities {
              drop = ["ALL"]
            }
          }
          resources {
            requests = { cpu = "25m", memory = "32Mi" }
            limits   = { cpu = "250m", memory = "128Mi" }
          }
          volume_mount {
            name       = "config"
            mount_path = "/etc/envoy"
            read_only  = true
          }
        }
        volume {
          name = "config"
          config_map {
            name = kubernetes_config_map_v1.envoy.metadata[0].name
          }
        }
      }
    }
  }
  wait_for_rollout = true
}

resource "kubernetes_service_v1" "gateway" {
  metadata {
    name        = "gateway"
    namespace   = local.namespace
    labels      = local.labels
    annotations = local.annotations
  }
  spec {
    type     = "NodePort"
    selector = local.labels
    port {
      name        = "http"
      port        = 80
      target_port = 18088
      node_port   = 31480
    }
  }
}
