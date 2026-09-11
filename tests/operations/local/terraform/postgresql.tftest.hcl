mock_provider "kubernetes" {}
mock_provider "random" {
  mock_resource "random_password" {
    defaults = { result = "offline-PG!Password&only" }
  }
}

variables {
  node_address = "172.18.0.2"
  context = {
    application = { name = "management" }
    environment = { name = "management" }
    resource = {
      id         = "/planes/radius/local/resourceGroups/radplanes-local/providers/Demo.Platform/postgreSqlDatabases/postgres"
      properties = { databaseName = "management" }
    }
    runtime = { kubernetes = { namespace = "radplanes-local-management-management" } }
  }
}

run "postgres_contract" {
  command = apply

  assert {
    condition     = output.result.values.host == "172.18.0.2" && output.result.values.port == 31543 && !output.result.values.tlsRequired
    error_message = "Only the actual private node NodePort endpoint is allowed, with explicit local non-TLS."
  }
  assert {
    condition     = output.result.values.database == "management" && output.result.values.username == "plane_setup" && output.result.values.serverId == "kubernetes://radplanes-local-management-management/statefulsets/postgres"
    error_message = "The PostgreSQL output contract must match the existing type without Azure fields."
  }
  assert {
    condition     = output.result.values.setupSecretName == "postgres-setup" && kubernetes_secret_v1.server.metadata[0].name == "postgres-credentials"
    error_message = "Deleting initializer credentials must not remove the server credential."
  }
  assert {
    condition     = kubernetes_secret_v1.setup.data.password == kubernetes_secret_v1.server.data.password && output.result.secrets.password == random_password.server.result
    error_message = "The initializer and retained server must use the same generated password."
  }
  assert {
    condition     = issensitive(output.result) && !contains(keys(output.result.values), "password")
    error_message = "The password must only be in a sensitive result.secrets value."
  }
  assert {
    condition     = random_password.server.length == 40 && random_password.server.special && try(length(random_password.server.keepers), 0) == 0
    error_message = "The generated password must not rotate on Recipe reapply."
  }
  assert {
    condition     = kubernetes_persistent_volume_claim_v1.data.spec[0].storage_class_name == "standard" && tonumber(kubernetes_stateful_set_v1.postgres.spec[0].replicas) == 1
    error_message = "PostgreSQL requires a persistent singleton on the kind standard storage class."
  }
  assert {
    condition     = kubernetes_service_v1.postgres.spec[0].type == "NodePort" && kubernetes_service_v1.postgres.spec[0].port[0].node_port == 31543
    error_message = "The parent database must use only its private NodePort, not a host mapping."
  }
  assert {
    condition     = !kubernetes_stateful_set_v1.postgres.spec[0].template[0].spec[0].automount_service_account_token && kubernetes_stateful_set_v1.postgres.spec[0].template[0].spec[0].security_context[0].fs_group_change_policy == "OnRootMismatch"
    error_message = "Datastore Pods must not receive administrative tokens and must preserve volume permissions."
  }
  assert {
    condition     = one([for env in kubernetes_stateful_set_v1.postgres.spec[0].template[0].spec[0].container[0].env : env.value if env.name == "POSTGRES_HOST_AUTH_METHOD"]) == "scram-sha-256"
    error_message = "PostgreSQL must require password authentication."
  }
}

run "reapply_preserves_password_and_database" {
  command = apply
  assert {
    condition     = output.result.secrets.password == run.postgres_contract.result.secrets.password && output.result.values.database == run.postgres_contract.result.values.database
    error_message = "Reapply must preserve database identity and the generated credential."
  }
}

run "reject_public_node" {
  command = plan
  variables { node_address = "203.0.113.10" }
  expect_failures = [var.node_address]
}

run "reject_loopback_node" {
  command = plan
  variables { node_address = "127.0.0.1" }
  expect_failures = [var.node_address]
}

run "reject_invalid_ipv4" {
  command = plan
  variables { node_address = "172.18.999.2" }
  expect_failures = [var.node_address]
}
