param prefix string
param location string
@description('Ordered roles, including management at index zero. Address allocations must not be reordered after deployment.')
param slots array
param tags object
param registryName string
@allowed(['Basic', 'Standard', 'Premium'])
param registrySkuName string
@description('Existing owned registries are read-only here so bootstrap cannot erase ARM-owned build proofs or unrelated tags.')
param registryExists bool = false
param vaultName string
@allowed(['standard', 'premium'])
param vaultSkuName string
param externalVaultResourceGroup string = ''

resource egressIp 'Microsoft.Network/publicIPAddresses@2024-07-01' = {
  name: 'pip-${prefix}-egress'
  location: location
  tags: tags
  sku: {
    name: 'Standard'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
  }
}

resource nat 'Microsoft.Network/natGateways@2024-07-01' = {
  name: 'nat-${prefix}'
  location: location
  tags: tags
  sku: {
    name: 'Standard'
  }
  properties: {
    idleTimeoutInMinutes: 10
    publicIpAddresses: [
      {
        id: egressIp.id
      }
    ]
  }
}

resource gatewayNsg 'Microsoft.Network/networkSecurityGroups@2024-07-01' = {
  name: 'nsg-${prefix}-gateways'
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: 'PublicHttpHttps'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'Internet'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRanges: [
            '80'
            '443'
          ]
        }
      }
      {
        name: 'GatewayManagement'
        properties: {
          priority: 110
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'GatewayManager'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '65200-65535'
        }
      }
      {
        name: 'GatewayHealth'
        properties: {
          priority: 120
          direction: 'Inbound'
          access: 'Allow'
          protocol: '*'
          sourceAddressPrefix: 'AzureLoadBalancer'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
    ]
  }
}

var nodeSubnets = [for (slot, i) in slots: {
  name: 'snet-${slot}-nodes'
  properties: {
    addressPrefix: '10.64.${i}.0/24'
    natGateway: {
      id: nat.id
    }
  }
}]
var gatewaySubnets = [for (slot, i) in slots: {
  name: 'snet-${slot}-gateway'
  properties: {
    addressPrefix: '10.64.${16 + i}.0/24'
    networkSecurityGroup: {
      id: gatewayNsg.id
    }
  }
}]
var endpointSubnets = [for (slot, i) in slots: {
  name: 'snet-${slot}-endpoints'
  properties: {
    addressPrefix: '10.64.${32 + i}.0/27'
    privateEndpointNetworkPolicies: 'Disabled'
  }
}]
var postgresSubnets = [for (slot, i) in slots: {
  name: 'snet-${slot}-postgresql'
  properties: {
    addressPrefix: '10.64.${48 + i}.0/27'
    delegations: [
      {
        name: 'postgresql'
        properties: {
          serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers'
        }
      }
    ]
  }
}]

resource vnet 'Microsoft.Network/virtualNetworks@2024-07-01' = {
  name: 'vnet-${prefix}'
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.64.0.0/16'
      ]
    }
    subnets: concat(nodeSubnets, gatewaySubnets, endpointSubnets, postgresSubnets)
  }
}

var zoneNames = [
  '${prefix}.postgres.database.azure.com'
  'privatelink.redis.azure.net'
  'privatelink.vaultcore.azure.net'
]

resource zones 'Microsoft.Network/privateDnsZones@2024-06-01' = [for name in zoneNames: {
  name: name
  location: 'global'
  tags: tags
}]

resource links 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = [for (name, i) in zoneNames: {
  parent: zones[i]
  name: 'link-${prefix}'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}]

resource registry 'Microsoft.ContainerRegistry/registries@2025-11-01' = if (!registryExists) {
  name: registryName
  location: location
  tags: tags
  sku: {
    name: registrySkuName
  }
  properties: {
    adminUserEnabled: false
    anonymousPullEnabled: false
    roleAssignmentMode: 'AbacRepositoryPermissions'
    publicNetworkAccess: 'Enabled'
  }
}

resource retainedRegistry 'Microsoft.ContainerRegistry/registries@2025-11-01' existing = {
  name: registryName
}

resource vault 'Microsoft.KeyVault/vaults@2024-11-01' = if (empty(externalVaultResourceGroup)) {
  name: vaultName
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: {
      family: 'A'
      name: vaultSkuName
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    enablePurgeProtection: true
    softDeleteRetentionInDays: 7
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      // App Gateway certificate validation uses the trusted-service path.
      // Public clients remain disabled and data-plane RBAC stays object-scoped.
      bypass: 'AzureServices'
      defaultAction: 'Deny'
    }
  }
}

resource suppliedVault 'Microsoft.KeyVault/vaults@2024-11-01' existing = {
  name: vaultName
  scope: resourceGroup(empty(externalVaultResourceGroup) ? resourceGroup().name : externalVaultResourceGroup)
}
var selectedVaultId = empty(externalVaultResourceGroup) ? vault!.id : suppliedVault.id

resource vaultEndpoint 'Microsoft.Network/privateEndpoints@2024-07-01' = {
  name: 'pe-${prefix}-vault'
  location: location
  tags: tags
  properties: {
    customNetworkInterfaceName: 'nic-${prefix}-vault'
    subnet: {
      id: resourceId('Microsoft.Network/virtualNetworks/subnets', vnet.name, 'snet-management-endpoints')
    }
    privateLinkServiceConnections: [
      {
        name: 'vault'
        properties: {
          privateLinkServiceId: selectedVaultId
          groupIds: [
            'vault'
          ]
        }
      }
    ]
  }
}

resource vaultNic 'Microsoft.Network/networkInterfaces@2024-07-01' existing = {
  name: 'nic-${prefix}-vault'
}
resource vaultNicTags 'Microsoft.Resources/tags@2021-04-01' = {
  scope: vaultNic
  name: 'default'
  properties: {
    tags: tags
  }
  dependsOn: [
    vaultEndpoint
  ]
}

resource vaultZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-07-01' = {
  parent: vaultEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'vault'
        properties: {
          privateDnsZoneId: zones[2].id
        }
      }
    ]
  }
}

output foundation object = {
  virtualNetworkId: vnet.id
  virtualNetworkName: vnet.name
  egressIp: egressIp.properties.ipAddress
  egressIpId: egressIp.id
  registryId: registryExists ? retainedRegistry.id : registry!.id
  registryName: registryName
  registryLoginServer: registryExists ? retainedRegistry.properties.loginServer : registry!.properties.loginServer
  registryRoleAssignmentMode: registryExists ? retainedRegistry.properties.roleAssignmentMode : registry!.properties.roleAssignmentMode
  vaultId: selectedVaultId
  vaultName: vaultName
  vaultUri: empty(externalVaultResourceGroup) ? vault!.properties.vaultUri : suppliedVault.properties.vaultUri
  vaultOwned: empty(externalVaultResourceGroup)
  vaultResourceGroup: empty(externalVaultResourceGroup) ? resourceGroup().name : externalVaultResourceGroup
  vaultPrivateEndpointId: vaultEndpoint.id
  postgresqlDnsZoneId: zones[0].id
  redisDnsZoneId: zones[1].id
  vaultDnsZoneId: zones[2].id
}

output allocations array = [for (slot, i) in slots: {
  slot: slot
  certificateName: 'gateway-${prefix}-${slot}'
  acmeStateSecretName: 'acme-${prefix}-${slot}'
  nodeSubnetId: resourceId('Microsoft.Network/virtualNetworks/subnets', vnet.name, 'snet-${slot}-nodes')
  nodeSubnetName: 'snet-${slot}-nodes'
  gatewaySubnetId: resourceId('Microsoft.Network/virtualNetworks/subnets', vnet.name, 'snet-${slot}-gateway')
  gatewaySubnetCidr: '10.64.${16 + i}.0/24'
  privateEndpointSubnetId: resourceId('Microsoft.Network/virtualNetworks/subnets', vnet.name, 'snet-${slot}-endpoints')
  postgresqlSubnetId: resourceId('Microsoft.Network/virtualNetworks/subnets', vnet.name, 'snet-${slot}-postgresql')
  apiPrivateIp: '10.64.${i}.240'
  challengePrivateIp: '10.64.${i}.241'
}]
