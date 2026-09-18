targetScope = 'subscription'

param base object
@minLength(10)
@maxLength(21)
param pairId string
@allowed([3, 5, 7, 9, 11, 13])
param allocationStart int
param operatorObjectId string
@minLength(6)
@maxLength(6)
param applicationCredentialNames array

var foundation = base.foundation
var prefix = foundation.resourcePrefix
var location = foundation.location
var rolePrefix = '${prefix}-${pairId}'
var slots = [
  '${pairId}-control'
  '${pairId}-data'
]
var tags = union(foundation.tags, {
  'plane-demo/environment-id': pairId
})
var managementRadius = base.allocations[0].identities.radius
var coordinator = base.coordinatorIdentity
var harness = foundation.harnessIdentity
var planePolicy = loadJsonContent('../../scripts/operations/azure/plane-policy.json')
var roleContracts = [
  {
    purpose: planePolicy.roles.postgresApplication.purpose
    name: planePolicy.roles.postgresApplication.name
    actions: concat(planePolicy.deploymentActions, planePolicy.gatewayActions, planePolicy.roles.postgresApplication.actions)
    slots: [slots[0]]
  }
  {
    purpose: planePolicy.roles.redisApplication.purpose
    name: planePolicy.roles.redisApplication.name
    actions: concat(planePolicy.deploymentActions, planePolicy.gatewayActions, planePolicy.roles.redisApplication.actions)
    slots: [slots[1]]
  }
  {
    purpose: 'child-cluster-recipe'
    name: 'child cluster recipe'
    actions: concat(planePolicy.deploymentActions, planePolicy.clusterActions)
    slots: slots
  }
  {
    purpose: 'child-identity-federation'
    name: 'child identity federation'
    actions: [
      'Microsoft.ManagedIdentity/userAssignedIdentities/read'
      'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials/read'
      'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials/write'
      'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials/delete'
    ]
    slots: slots
  }
]

resource groups 'Microsoft.Resources/resourceGroups@2024-03-01' = [for slot in slots: {
  name: 'rg-${prefix}-${slot}'
  location: location
  tags: tags
}]

resource roles 'Microsoft.Authorization/roleDefinitions@2022-04-01' = [for role in roleContracts: {
  name: guid(subscription().id, rolePrefix, role.purpose)
  properties: {
    roleName: '${rolePrefix} ${role.name}'
    description: 'Provision only this isolated environment without Azure role delegation.'
    type: 'CustomRole'
    assignableScopes: [for slot in role.slots: subscriptionResourceId('Microsoft.Resources/resourceGroups', 'rg-${prefix}-${slot}')]
    permissions: [
      {
        actions: role.actions
        notActions: []
        dataActions: []
        notDataActions: []
      }
    ]
  }
  dependsOn: [
    groups
  ]
}]

module identities './identities.bicep' = [for slot in slots: {
  name: 'identities-${slot}'
  scope: resourceGroup('rg-${prefix}-${slot}')
  params: {
    prefix: prefix
    slot: slot
    location: location
    tags: tags
  }
  dependsOn: [
    groups
  ]
}]

module network './isolated-network.bicep' = {
  name: 'network-${pairId}'
  scope: resourceGroup(foundation.platformResourceGroup)
  params: {
    prefix: prefix
    pairId: pairId
    allocationStart: allocationStart
    foundation: foundation
  }
}

module platformAccess './platform-access.bicep' = {
  name: 'platform-access-${pairId}'
  scope: resourceGroup(foundation.platformResourceGroup)
  params: {
    foundation: foundation
    allocations: [for (slot, i) in slots: union(network.outputs.allocations[i], {
      identities: identities[i].outputs.identity
    })]
    managementRadiusPrincipalId: managementRadius.principalId
    coordinatorPrincipalId: coordinator.principalId
    operatorObjectId: operatorObjectId
    includePlatformOperatorGrants: false
    includeCoordinatorPull: false
  }
}

module applicationAccess './resource-group-access.bicep' = [for (slot, i) in slots: {
  name: 'app-access-${slot}'
  scope: resourceGroup('rg-${prefix}-${slot}')
  params: {
    assignments: [
      {
        principalId: identities[i].outputs.identity.radius.principalId
        principalType: 'ServicePrincipal'
        roleDefinitionGuid: roles[i].name
      }
    ]
  }
  dependsOn: [
    groups
  ]
}]

var clusterUser = '4abbcc35-e782-43d8-92c5-2d3f1bd2253f'
var clusterAdmin = 'b1ff04bb-8a4e-4dc4-8eb5-8693973ce19b'
var reader = 'acdd72a7-3385-48ef-bd42-f606fba81ae7'
module clusterAccess './resource-group-access.bicep' = [for slot in slots: {
  name: 'cluster-access-${slot}'
  scope: resourceGroup('rg-${prefix}-${slot}')
  params: {
    assignments: concat(
      map([clusterUser, clusterAdmin], role => {
        principalId: operatorObjectId
        principalType: 'User'
        roleDefinitionGuid: role
      }),
      map([clusterUser, clusterAdmin], role => {
        principalId: coordinator.principalId
        principalType: 'ServicePrincipal'
        roleDefinitionGuid: role
      }),
      map([clusterUser, clusterAdmin, reader], role => {
        principalId: harness.principalId
        principalType: 'ServicePrincipal'
        roleDefinitionGuid: role
      }),
      [
        {
          principalId: managementRadius.principalId
          principalType: 'ServicePrincipal'
          roleDefinitionGuid: roles[2].name
        }
      ]
    )
  }
  dependsOn: [
    groups
  ]
}]

module identityAccess './tenant-identity-access.bicep' = [for (slot, i) in slots: {
  name: 'child-identity-access-${slot}'
  scope: resourceGroup('rg-${prefix}-${slot}')
  params: {
    identities: identities[i].outputs.identity
    managementRadiusPrincipalId: managementRadius.principalId
    federationRoleId: roles[3].id
  }
  dependsOn: [
    groups
  ]
}]

module deploymentRead './coordinator-bootstrap-read.bicep' = {
  name: '${prefix}-${pairId}-read'
  params: {
    bootstrapDeploymentName: deployment().name
    coordinatorPrincipalId: coordinator.principalId
  }
}

module gatewaySecrets './vault-object-access.json' = [for (slot, i) in slots: {
  name: 'kv-gateway-${foundation.deploymentHash}-${slot}'
  scope: resourceGroup(foundation.vaultResourceGroup)
  params: {
    objectScope: '${foundation.vaultId}/secrets/${network.outputs.allocations[i].certificateName}'
    principalId: identities[i].outputs.identity.gateway.principalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
  }
}]
module issuerCertificates './vault-object-access.json' = [for (slot, i) in slots: {
  name: 'kv-certificate-${foundation.deploymentHash}-${slot}'
  scope: resourceGroup(foundation.vaultResourceGroup)
  params: {
    objectScope: '${foundation.vaultId}/certificates/${network.outputs.allocations[i].certificateName}'
    principalId: identities[i].outputs.identity.certificateIssuer.principalId
    roleDefinitionId: foundation.roleDefinitionIds.certificateImporter
  }
}]
module issuerCertificateSecrets './vault-object-access.json' = [for (slot, i) in slots: {
  name: 'kv-cert-secret-${foundation.deploymentHash}-${slot}'
  scope: resourceGroup(foundation.vaultResourceGroup)
  params: {
    objectScope: '${foundation.vaultId}/secrets/${network.outputs.allocations[i].certificateName}'
    principalId: identities[i].outputs.identity.certificateIssuer.principalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
  }
}]
module issuerStateSecrets './vault-object-access.json' = [for (slot, i) in slots: {
  name: 'kv-acme-${foundation.deploymentHash}-${slot}'
  scope: resourceGroup(foundation.vaultResourceGroup)
  params: {
    objectScope: '${foundation.vaultId}/secrets/${network.outputs.allocations[i].acmeStateSecretName}'
    principalId: identities[i].outputs.identity.certificateIssuer.principalId
    roleDefinitionId: foundation.roleDefinitionIds.acmeStateWriter
  }
}]
module applicationCredentials './vault-object-access.json' = [for name in applicationCredentialNames: {
  name: 'kv-credential-${foundation.deploymentHash}-${uniqueString(name)}'
  scope: resourceGroup(foundation.vaultResourceGroup)
  params: {
    objectScope: '${foundation.vaultId}/secrets/${name}'
    principalId: coordinator.principalId
    roleDefinitionId: foundation.roleDefinitionIds.acmeStateWriter
  }
}]

output environment object = {
  pairId: pairId
  allocationStart: allocationStart
  projectName: foundation.projectName
  deploymentName: foundation.deploymentName
  subscriptionId: subscription().subscriptionId
  location: location
  environmentMode: 'prepared-v1'
  baseDeploymentId: subscriptionResourceId('Microsoft.Resources/deployments', '${prefix}-bootstrap')
  virtualNetworkId: foundation.virtualNetworkId
  registryId: foundation.registryId
  vaultId: foundation.vaultId
}
output allocations array = [for (slot, i) in slots: union(network.outputs.allocations[i], {
  namespace: '${prefix}-${slot}-${last(split(slot, '-'))}'
  clusterName: 'aks-${prefix}-${slot}'
  clusterResourceGroup: 'rg-${prefix}-${slot}'
  clusterResourceGroupId: subscriptionResourceId('Microsoft.Resources/resourceGroups', 'rg-${prefix}-${slot}')
  appResourceGroup: 'rg-${prefix}-${slot}'
  appResourceGroupId: subscriptionResourceId('Microsoft.Resources/resourceGroups', 'rg-${prefix}-${slot}')
  nodeResourceGroup: 'rg-${prefix}-${slot}-nodes'
  identities: identities[i].outputs.identity
  certificateIssuerSubject: 'system:serviceaccount:${prefix}-system:certificate-issuer'
  roleDefinitionPrefix: rolePrefix
})]
