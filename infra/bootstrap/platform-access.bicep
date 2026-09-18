param foundation object
param allocations array
param coordinatorPrincipalId string
param operatorObjectId string
param managementRadiusPrincipalId string
param includePlatformOperatorGrants bool = true
param includeCoordinatorPull bool = true

var networkContributor = '4d97b98b-1d4f-4787-a291-c67834d212e7'
var dnsContributor = 'b12aa53e-6015-4669-85d0-8515ebb3ae7f'
var registryPolicy = loadJsonContent('../../scripts/operations/azure/registry-policy.json')
var reader = 'acdd72a7-3385-48ef-bd42-f606fba81ae7'

resource vnet 'Microsoft.Network/virtualNetworks@2024-07-01' existing = {
  name: foundation.virtualNetworkName
}
resource nodeSubnets 'Microsoft.Network/virtualNetworks/subnets@2024-07-01' existing = [for allocation in allocations: {
  parent: vnet
  name: allocation.nodeSubnetName
}]
resource gatewaySubnets 'Microsoft.Network/virtualNetworks/subnets@2024-07-01' existing = [for allocation in allocations: {
  parent: vnet
  name: 'snet-${allocation.slot}-gateway'
}]
resource endpointSubnets 'Microsoft.Network/virtualNetworks/subnets@2024-07-01' existing = [for allocation in allocations: {
  parent: vnet
  name: 'snet-${allocation.slot}-endpoints'
}]
resource postgresSubnets 'Microsoft.Network/virtualNetworks/subnets@2024-07-01' existing = [for allocation in allocations: {
  parent: vnet
  name: 'snet-${allocation.slot}-postgresql'
}]
resource registry 'Microsoft.ContainerRegistry/registries@2025-11-01' existing = {
  name: foundation.registryName
}
resource postgresDns 'Microsoft.Network/privateDnsZones@2024-06-01' existing = {
  name: last(split(foundation.postgresqlDnsZoneId, '/'))
}
resource redisDns 'Microsoft.Network/privateDnsZones@2024-06-01' existing = {
  name: 'privatelink.redis.azure.net'
}

resource clusterNetwork 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (allocation, i) in allocations: {
  scope: nodeSubnets[i]
  name: guid(nodeSubnets[i].id, allocation.identities.controlPlane.principalId, networkContributor)
  properties: {
    principalId: allocation.identities.controlPlane.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', networkContributor)
  }
}]

var networkReaders = concat(
  map(allocations, allocation => allocation.identities.controlPlane.principalId),
  map(allocations, allocation => allocation.identities.radius.principalId)
)
// Reading the containing VNet does not follow from a role granted on a child subnet.
resource networkRead 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for principal in networkReaders: {
  scope: vnet
  name: guid(vnet.id, principal, reader)
  properties: {
    principalId: principal
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', reader)
  }
}]

// Only management Radius needs to join AKS to all allocated node subnets.
resource recipeClusterNetwork 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (allocation, i) in allocations: {
  scope: nodeSubnets[i]
  name: guid(nodeSubnets[i].id, managementRadiusPrincipalId, networkContributor)
  properties: {
    principalId: managementRadiusPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', networkContributor)
  }
}]

resource gatewayNetwork 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (allocation, i) in allocations: {
  scope: gatewaySubnets[i]
  name: guid(gatewaySubnets[i].id, allocation.identities.radius.principalId, networkContributor)
  properties: {
    principalId: allocation.identities.radius.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', networkContributor)
  }
}]
resource endpointNetwork 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (allocation, i) in allocations: if (endsWith(allocation.slot, '-data')) {
  scope: endpointSubnets[i]
  name: guid(endpointSubnets[i].id, allocation.identities.radius.principalId, networkContributor)
  properties: {
    principalId: allocation.identities.radius.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', networkContributor)
  }
}]
resource postgresNetwork 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (allocation, i) in allocations: if (!endsWith(allocation.slot, '-data')) {
  scope: postgresSubnets[i]
  name: guid(postgresSubnets[i].id, allocation.identities.radius.principalId, networkContributor)
  properties: {
    principalId: allocation.identities.radius.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', networkContributor)
  }
}]
resource postgresDnsAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for allocation in allocations: if (!endsWith(allocation.slot, '-data')) {
  scope: postgresDns
  name: guid(postgresDns.id, allocation.identities.radius.principalId, dnsContributor)
  properties: {
    principalId: allocation.identities.radius.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', dnsContributor)
  }
}]
resource redisDnsAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for allocation in allocations: if (endsWith(allocation.slot, '-data')) {
  scope: redisDns
  name: guid(redisDns.id, allocation.identities.radius.principalId, dnsContributor)
  properties: {
    principalId: allocation.identities.radius.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', dnsContributor)
  }
}]

var imageReaders = concat(
  includeCoordinatorPull ? [coordinatorPrincipalId] : [],
  map(allocations, allocation => allocation.identities.kubelet.principalId),
  map(allocations, allocation => allocation.identities.radius.principalId)
)
resource coordinatorRegistryRead 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (includeCoordinatorPull) {
  scope: registry
  name: guid(registry.id, coordinatorPrincipalId, reader)
  properties: {
    principalId: coordinatorPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', reader)
    description: 'Read registry ARM properties and authorization metadata through this registry scope; no writes or delegation.'
  }
}
resource registryPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for principal in imageReaders: {
  scope: registry
  name: guid(registry.id, principal, registryPolicy.repositoryReaderRoleId)
  properties: {
    principalId: principal
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', registryPolicy.repositoryReaderRoleId)
  }
}]
resource registryPublish 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (includePlatformOperatorGrants) {
  scope: registry
  name: guid(registry.id, operatorObjectId, registryPolicy.repositoryWriterRoleId)
  properties: {
    principalId: operatorObjectId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', registryPolicy.repositoryWriterRoleId)
    conditionVersion: registryPolicy.conditionVersion
    condition: registryPolicy.writerCondition
    description: 'Data-plane writes are limited to image repositories and Recipe staging, never canonical Recipes.'
  }
}
resource registryImport 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (includePlatformOperatorGrants) {
  scope: registry
  name: guid(registry.id, operatorObjectId, registryPolicy.dataImporterRoleId)
  properties: {
    principalId: operatorObjectId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', registryPolicy.dataImporterRoleId)
    description: 'Trusted operator ARM import plus repository/catalog reads; no data-plane content or metadata writes.'
  }
}
