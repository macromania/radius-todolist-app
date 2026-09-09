param identities object
param managementRadiusPrincipalId string
param federationRoleId string

resource controlPlane 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = {
  name: last(split(identities.controlPlane.id, '/'))
}
resource kubelet 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = {
  name: last(split(identities.kubelet.id, '/'))
}
resource radius 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = {
  name: last(split(identities.radius.id, '/'))
}
resource issuer 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = {
  name: last(split(identities.certificateIssuer.id, '/'))
}
var identityOperatorRole = 'f1a07417-d97a-45cb-824c-7a7467783830'

resource controlPlaneAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: controlPlane
  name: guid(controlPlane.id, managementRadiusPrincipalId, identityOperatorRole)
  properties: {
    principalId: managementRadiusPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', identityOperatorRole)
  }
}
resource kubeletAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: kubelet
  name: guid(kubelet.id, managementRadiusPrincipalId, identityOperatorRole)
  properties: {
    principalId: managementRadiusPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', identityOperatorRole)
  }
}
resource radiusFederation 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: radius
  name: guid(radius.id, managementRadiusPrincipalId, federationRoleId)
  properties: {
    principalId: managementRadiusPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: federationRoleId
  }
}
resource issuerFederation 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: issuer
  name: guid(issuer.id, managementRadiusPrincipalId, federationRoleId)
  properties: {
    principalId: managementRadiusPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: federationRoleId
  }
}
