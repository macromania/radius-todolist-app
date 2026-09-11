resource "random_password" "server" {
  length           = 40
  special          = true
  override_special = "!#$%&*+-=?@^_"
}

resource "kubernetes_secret_v1" "server" {
  metadata {
    name        = "postgres-credentials"
    namespace   = local.namespace
    labels      = local.labels
    annotations = local.annotations
  }
  type = "Opaque"
  data = {
    password = random_password.server.result
  }
}

# The initializer copy may be deleted after setup; the server's credential is retained.
resource "kubernetes_secret_v1" "setup" {
  metadata {
    name        = "postgres-setup"
    namespace   = local.namespace
    labels      = local.labels
    annotations = local.annotations
  }
  type = "Opaque"
  data = {
    host     = var.node_address
    port     = "31543"
    database = local.database
    username = local.username
    password = random_password.server.result
  }
}

resource "kubernetes_persistent_volume_claim_v1" "data" {
  metadata {
    name        = "postgres-data"
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

resource "kubernetes_service_v1" "postgres" {
  metadata {
    name        = "postgres"
    namespace   = local.namespace
    labels      = local.labels
    annotations = local.annotations
  }
  spec {
    type     = "NodePort"
    selector = local.labels
    port {
      name        = "postgres"
      port        = 5432
      target_port = 5432
      node_port   = 31543
    }
  }
}

resource "kubernetes_stateful_set_v1" "postgres" {
  metadata {
    name        = "postgres"
    namespace   = local.namespace
    labels      = local.labels
    annotations = local.annotations
  }
  spec {
    service_name = kubernetes_service_v1.postgres.metadata[0].name
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
          fs_group               = 70
          fs_group_change_policy = "OnRootMismatch"
          run_as_user            = 70
          run_as_group           = 70
          run_as_non_root        = true
        }
        container {
          name  = "postgres"
          image = local.image
          args  = ["postgres", "-c", "password_encryption=scram-sha-256"]
          env {
            name  = "POSTGRES_DB"
            value = local.database
          }
          env {
            name  = "POSTGRES_USER"
            value = local.username
          }
          env {
            name = "POSTGRES_PASSWORD"
            value_from {
              secret_key_ref {
                name = kubernetes_secret_v1.server.metadata[0].name
                key  = "password"
              }
            }
          }
          env {
            name  = "POSTGRES_HOST_AUTH_METHOD"
            value = "scram-sha-256"
          }
          env {
            name  = "POSTGRES_INITDB_ARGS"
            value = "--auth-host=scram-sha-256 --auth-local=scram-sha-256"
          }
          env {
            name  = "PGDATA"
            value = "/var/lib/postgresql/data/pgdata"
          }
          port {
            name           = "postgres"
            container_port = 5432
          }
          readiness_probe {
            exec {
              command = ["pg_isready", "-h", "127.0.0.1", "-U", local.username, "-d", local.database]
            }
            initial_delay_seconds = 5
            period_seconds        = 5
          }
          security_context {
            allow_privilege_escalation = false
            capabilities {
              drop = ["ALL"]
            }
          }
          resources {
            requests = { cpu = "50m", memory = "128Mi" }
            limits   = { cpu = "500m", memory = "512Mi" }
          }
          volume_mount {
            name       = "data"
            mount_path = "/var/lib/postgresql/data"
          }
          volume_mount {
            name       = "run"
            mount_path = "/var/run/postgresql"
          }
        }
        volume {
          name = "data"
          persistent_volume_claim {
            claim_name = kubernetes_persistent_volume_claim_v1.data.metadata[0].name
          }
        }
        volume {
          name = "run"
          empty_dir {}
        }
      }
    }
  }
  wait_for_rollout = true
}
