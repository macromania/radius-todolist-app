resource "random_password" "server" {
  length           = 40
  special          = true
  override_special = "!#$%&*+-=?@^_"
}

resource "kubernetes_secret_v1" "server" {
  metadata {
    name        = "redis-credentials"
    namespace   = local.namespace
    labels      = local.labels
    annotations = local.annotations
  }
  type = "Opaque"
  data = {
    password     = random_password.server.result
    "redis.conf" = <<-CONF
      bind 0.0.0.0
      port 6379
      protected-mode yes
      requirepass "${random_password.server.result}"
      appendonly yes
      appendfsync everysec
      dir /data
    CONF
  }
}

resource "kubernetes_persistent_volume_claim_v1" "data" {
  metadata {
    name        = "redis-data"
    namespace   = local.namespace
    labels      = local.labels
    annotations = local.annotations
  }
  spec {
    access_modes       = ["ReadWriteOnce"]
    storage_class_name = "standard"
    resources {
      requests = { storage = "1Gi" }
    }
  }
  wait_until_bound = false
}

resource "kubernetes_service_v1" "redis" {
  metadata {
    name        = "redis"
    namespace   = local.namespace
    labels      = local.labels
    annotations = local.annotations
  }
  spec {
    type     = "ClusterIP"
    selector = local.labels
    port {
      name        = "redis"
      port        = 6379
      target_port = 6379
    }
  }
}

resource "kubernetes_stateful_set_v1" "redis" {
  metadata {
    name        = "redis"
    namespace   = local.namespace
    labels      = local.labels
    annotations = local.annotations
  }
  spec {
    service_name = kubernetes_service_v1.redis.metadata[0].name
    replicas     = 1
    selector {
      match_labels = local.labels
    }
    template {
      metadata {
        labels = local.labels
      }
      spec {
        automount_service_account_token = false
        security_context {
          fs_group               = 999
          fs_group_change_policy = "OnRootMismatch"
          run_as_user            = 999
          run_as_group           = 999
          run_as_non_root        = true
        }
        container {
          name  = "redis"
          image = local.image
          args  = ["redis-server", "/etc/redis/redis.conf"]
          env {
            name = "REDISCLI_AUTH"
            value_from {
              secret_key_ref {
                name = kubernetes_secret_v1.server.metadata[0].name
                key  = "password"
              }
            }
          }
          port {
            name           = "redis"
            container_port = 6379
          }
          readiness_probe {
            exec {
              command = ["sh", "-ec", "test \"$(redis-cli --raw ping)\" = PONG"]
            }
            initial_delay_seconds = 3
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
            name       = "data"
            mount_path = "/data"
          }
          volume_mount {
            name       = "config"
            mount_path = "/etc/redis"
            read_only  = true
          }
        }
        volume {
          name = "data"
          persistent_volume_claim {
            claim_name = kubernetes_persistent_volume_claim_v1.data.metadata[0].name
          }
        }
        volume {
          name = "config"
          secret {
            secret_name  = kubernetes_secret_v1.server.metadata[0].name
            default_mode = "0440"
            items {
              key  = "redis.conf"
              path = "redis.conf"
            }
          }
        }
      }
    }
  }
  wait_for_rollout = true
}
