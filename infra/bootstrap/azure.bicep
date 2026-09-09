targetScope = 'subscription'

@minLength(3)
@maxLength(12)
param projectName string = 'radplanes'
@description('Central US passed the parent preflight; East US 2 PostgreSQL is subscription-restricted.')
param location string = 'centralus'
@description('Run-unique non-secret naming salt. Retain for redeployments; change after vault teardown.')
param nameSalt string
@description('Validated bare public operator IPv4 address. The template authorizes only its /32, never a broad CIDR.')
@minLength(7)
@maxLength(15)
param operatorIp string
param operatorObjectId string
@description('Regional az aks get-versions probe on 2026-09-09 confirmed 1.35.7 in centralus.')
param kubernetesVersion string = '1.35.7'
param nodeVmSize string = 'Standard_D4s_v5'
@description('Current AKS system-pool guidance requires at least two nodes and four vCPUs per node.')
@minValue(2)
param nodeCount int = 2
@description('Append slots; never reorder an existing allocation. This is infrastructure capacity, not a tenant product limit.')
@minLength(1)
@maxLength(15)
param childSlots array = [
  'shared-control'
  'shared-data'
  'isolated-1-control'
  'isolated-1-data'
]
param coordinatorServiceAccountSubject string = 'system:serviceaccount:radplanes-management-management:provisioner'
param certificateIssuerServiceAccountSubject string = 'system:serviceaccount:radplanes-system:certificate-issuer'
param tags object = {}

var prefix = projectName
var operatorIpCidr = '${operatorIp}/32'
var requiredTags = union(tags, {
  SecurityControl: 'Ignore'
  project: 'radplanes'
  managedBy: 'radius-todolist-app'
})
var slots = concat(['management'], childSlots)
var contributor = 'b24988ac-6180-42a0-ab88-20f7382dd24c'
var clusterUser = '4abbcc35-e782-43d8-92c5-2d3f1bd2253f'
var clusterAdmin = 'b1ff04bb-8a4e-4dc4-8eb5-8693973ce19b'
var radiusAccounts = [
  'applications-rp'
  'bicep-de'
  'ucp'
  'dynamic-rp'
]

resource platformGroup 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${prefix}-platform'
  location: location
  tags: requiredTags
}
resource clusterGroups 'Microsoft.Resources/resourceGroups@2024-03-01' = [for slot in slots: {
  name: 'rg-${prefix}-${slot}-cluster'
  location: location
  tags: requiredTags
}]
resource appGroups 'Microsoft.Resources/resourceGroups@2024-03-01' = [for slot in slots: {
  name: 'rg-${prefix}-${slot}-app'
  location: location
  tags: requiredTags
}]

// Certificate and ACME state rights are separate and assigned only at exact object scopes.
resource issuerRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(subscription().id, prefix, 'certificate-importer')
  properties: {
    roleName: '${prefix} certificate importer'
    description: 'Read, import, and update only the certificate named by an object-scoped assignment.'
    type: 'CustomRole'
    assignableScopes: [
      subscriptionResourceId('Microsoft.Resources/resourceGroups', 'rg-${prefix}-platform')
    ]
    permissions: [
      {
        actions: []
        notActions: []
        dataActions: [
          'Microsoft.KeyVault/vaults/certificates/read'
          'Microsoft.KeyVault/vaults/certificates/import/action'
          'Microsoft.KeyVault/vaults/certificates/update/action'
        ]
        notDataActions: []
      }
    ]
  }
  dependsOn: [
    platformGroup
  ]
}

resource acmeStateRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(subscription().id, prefix, 'acme-state-writer')
  properties: {
    roleName: '${prefix} ACME state writer'
    description: 'Read and set only the ACME state secret named by an object-scoped assignment.'
    type: 'CustomRole'
    assignableScopes: [
      subscriptionResourceId('Microsoft.Resources/resourceGroups', 'rg-${prefix}-platform')
    ]
    permissions: [
      {
        actions: []
        notActions: []
        dataActions: [
          'Microsoft.KeyVault/vaults/secrets/readMetadata/action'
          'Microsoft.KeyVault/vaults/secrets/getSecret/action'
          'Microsoft.KeyVault/vaults/secrets/setSecret/action'
        ]
        notDataActions: []
      }
    ]
  }
  dependsOn: [
    platformGroup
  ]
}

resource clusterRecipeRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(subscription().id, prefix, 'child-cluster-recipe')
  properties: {
    roleName: '${prefix} child cluster recipe'
    description: 'Create Radius-owned child AKS and nested deployments, without identity writes or Azure role grants.'
    type: 'CustomRole'
    assignableScopes: [for slot in childSlots: subscriptionResourceId('Microsoft.Resources/resourceGroups', 'rg-${prefix}-${slot}-cluster')]
    permissions: [
      {
        actions: [
          'Microsoft.Resources/subscriptions/resourceGroups/read'
          'Microsoft.Resources/deployments/*'
          'Microsoft.ContainerService/managedClusters/*'
          'Microsoft.ManagedIdentity/userAssignedIdentities/read'
          'Microsoft.Authorization/*/read'
        ]
        notActions: []
        dataActions: []
        notDataActions: []
      }
    ]
  }
  dependsOn: [
    clusterGroups
  ]
}
resource federationRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(subscription().id, prefix, 'child-identity-federation')
  properties: {
    roleName: '${prefix} child identity federation'
    description: 'Bind service accounts on a preallocated identity; assigned only at individual identity scopes.'
    type: 'CustomRole'
    assignableScopes: [for slot in childSlots: subscriptionResourceId('Microsoft.Resources/resourceGroups', 'rg-${prefix}-${slot}-cluster')]
    permissions: [
      {
        actions: [
          'Microsoft.ManagedIdentity/userAssignedIdentities/read'
          'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials/read'
          'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials/write'
          'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials/delete'
        ]
        notActions: []
        dataActions: []
        notDataActions: []
      }
    ]
  }
  dependsOn: [
    clusterGroups
  ]
}

// Bicep 0.42.1 misbinds copyIndex() in cross-scope references to RG resource collections.
// Keep explicit resourceGroup(name) scopes and group-creation dependencies on these modules.
module identity './identities.bicep' = [for slot in slots: {
  name: 'identities-${slot}'
  scope: resourceGroup('rg-${prefix}-${slot}-cluster')
  params: {
    prefix: prefix
    slot: slot
    location: location
    tags: requiredTags
  }
  dependsOn: [
    clusterGroups
  ]
}]
module coordinator './coordinator.bicep' = {
  name: 'coordinator-identity'
  scope: resourceGroup('rg-${prefix}-management-cluster')
  params: {
    prefix: prefix
    location: location
    tags: requiredTags
  }
  dependsOn: [
    clusterGroups
  ]
}
module network './network.bicep' = {
  name: 'network'
  scope: resourceGroup('rg-${prefix}-platform')
  params: {
    prefix: prefix
    location: location
    nameSalt: nameSalt
    slots: slots
    tags: requiredTags
  }
  dependsOn: [
    platformGroup
  ]
}

module platformAccess './platform-access.bicep' = {
  name: 'platform-access'
  scope: resourceGroup('rg-${prefix}-platform')
  params: {
    foundation: network.outputs.foundation
    allocations: [for (slot, i) in slots: union(network.outputs.allocations[i], {
      identities: identity[i].outputs.identity
    })]
    coordinatorPrincipalId: coordinator.outputs.identity.principalId
    operatorObjectId: operatorObjectId
    certificateIssuerRoleId: issuerRole.id
    acmeStateRoleId: acmeStateRole.id
  }
  dependsOn: [
    platformGroup
  ]
}
module appAccess './resource-group-access.bicep' = [for (slot, i) in slots: {
  name: 'app-access-${slot}'
  scope: resourceGroup('rg-${prefix}-${slot}-app')
  params: {
    assignments: [
      {
        principalId: identity[i].outputs.identity.radius.principalId
        principalType: 'ServicePrincipal'
        roleDefinitionGuid: contributor
      }
    ]
  }
  dependsOn: [
    appGroups
  ]
}]
module clusterAccess './resource-group-access.bicep' = [for (slot, i) in slots: {
  name: 'cluster-access-${slot}'
  scope: resourceGroup('rg-${prefix}-${slot}-cluster')
  params: {
    assignments: concat([
      {
        principalId: operatorObjectId
        principalType: 'User'
        roleDefinitionGuid: clusterUser
      }
      {
        principalId: operatorObjectId
        principalType: 'User'
        roleDefinitionGuid: clusterAdmin
      }
      {
        principalId: coordinator.outputs.identity.principalId
        principalType: 'ServicePrincipal'
        roleDefinitionGuid: clusterUser
      }
      {
        principalId: coordinator.outputs.identity.principalId
        principalType: 'ServicePrincipal'
        roleDefinitionGuid: clusterAdmin
      }
    ], i == 0 ? [] : [
      {
        principalId: identity[0].outputs.identity.radius.principalId
        principalType: 'ServicePrincipal'
        roleDefinitionGuid: clusterRecipeRole.name
      }
    ])
  }
  dependsOn: [
    clusterGroups
  ]
}]
module childIdentityAccess './tenant-identity-access.bicep' = [for (slot, i) in childSlots: {
  name: 'child-identity-access-${slot}'
  scope: resourceGroup('rg-${prefix}-${slot}-cluster')
  params: {
    identities: identity[i + 1].outputs.identity
    managementRadiusPrincipalId: identity[0].outputs.identity.radius.principalId
    federationRoleId: federationRole.id
  }
  dependsOn: [
    clusterGroups
  ]
}]

module management './aks.bicep' = {
  name: 'management-cluster'
  scope: resourceGroup('rg-${prefix}-management-cluster')
  params: {
    clusterName: 'aks-${prefix}-management'
    location: location
    kubernetesVersion: kubernetesVersion
    nodeVmSize: nodeVmSize
    nodeCount: nodeCount
    nodeSubnetId: network.outputs.allocations[0].nodeSubnetId
    nodeResourceGroup: 'rg-${prefix}-management-nodes'
    controlPlaneIdentityId: identity[0].outputs.identity.controlPlane.id
    kubeletIdentity: identity[0].outputs.identity.kubelet
    authorizedIpRanges: [
      operatorIpCidr
      '${network.outputs.foundation.egressIp}/32'
    ]
    tags: requiredTags
  }
  dependsOn: [
    clusterGroups
    platformAccess
    clusterAccess
  ]
}
module managementRadiusFederation './federation.bicep' = {
  name: 'management-radius-federation'
  scope: resourceGroup('rg-${prefix}-management-cluster')
  params: {
    identityName: last(split(identity[0].outputs.identity.radius.id, '/'))
    issuer: management.outputs.oidcIssuer
    bindings: [for account in radiusAccounts: {
      name: 'radius-${account}'
      subject: 'system:serviceaccount:radius-system:${account}'
    }]
  }
  dependsOn: [
    clusterGroups
  ]
}
module managementIssuerFederation './federation.bicep' = {
  name: 'management-issuer-federation'
  scope: resourceGroup('rg-${prefix}-management-cluster')
  params: {
    identityName: last(split(identity[0].outputs.identity.certificateIssuer.id, '/'))
    issuer: management.outputs.oidcIssuer
    bindings: [
      {
        name: 'certificate-issuer'
        subject: certificateIssuerServiceAccountSubject
      }
    ]
  }
  dependsOn: [
    clusterGroups
  ]
}
module coordinatorFederation './federation.bicep' = {
  name: 'coordinator-federation'
  scope: resourceGroup('rg-${prefix}-management-cluster')
  params: {
    identityName: last(split(coordinator.outputs.identity.id, '/'))
    issuer: management.outputs.oidcIssuer
    bindings: [
      {
        name: 'management-provisioner'
        subject: coordinatorServiceAccountSubject
      }
    ]
  }
  dependsOn: [
    clusterGroups
  ]
}

output foundation object = union(network.outputs.foundation, {
  projectName: projectName
  subscriptionId: subscription().subscriptionId
  tenantId: subscription().tenantId
  location: location
  platformResourceGroup: platformGroup.name
  kubernetesVersion: kubernetesVersion
  nodeVmSize: nodeVmSize
  nodeCount: nodeCount
  roleDefinitionIds: {
    certificateImporter: issuerRole.id
    acmeStateWriter: acmeStateRole.id
    childClusterRecipe: clusterRecipeRole.id
    childIdentityFederation: federationRole.id
  }
  authorizedIpRanges: [
    operatorIpCidr
    '${network.outputs.foundation.egressIp}/32'
  ]
  tags: requiredTags
})
output allocations array = [for (slot, i) in slots: union(network.outputs.allocations[i], {
  clusterName: 'aks-${prefix}-${slot}'
  clusterResourceGroup: 'rg-${prefix}-${slot}-cluster'
  clusterResourceGroupId: subscriptionResourceId('Microsoft.Resources/resourceGroups', 'rg-${prefix}-${slot}-cluster')
  appResourceGroup: 'rg-${prefix}-${slot}-app'
  appResourceGroupId: subscriptionResourceId('Microsoft.Resources/resourceGroups', 'rg-${prefix}-${slot}-app')
  nodeResourceGroup: 'rg-${prefix}-${slot}-nodes'
  identities: identity[i].outputs.identity
  certificateIssuerSubject: certificateIssuerServiceAccountSubject
})]
output coordinatorIdentity object = coordinator.outputs.identity
output managementCluster object = {
  id: management.outputs.clusterId
  name: management.outputs.clusterName
  resourceGroup: 'rg-${prefix}-management-cluster'
  fqdn: management.outputs.fqdn
  oidcIssuer: management.outputs.oidcIssuer
}
