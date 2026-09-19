param context object
param location string = resourceGroup().location
param gatewaySubnetId string
param gatewaySubnetCidr string
param nodeSubnetName string
param apiPrivateIp string
param challengePrivateIp string
param gatewayIdentityId string
@allowed([1, 2, 3])
param capacity int
param apiHealthPath string = '/healthz'
@allowed(['challenge', 'https'])
param phase string = context.resource.properties.phase
@description('Non-secret, versionless vault URI. HTTPS without a nonempty reference is rejected by parameter validation.')
@minLength(1)
param certificateSecretUri string = phase == 'https' ? context.resource.properties.certificateSecretUri : 'challenge-only'
param tags object = {}

// This built-in Kubernetes extension is pinned by Radius 0.60.2 / Bicep 0.42.1.
extension kubernetes with {
  kubeConfig: ''
  namespace: context.runtime.kubernetes.namespace
} as kubernetes

var suffix = uniqueString(context.resource.id, resourceGroup().id)
var gatewayName = 'agw-${suffix}'
var gatewayId = resourceId('Microsoft.Network/applicationGateways', gatewayName)
var preference = context.resource.properties.?hostname ?? ''
var dnsLabel = empty(preference) ? 'radplanes-${suffix}' : preference
var requiredTags = union(tags, {
  SecurityControl: 'Ignore'
  managedBy: 'radius-todolist-app'
  'radapp.io-environment': context.environment.id
  'radapp.io-application': context.application.id
  'radapp.io-resource': context.resource.id
})

resource apiService 'core/Service@v1' = {
  metadata: {
    name: 'gw-${suffix}-api'
    namespace: context.runtime.kubernetes.namespace
    annotations: {
      'service.beta.kubernetes.io/azure-load-balancer-internal': 'true'
      'service.beta.kubernetes.io/azure-load-balancer-internal-subnet': nodeSubnetName
      'service.beta.kubernetes.io/azure-load-balancer-ipv4': apiPrivateIp
      'service.beta.kubernetes.io/azure-load-balancer-resource-tags': 'SecurityControl=Ignore,project=radplanes,managedBy=radius-todolist-app'
    }
  }
  spec: {
    type: 'LoadBalancer'
    loadBalancerSourceRanges: [
      gatewaySubnetCidr
    ]
    // These are the exact v0.60.2 renderer selectors, not guessed Service-name labels.
    selector: {
      'radapp.io/application': toLower(context.application.name)
      'radapp.io/resource': toLower(context.resource.properties.apiService)
    }
    ports: [
      {
        name: 'http'
        protocol: 'TCP'
        port: context.resource.properties.apiPort
        targetPort: context.resource.properties.apiPort
      }
    ]
  }
}

resource challengeService 'core/Service@v1' = {
  metadata: {
    name: 'gw-${suffix}-challenge'
    namespace: context.runtime.kubernetes.namespace
    annotations: {
      'service.beta.kubernetes.io/azure-load-balancer-internal': 'true'
      'service.beta.kubernetes.io/azure-load-balancer-internal-subnet': nodeSubnetName
      'service.beta.kubernetes.io/azure-load-balancer-ipv4': challengePrivateIp
      'service.beta.kubernetes.io/azure-load-balancer-resource-tags': 'SecurityControl=Ignore,project=radplanes,managedBy=radius-todolist-app'
    }
  }
  spec: {
    type: 'LoadBalancer'
    loadBalancerSourceRanges: [
      gatewaySubnetCidr
    ]
    selector: {
      'radapp.io/application': toLower(context.application.name)
      'radapp.io/resource': toLower(context.resource.properties.challengeService)
    }
    ports: [
      {
        name: 'http'
        protocol: 'TCP'
        port: context.resource.properties.challengePort
        targetPort: context.resource.properties.challengePort
      }
    ]
  }
}

resource publicIp 'Microsoft.Network/publicIPAddresses@2024-07-01' = {
  name: 'pip-${suffix}'
  location: location
  tags: requiredTags
  sku: {
    name: 'Standard'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
    dnsSettings: {
      domainNameLabel: dnsLabel
      domainNameLabelScope: 'ResourceGroupReuse'
    }
  }
}

var httpListener = {
  name: 'http'
  properties: {
    protocol: 'Http'
    hostName: publicIp.properties.dnsSettings.fqdn
    frontendIPConfiguration: {
      id: '${gatewayId}/frontendIPConfigurations/public'
    }
    frontendPort: {
      id: '${gatewayId}/frontendPorts/http'
    }
  }
}
var httpsListener = {
  name: 'https'
  properties: {
    protocol: 'Https'
    hostName: publicIp.properties.dnsSettings.fqdn
    requireServerNameIndication: true
    frontendIPConfiguration: {
      id: '${gatewayId}/frontendIPConfigurations/public'
    }
    frontendPort: {
      id: '${gatewayId}/frontendPorts/https'
    }
    sslCertificate: {
      id: '${gatewayId}/sslCertificates/plane'
    }
  }
}
var challengeDefault = {
  defaultBackendAddressPool: {
    id: '${gatewayId}/backendAddressPools/challenge'
  }
  defaultBackendHttpSettings: {
    id: '${gatewayId}/backendHttpSettingsCollection/challenge'
  }
}
var httpsDefault = {
  defaultRedirectConfiguration: {
    id: '${gatewayId}/redirectConfigurations/to-https'
  }
}
var httpRule = {
  name: 'http-challenges'
  properties: {
    priority: 100
    ruleType: 'PathBasedRouting'
    httpListener: {
      id: '${gatewayId}/httpListeners/http'
    }
    urlPathMap: {
      id: '${gatewayId}/urlPathMaps/http'
    }
  }
}
var httpsRule = {
  name: 'https-api'
  properties: {
    priority: 200
    ruleType: 'Basic'
    httpListener: {
      id: '${gatewayId}/httpListeners/https'
    }
    backendAddressPool: {
      id: '${gatewayId}/backendAddressPools/api'
    }
    backendHttpSettings: {
      id: '${gatewayId}/backendHttpSettingsCollection/api'
    }
  }
}

resource gateway 'Microsoft.Network/applicationGateways@2024-07-01' = {
  name: gatewayName
  location: location
  tags: requiredTags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${gatewayIdentityId}': {}
    }
  }
  properties: {
    sku: {
      name: 'Standard_v2'
      tier: 'Standard_v2'
      capacity: capacity
    }
    enableHttp2: true
    sslPolicy: {
      policyType: 'Predefined'
      policyName: 'AppGwSslPolicy20220101S'
    }
    gatewayIPConfigurations: [
      {
        name: 'gateway'
        properties: {
          subnet: {
            id: gatewaySubnetId
          }
        }
      }
    ]
    frontendIPConfigurations: [
      {
        name: 'public'
        properties: {
          publicIPAddress: {
            id: publicIp.id
          }
        }
      }
    ]
    frontendPorts: concat([
      {
        name: 'http'
        properties: {
          port: 80
        }
      }
    ], phase == 'https' ? [
      {
        name: 'https'
        properties: {
          port: 443
        }
      }
    ] : [])
    backendAddressPools: [
      {
        name: 'api'
        properties: {
          backendAddresses: [
            {
              ipAddress: apiPrivateIp
            }
          ]
        }
      }
      {
        name: 'challenge'
        properties: {
          backendAddresses: [
            {
              ipAddress: challengePrivateIp
            }
          ]
        }
      }
    ]
    probes: [
      {
        name: 'api'
        properties: {
          protocol: 'Http'
          host: '127.0.0.1'
          path: apiHealthPath
          interval: 30
          timeout: 10
          unhealthyThreshold: 3
          match: {
            statusCodes: [
              '200'
            ]
          }
        }
      }
      {
        name: 'challenge'
        properties: {
          protocol: 'Http'
          host: '127.0.0.1'
          path: '/.well-known/acme-challenge/health-probe'
          interval: 30
          timeout: 10
          unhealthyThreshold: 3
          // No token is installed here. A real responder's deliberate 404 proves reachability.
          match: {
            statusCodes: [
              '404'
            ]
          }
        }
      }
    ]
    backendHttpSettingsCollection: [
      {
        name: 'api'
        properties: {
          port: context.resource.properties.apiPort
          protocol: 'Http'
          cookieBasedAffinity: 'Disabled'
          requestTimeout: 30
          probe: {
            id: '${gatewayId}/probes/api'
          }
        }
      }
      {
        name: 'challenge'
        properties: {
          port: context.resource.properties.challengePort
          protocol: 'Http'
          cookieBasedAffinity: 'Disabled'
          requestTimeout: 30
          probe: {
            id: '${gatewayId}/probes/challenge'
          }
        }
      }
    ]
    sslCertificates: phase == 'https' ? [
      {
        name: 'plane'
        properties: {
          keyVaultSecretId: certificateSecretUri
        }
      }
    ] : []
    httpListeners: concat([httpListener], phase == 'https' ? [httpsListener] : [])
    redirectConfigurations: phase == 'https' ? [
      {
        name: 'to-https'
        properties: {
          redirectType: 'Permanent'
          includePath: true
          includeQueryString: true
          targetListener: {
            id: '${gatewayId}/httpListeners/https'
          }
        }
      }
    ] : []
    urlPathMaps: [
      {
        name: 'http'
        properties: union(phase == 'https' ? httpsDefault : challengeDefault, {
          pathRules: [
            {
              name: 'acme-challenge'
              properties: {
                paths: [
                  '/.well-known/acme-challenge/*'
                ]
                backendAddressPool: {
                  id: '${gatewayId}/backendAddressPools/challenge'
                }
                backendHttpSettings: {
                  id: '${gatewayId}/backendHttpSettingsCollection/challenge'
                }
              }
            }
          ]
        })
      }
    ]
    requestRoutingRules: concat([httpRule], phase == 'https' ? [httpsRule] : [])
  }
  dependsOn: [
    apiService
    challengeService
  ]
}

output result object = {
  resources: [
    '/planes/kubernetes/local/namespaces/${apiService.metadata.namespace}/providers/core/Service/${apiService.metadata.name}'
    '/planes/kubernetes/local/namespaces/${challengeService.metadata.namespace}/providers/core/Service/${challengeService.metadata.name}'
    publicIp.id
    gateway.id
  ]
  values: {
    host: publicIp.properties.dnsSettings.fqdn
    url: '${phase == 'https' ? 'https' : 'http'}://${publicIp.properties.dnsSettings.fqdn}'
    gatewayId: gateway.id
    apiBackendService: apiService.metadata.name
    challengeBackendService: challengeService.metadata.name
    apiBackendIp: apiPrivateIp
    challengeBackendIp: challengePrivateIp
  }
}
