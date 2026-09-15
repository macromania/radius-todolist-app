mock_provider "kubernetes" {}

variables {
  resource_prefix   = "radplanes-local"
  radius_group      = "radplanes-local"
  gateway_host_port = 35492
  context = {
    application = { name = "data" }
    environment = { name = "shared-data" }
    resource = {
      id = "/planes/radius/local/resourcegroups/radplanes-local/providers/Demo.Platform/gateways/gateway"
      properties = {
        apiService           = "data-api"
        apiPort              = 8088
        phase                = "https"
        certificateSecretUri = "https://azure-only.vault.azure.net/secrets/unused"
        challengeService     = "challenge"
        challengePort        = 8088
      }
    }
    runtime = { kubernetes = { namespace = "radplanes-local-shared-data-data" } }
  }
}

run "gateway_contract" {
  command = apply
  assert {
    condition     = output.result.values.host == "127.0.0.1" && output.result.values.url == "http://127.0.0.1:35492"
    error_message = "This local implementation is HTTP even when the shared application uses its Azure HTTPS phase."
  }
  assert {
    condition     = output.result.values.gatewayId == "kubernetes://radplanes-local-shared-data-data/services/gateway"
    error_message = "Gateway IDs must reference actual Kubernetes resources, not synthetic Azure identifiers."
  }
  assert {
    condition     = toset(keys(output.result.values)) == toset(["host", "url", "gatewayId", "apiBackendService"])
    error_message = "The DNS-backed local gateway must not require or fabricate a node/backend IP."
  }
  assert {
    condition     = kubernetes_service_v1.backend.spec[0].selector == tomap({ "radapp.io/application" = "data", "radapp.io/resource" = "data-api" })
    error_message = "Backend selection must use the exact Radius 0.60.2 workload labels."
  }
  assert {
    condition     = kubernetes_service_v1.gateway.spec[0].type == "NodePort" && kubernetes_service_v1.gateway.spec[0].port[0].node_port == 31480
    error_message = "Envoy must use the existing kind gateway mapping."
  }
  assert {
    condition     = yamldecode(kubernetes_config_map_v1.envoy.data["envoy.yaml"]).static_resources.clusters[0].load_assignment.endpoints[0].lb_endpoints[0].endpoint.address.socket_address.address == "gateway-api.radplanes-local-shared-data-data.svc.cluster.local"
    error_message = "Envoy must route to the declared backend service in its own cluster."
  }
  assert {
    condition     = kubernetes_deployment_v1.envoy.spec[0].template[0].spec[0].container[0].command == tolist(["envoy", "-c", "/etc/envoy/envoy.yaml", "--log-level", "warning", "--concurrency", "1"])
    error_message = "The actual run path must load the Recipe's Envoy configuration."
  }
  assert {
    condition     = !contains(keys(output.result.values), "challengeBackendService") && !strcontains(kubernetes_config_map_v1.envoy.data["envoy.yaml"], "certificate") && !strcontains(kubernetes_config_map_v1.envoy.data["envoy.yaml"], "challenge")
    error_message = "Local HTTP must not pretend to configure certificates or cloud challenge backends."
  }
}

run "reject_foreign_radius_group" {
  command = plan
  variables {
    context = {
      application = { name = "data" }
      environment = { name = "shared-data" }
      resource = {
        id         = "/planes/radius/local/resourcegroups/foreign/providers/Demo.Platform/gateways/gateway"
        properties = { apiService = "data-api", apiPort = 8088 }
      }
      runtime = { kubernetes = { namespace = "radplanes-local-shared-data-data" } }
    }
  }
  expect_failures = [var.context]
}

run "reject_another_slots_port" {
  command = plan
  variables { gateway_host_port = 35491 }
  expect_failures = [var.gateway_host_port]
}

run "reject_unreserved_port" {
  command = plan
  variables { gateway_host_port = 8080 }
  expect_failures = [var.gateway_host_port]
}

run "reject_wrong_namespace" {
  command = plan
  variables {
    context = {
      application = { name = "data" }
      environment = { name = "shared-data" }
      resource = {
        id         = "/planes/radius/local/resourceGroups/radplanes-local/providers/Demo.Platform/gateways/gateway"
        properties = { apiService = "data-api", apiPort = 8088 }
      }
      runtime = { kubernetes = { namespace = "unrelated-data" } }
    }
  }
  expect_failures = [var.context]
}
