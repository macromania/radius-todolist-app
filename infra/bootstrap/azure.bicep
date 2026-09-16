targetScope = 'subscription'

@minLength(1)
@maxLength(16)
param projectName string
@minLength(1)
@maxLength(16)
param deploymentName string
@allowed(['azure'])
param environment string = 'azure'
param location string
@description('Deterministic global registry name derived from the selected deployment identity.')
@minLength(5)
@maxLength(50)
param registryName string
param registryExists bool = false
@minLength(3)
@maxLength(24)
param vaultName string
@description('Empty for a demo-owned vault. Otherwise the live-discovered resource group of an existing private RBAC vault in this subscription.')
param externalVaultResourceGroup string = ''
@minLength(20)
@maxLength(20)
param deploymentHash string
@description('Exact CredentialScope secret names for the five logical slots. Grants never target a whole vault.')
@minLength(15)
@maxLength(15)
param applicationCredentialNames array
@description('Validated bare public operator IPv4 address. The template authorizes only its /32, never a broad CIDR.')
@minLength(7)
@maxLength(15)
param operatorIp string
@description('Additional observed operator IPv4 addresses, each authorized as one /32.')
param additionalOperatorIps array = []
param operatorObjectId string
@description('Regional az aks get-versions probe on 2026-09-09 confirmed 1.35.7 in centralus.')
param kubernetesVersion string = '1.35.7'
@description('Operator-selected x64 size checked against current regional availability and quota before deployment.')
param nodeVmSize string
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
param coordinatorServiceAccountSubject string = 'system:serviceaccount:${projectName}-${deploymentName}-${environment}-management-management:provisioner'
param certificateIssuerServiceAccountSubject string = 'system:serviceaccount:${projectName}-${deploymentName}-${environment}-system:certificate-issuer'
param harnessServiceAccountSubject string = 'system:serviceaccount:${projectName}-${deploymentName}-${environment}-management-management:harness'
param tags object = {}

var prefix = '${projectName}-${deploymentName}-${environment}'
var vaultResourceGroup = empty(externalVaultResourceGroup) ? 'rg-${prefix}-platform' : externalVaultResourceGroup
var selectedVaultId = resourceId(subscription().subscriptionId, vaultResourceGroup, 'Microsoft.KeyVault/vaults', vaultName)
var operatorIpCidr = '${operatorIp}/32'
var operatorRanges = union([operatorIpCidr], map(additionalOperatorIps, ip => '${ip}/32'))
var requiredTags = union(tags, {
  SecurityControl: 'Ignore'
  project: projectName
  deployment: deploymentName
  environment: environment
  managedBy: 'radius-todolist-app'
})
var slots = concat(['management'], childSlots)
var planePolicy = loadJsonContent('../../scripts/operations/azure/plane-policy.json')
var reader = 'acdd72a7-3385-48ef-bd42-f606fba81ae7'
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
resource planeGroups 'Microsoft.Resources/resourceGroups@2024-03-01' = [for slot in slots: {
  name: 'rg-${prefix}-${slot}'
  location: location
  tags: requiredTags
}]

resource applicationRoles 'Microsoft.Authorization/roleDefinitions@2022-04-01' = [for role in items(planePolicy.roles): {
  name: guid(subscription().id, prefix, role.value.purpose)
  properties: {
    roleName: '${prefix} ${role.value.name}'
    description: 'Provision only the selected plane application resource types, without AKS, identity or role management.'
    type: 'CustomRole'
    assignableScopes: map(filter(slots, slot => endsWith(slot, '-data') == (role.key == 'redisApplication')), slot => subscriptionResourceId('Microsoft.Resources/resourceGroups', 'rg-${prefix}-${slot}'))
    permissions: [
      {
        actions: concat(planePolicy.deploymentActions, planePolicy.gatewayActions, role.value.actions)
        notActions: []
        dataActions: []
        notDataActions: []
      }
    ]
  }
  dependsOn: [
    planeGroups
  ]
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
      selectedVaultId
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
    network
  ]
}

resource acmeStateRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(subscription().id, prefix, 'acme-state-writer')
  properties: {
    roleName: '${prefix} ACME state writer'
    description: 'Read metadata, get, and set only an ACME state or application credential secret named by an exact object-scoped assignment.'
    type: 'CustomRole'
    assignableScopes: [
      subscriptionResourceId('Microsoft.Resources/resourceGroups', 'rg-${prefix}-platform')
      selectedVaultId
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
    network
  ]
}

resource clusterRecipeRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(subscription().id, prefix, 'child-cluster-recipe')
  properties: {
    roleName: '${prefix} child cluster recipe'
    description: 'Create Radius-owned child AKS and nested deployments, without identity writes or Azure role grants.'
    type: 'CustomRole'
    assignableScopes: [for slot in childSlots: subscriptionResourceId('Microsoft.Resources/resourceGroups', 'rg-${prefix}-${slot}')]
    permissions: [
      {
        actions: concat(planePolicy.deploymentActions, planePolicy.clusterActions)
        notActions: []
        dataActions: []
        notDataActions: []
      }
    ]
  }
  dependsOn: [
    planeGroups
  ]
}
resource federationRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(subscription().id, prefix, 'child-identity-federation')
  properties: {
    roleName: '${prefix} child identity federation'
    description: 'Bind service accounts on a preallocated identity; assigned only at individual identity scopes.'
    type: 'CustomRole'
    assignableScopes: [for slot in childSlots: subscriptionResourceId('Microsoft.Resources/resourceGroups', 'rg-${prefix}-${slot}')]
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
    planeGroups
  ]
}

// Bicep 0.42.1 misbinds copyIndex() in cross-scope references to RG resource collections.
// Keep explicit resourceGroup(name) scopes and group-creation dependencies on these modules.
module identity './identities.bicep' = [for slot in slots: {
  name: 'identities-${slot}'
  scope: resourceGroup('rg-${prefix}-${slot}')
  params: {
    prefix: prefix
    slot: slot
    location: location
    tags: requiredTags
  }
  dependsOn: [
    planeGroups
  ]
}]
module coordinator './coordinator.bicep' = {
  name: 'coordinator-identity'
  scope: resourceGroup('rg-${prefix}-management')
  params: {
    prefix: prefix
    location: location
    tags: requiredTags
  }
  dependsOn: [
    planeGroups
  ]
}
module harness './coordinator.bicep' = {
  name: 'harness-identity'
  scope: resourceGroup('rg-${prefix}-management')
  params: {
    prefix: prefix
    location: location
    tags: requiredTags
    purpose: 'harness'
  }
  dependsOn: [
    planeGroups
  ]
}
module coordinatorBootstrapRead './coordinator-bootstrap-read.bicep' = {
  name: '${prefix}-bootstrap-read'
  params: {
    bootstrapDeploymentName: '${prefix}-bootstrap'
    coordinatorPrincipalId: coordinator.outputs.identity.principalId
  }
}
module network './network.bicep' = {
  name: 'network'
  scope: resourceGroup('rg-${prefix}-platform')
  params: {
    prefix: prefix
    location: location
    registryName: registryName
    registryExists: registryExists
    vaultName: vaultName
    externalVaultResourceGroup: externalVaultResourceGroup
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
  }
  dependsOn: [
    platformGroup
  ]
}
module appAccess './resource-group-access.bicep' = [for (slot, i) in slots: {
  name: 'app-access-${slot}'
  scope: resourceGroup('rg-${prefix}-${slot}')
  params: {
    assignments: [
      {
        principalId: identity[i].outputs.identity.radius.principalId
        principalType: 'ServicePrincipal'
        roleDefinitionGuid: guid(subscription().id, prefix, endsWith(slot, '-data') ? planePolicy.roles.redisApplication.purpose : planePolicy.roles.postgresApplication.purpose)
      }
    ]
  }
  dependsOn: [
    planeGroups
    applicationRoles
  ]
}]
module clusterAccess './resource-group-access.bicep' = [for (slot, i) in slots: {
  name: 'cluster-access-${slot}'
  scope: resourceGroup('rg-${prefix}-${slot}')
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
      {
        principalId: harness.outputs.identity.principalId
        principalType: 'ServicePrincipal'
        roleDefinitionGuid: clusterUser
      }
      {
        principalId: harness.outputs.identity.principalId
        principalType: 'ServicePrincipal'
        roleDefinitionGuid: clusterAdmin
      }
      {
        principalId: harness.outputs.identity.principalId
        principalType: 'ServicePrincipal'
        roleDefinitionGuid: reader
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
    planeGroups
  ]
}]
module childIdentityAccess './tenant-identity-access.bicep' = [for (slot, i) in childSlots: {
  name: 'child-identity-access-${slot}'
  scope: resourceGroup('rg-${prefix}-${slot}')
  params: {
    identities: identity[i + 1].outputs.identity
    managementRadiusPrincipalId: identity[0].outputs.identity.radius.principalId
    federationRoleId: federationRole.id
  }
  dependsOn: [
    planeGroups
  ]
}]

module management './aks.bicep' = {
  name: 'management-cluster'
  scope: resourceGroup('rg-${prefix}-management')
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
    authorizedIpRanges: concat(operatorRanges, [
      '${network.outputs.foundation.egressIp}/32'
    ])
    tags: requiredTags
  }
  dependsOn: [
    planeGroups
    platformAccess
    clusterAccess
  ]
}
module managementRadiusFederation './federation.bicep' = {
  name: 'management-radius-federation'
  scope: resourceGroup('rg-${prefix}-management')
  params: {
    identityName: last(split(identity[0].outputs.identity.radius.id, '/'))
    issuer: management.outputs.oidcIssuer
    bindings: [for account in radiusAccounts: {
      name: 'radius-${account}'
      subject: 'system:serviceaccount:radius-system:${account}'
    }]
  }
  dependsOn: [
    planeGroups
  ]
}
module managementIssuerFederation './federation.bicep' = {
  name: 'management-issuer-federation'
  scope: resourceGroup('rg-${prefix}-management')
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
    planeGroups
  ]
}
module coordinatorFederation './federation.bicep' = {
  name: 'coordinator-federation'
  scope: resourceGroup('rg-${prefix}-management')
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
    planeGroups
  ]
}

module gatewaySecrets './vault-object-access.json' = [for (slot, i) in slots: {
  name: 'kv-gateway-${deploymentHash}-${slot}'
  scope: resourceGroup(vaultResourceGroup)
  params: {
    objectScope: '${selectedVaultId}/secrets/${network.outputs.allocations[i].certificateName}'
    principalId: identity[i].outputs.identity.gateway.principalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
  }
}]
module issuerCertificates './vault-object-access.json' = [for (slot, i) in slots: {
  name: 'kv-certificate-${deploymentHash}-${slot}'
  scope: resourceGroup(vaultResourceGroup)
  params: {
    objectScope: '${selectedVaultId}/certificates/${network.outputs.allocations[i].certificateName}'
    principalId: identity[i].outputs.identity.certificateIssuer.principalId
    roleDefinitionId: issuerRole.id
  }
}]
module issuerCertificateSecrets './vault-object-access.json' = [for (slot, i) in slots: {
  name: 'kv-cert-secret-${deploymentHash}-${slot}'
  scope: resourceGroup(vaultResourceGroup)
  params: {
    objectScope: '${selectedVaultId}/secrets/${network.outputs.allocations[i].certificateName}'
    principalId: identity[i].outputs.identity.certificateIssuer.principalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
  }
}]
module issuerStateSecrets './vault-object-access.json' = [for (slot, i) in slots: {
  name: 'kv-acme-${deploymentHash}-${slot}'
  scope: resourceGroup(vaultResourceGroup)
  params: {
    objectScope: '${selectedVaultId}/secrets/${network.outputs.allocations[i].acmeStateSecretName}'
    principalId: identity[i].outputs.identity.certificateIssuer.principalId
    roleDefinitionId: acmeStateRole.id
  }
}]
module applicationCredentials './vault-object-access.json' = [for name in applicationCredentialNames: {
  name: 'kv-credential-${deploymentHash}-${uniqueString(name)}'
  scope: resourceGroup(vaultResourceGroup)
  params: {
    objectScope: '${selectedVaultId}/secrets/${name}'
    principalId: coordinator.outputs.identity.principalId
    roleDefinitionId: acmeStateRole.id
  }
}]

module harnessPlatformAccess './resource-group-access.bicep' = {
  name: 'harness-platform-read'
  scope: resourceGroup('rg-${prefix}-platform')
  params: {
    assignments: [
      {
        principalId: harness.outputs.identity.principalId
        principalType: 'ServicePrincipal'
        roleDefinitionGuid: reader
      }
    ]
  }
  dependsOn: [
    platformGroup
  ]
}
module harnessFederation './federation.bicep' = {
  name: 'harness-federation'
  scope: resourceGroup('rg-${prefix}-management')
  params: {
    identityName: last(split(harness.outputs.identity.id, '/'))
    issuer: management.outputs.oidcIssuer
    bindings: [
      {
        name: 'demo-harness'
        subject: harnessServiceAccountSubject
      }
    ]
  }
  dependsOn: [
    planeGroups
  ]
}

output foundation object = union(network.outputs.foundation, {
  projectName: projectName
  deploymentName: deploymentName
  deploymentHash: deploymentHash
  environment: environment
  resourceGroupLayout: planePolicy.layout
  resourcePrefix: prefix
  radiusResourceGroup: prefix
  subscriptionId: subscription().subscriptionId
  tenantId: subscription().tenantId
  location: location
  platformResourceGroup: platformGroup.name
  kubernetesVersion: kubernetesVersion
  nodeVmSize: nodeVmSize
  nodeCount: nodeCount
  harnessIdentity: harness.outputs.identity
  roleDefinitionIds: {
    certificateImporter: issuerRole.id
    acmeStateWriter: acmeStateRole.id
    childClusterRecipe: clusterRecipeRole.id
    childIdentityFederation: federationRole.id
    postgresApplication: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', guid(subscription().id, prefix, planePolicy.roles.postgresApplication.purpose))
    redisApplication: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', guid(subscription().id, prefix, planePolicy.roles.redisApplication.purpose))
  }
  authorizedIpRanges: concat(operatorRanges, [
    '${network.outputs.foundation.egressIp}/32'
  ])
  tags: requiredTags
})
output allocations array = [for (slot, i) in slots: union(network.outputs.allocations[i], {
  namespace: '${prefix}-${slot}-${slot == 'management' ? 'management' : last(split(slot, '-'))}'
  clusterName: 'aks-${prefix}-${slot}'
  clusterResourceGroup: 'rg-${prefix}-${slot}'
  clusterResourceGroupId: subscriptionResourceId('Microsoft.Resources/resourceGroups', 'rg-${prefix}-${slot}')
  appResourceGroup: 'rg-${prefix}-${slot}'
  appResourceGroupId: subscriptionResourceId('Microsoft.Resources/resourceGroups', 'rg-${prefix}-${slot}')
  nodeResourceGroup: 'rg-${prefix}-${slot}-nodes'
  identities: identity[i].outputs.identity
  certificateIssuerSubject: certificateIssuerServiceAccountSubject
})]
output coordinatorIdentity object = coordinator.outputs.identity
output managementCluster object = {
  id: management.outputs.clusterId
  name: management.outputs.clusterName
  resourceGroup: 'rg-${prefix}-management'
  fqdn: management.outputs.fqdn
  oidcIssuer: management.outputs.oidcIssuer
}
