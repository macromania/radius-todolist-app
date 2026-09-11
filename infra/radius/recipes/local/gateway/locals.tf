locals {
  namespace = var.context.runtime.kubernetes.namespace
  image     = "docker.io/envoyproxy/envoy:v1.37.1@sha256:29496a88fba9c4c9cdef4afe8fec70f536c5ba111b1c2bddbc5436b091ceca33"
  labels = {
    "radplanes.local/component" = "gateway"
    "radplanes.local/slot"      = var.context.environment.name
  }
  annotations = {
    "radplanes.local/radius-resource" = var.context.resource.id
  }
  envoy = yamlencode({
    static_resources = {
      listeners = [{
        name    = "http"
        address = { socket_address = { address = "0.0.0.0", port_value = 18088 } }
        filter_chains = [{
          filters = [{
            name = "envoy.filters.network.http_connection_manager"
            typed_config = {
              "@type"     = "type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager"
              stat_prefix = "local_http"
              route_config = {
                name = "api"
                virtual_hosts = [{
                  name    = "api"
                  domains = ["*"]
                  routes  = [{ match = { prefix = "/" }, route = { cluster = "api", timeout = "30s" } }]
                }]
              }
              http_filters = [{
                name         = "envoy.filters.http.router"
                typed_config = { "@type" = "type.googleapis.com/envoy.extensions.filters.http.router.v3.Router" }
              }]
            }
          }]
        }]
      }]
      clusters = [{
        name            = "api"
        type            = "STRICT_DNS"
        connect_timeout = "5s"
        load_assignment = {
          cluster_name = "api"
          endpoints = [{
            lb_endpoints = [{
              endpoint = {
                address = {
                  socket_address = {
                    address    = "gateway-api.${local.namespace}.svc.cluster.local"
                    port_value = var.context.resource.properties.apiPort
                  }
                }
              }
            }]
          }]
        }
      }]
    }
  })
}
