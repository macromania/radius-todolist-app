mock_provider "kind" {
  mock_resource "kind_cluster" {
    defaults = {
      kubeconfig = <<-YAML
        apiVersion: v1
        kind: Config
        current-context: kind-radplanes-local-shared-control
        clusters:
          - name: kind-radplanes-local-shared-control
            cluster:
              server: https://127.0.0.1:35496
              certificate-authority-data: offline-ca-fixture
        contexts:
          - name: kind-radplanes-local-shared-control
            context:
              cluster: kind-radplanes-local-shared-control
              user: kind-radplanes-local-shared-control
        users:
          - name: kind-radplanes-local-shared-control
            user:
              client-certificate-data: offline-client-certificate
              client-key-data: offline-client-key
      YAML
    }
  }
}

mock_provider "external" {
  mock_data "external" {
    defaults = {
      result = { address = "172.18.0.3" }
    }
  }
}

mock_provider "kubernetes" {
}

variables {
  context = {
    resource = {
      id = "/planes/radius/local/resourceGroups/radplanes-local/providers/Demo.Platform/clusters/shared-control"
      properties = {
        environment = "/planes/radius/local/resourceGroups/radplanes-local/providers/Applications.Core/environments/cluster-gate"
        slot        = "shared-control"
      }
    }
  }
}

run "bounded_recipe_contract" {
  command = apply

  assert {
    condition     = kind_cluster.child.kind_config[0].networking[0].api_server_address == "127.0.0.1"
    error_message = "The host API must bind loopback only."
  }

  assert {
    condition     = kind_cluster.child.kind_config[0].networking[0].api_server_port == 35496
    error_message = "The child must use its reserved API port."
  }

  assert {
    condition     = kind_cluster.child.kind_config[0].node[0].extra_port_mappings[0].host_port == 35491
    error_message = "The gateway must use the shared-control reservation."
  }

  assert {
    condition     = length(kind_cluster.child.kind_config[0].node[0].extra_port_mappings) == 1
    error_message = "PostgreSQL must never receive a host mapping."
  }

  assert {
    condition     = output.result.values.bootstrapAccessRef == "kubernetes://radplanes-local-access/radplanes-local-shared-control-access#kubeconfig"
    error_message = "Only the protected reference may leave the Recipe."
  }

  assert {
    condition     = nonsensitive(jsondecode(kubernetes_secret_v1.access.data.kubeconfig).clusters[0].cluster.server) == "https://172.18.0.3:6443"
    error_message = "Sibling Pods cannot use the host-facing 127.0.0.1 endpoint."
  }

  assert {
    condition     = nonsensitive(jsondecode(kubernetes_secret_v1.access.data.kubeconfig).clusters[0].cluster["certificate-authority-data"]) == "offline-ca-fixture"
    error_message = "Endpoint correction must preserve the original CA."
  }

  assert {
    condition     = nonsensitive(jsondecode(kubernetes_secret_v1.access.data.kubeconfig).clusters[0].cluster["tls-server-name"]) == "radplanes-local-shared-control"
    error_message = "Certificate verification must use the explicit kubeadm SAN."
  }

  assert {
    condition     = nonsensitive(jsondecode(kubernetes_secret_v1.access.data.kubeconfig)["current-context"]) == "radplanes-local-shared-control"
    error_message = "The protected kubeconfig must select the project context, without kind's prefix."
  }

  assert {
    condition     = nonsensitive(jsondecode(kubernetes_secret_v1.access.data.kubeconfig).contexts[0].name) == "radplanes-local-shared-control"
    error_message = "The selected project context must exist in the protected kubeconfig."
  }

  assert {
    condition     = nonsensitive(jsondecode(kubernetes_secret_v1.access.data.kubeconfig).contexts[0].context.cluster) == "kind-radplanes-local-shared-control" && nonsensitive(jsondecode(kubernetes_secret_v1.access.data.kubeconfig).contexts[0].context.user) == "kind-radplanes-local-shared-control"
    error_message = "Renaming the context must preserve its existing cluster and user references."
  }

  assert {
    condition     = nonsensitive(jsondecode(kubernetes_secret_v1.access.data.kubeconfig).users[0].user["client-key-data"]) == "offline-client-key"
    error_message = "Context renaming must preserve the original client credentials."
  }

  assert {
    condition     = yamldecode(kind_cluster.child.kubeconfig)["current-context"] == "kind-radplanes-local-shared-control"
    error_message = "Leave the kind provider's original kubeconfig unchanged."
  }

  assert {
    condition     = toset(keys(output.result.values)) == toset(["clusterId", "clusterName", "bootstrapAccessRef"])
    error_message = "Do not fabricate Azure-only fields or expose kubeconfig output."
  }
}

run "reject_other_slots" {
  command = plan
  variables {
    context = {
      resource = {
        id = "unused"
        properties = {
          environment = "unused"
          slot        = "isolated-1-control"
        }
      }
    }
  }
  expect_failures = [var.context]
}
