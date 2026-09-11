mock_provider "kubernetes" {}
mock_provider "random" {
  mock_resource "random_password" {
    defaults = { result = "offline+Password&=!?@" }
  }
}

variables {
  context = {
    application = { name = "data" }
    environment = { name = "shared-data" }
    resource    = { id = "/planes/radius/local/resourceGroups/radplanes-local/providers/Applications.Datastores/redisCaches/redis" }
    runtime     = { kubernetes = { namespace = "radplanes-local-shared-data-data" } }
  }
}

run "redis_contract" {
  command = apply
  assert {
    condition     = output.result.values.host == "redis.radplanes-local-shared-data-data.svc.cluster.local" && output.result.values.port == 6379 && !output.result.values.tls
    error_message = "Redis must be local cluster DNS only, with explicit local non-TLS."
  }
  assert {
    condition     = output.result.secrets.password == "offline%2BPassword%26%3D%21%3F%40" && output.result.secrets.url == "redis://:offline%2BPassword%26%3D%21%3F%40@redis.radplanes-local-shared-data-data.svc.cluster.local:6379"
    error_message = "Encode the standalone password and URL exactly once for the existing consumer."
  }
  assert {
    condition     = issensitive(output.result) && !contains(keys(output.result.values), "password") && !contains(keys(output.result.values), "url")
    error_message = "Redis credentials must not appear in ordinary Recipe values."
  }
  assert {
    condition     = kubernetes_service_v1.redis.spec[0].type == "ClusterIP" && kubernetes_persistent_volume_claim_v1.data.spec[0].storage_class_name == "standard"
    error_message = "Redis is persistent and private, not a NodePort or host binding."
  }
  assert {
    condition     = strcontains(kubernetes_secret_v1.server.data["redis.conf"], "appendonly yes") && strcontains(kubernetes_secret_v1.server.data["redis.conf"], "requirepass \"offline+Password&=!?@\"")
    error_message = "The actual server configuration must require the generated password and AOF."
  }
  assert {
    condition     = kubernetes_stateful_set_v1.redis.spec[0].template[0].spec[0].container[0].args == tolist(["redis-server", "/etc/redis/redis.conf"])
    error_message = "The StatefulSet must invoke the authenticated configuration, not an unused fixture."
  }
  assert {
    condition     = !kubernetes_stateful_set_v1.redis.spec[0].template[0].spec[0].automount_service_account_token && kubernetes_stateful_set_v1.redis.spec[0].template[0].spec[0].security_context[0].fs_group_change_policy == "OnRootMismatch"
    error_message = "Redis must not receive a Kubernetes token or change PVC traversal behavior."
  }
}

run "reapply_preserves_redis_password" {
  command = apply
  assert {
    condition     = output.result.secrets.password == run.redis_contract.result.secrets.password
    error_message = "Reapply must not regenerate the Redis password."
  }
}

run "reject_management_redis" {
  command = plan
  variables {
    context = {
      application = { name = "management" }
      environment = { name = "management" }
      resource    = { id = "/planes/radius/local/resourceGroups/radplanes-local/providers/Applications.Datastores/redisCaches/redis" }
      runtime     = { kubernetes = { namespace = "radplanes-local-management-management" } }
    }
  }
  expect_failures = [var.context]
}
