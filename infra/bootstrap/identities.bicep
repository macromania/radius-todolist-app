param location string
param prefix string
param slot string
param tags object

var purposes = [
  'control-plane'
  'kubelet'
  'radius'
  'gateway'
  'certificate-issuer'
]

resource identities 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = [for purpose in purposes: {
  name: 'id-${prefix}-${slot}-${purpose}'
  location: location
  tags: tags
}]

var identityOperatorRole = 'f1a07417-d97a-45cb-824c-7a7467783830'

// AKS must be able to assign the already-authorized kubelet identity without an ACR role grant.
resource kubeletOperator 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: identities[1]
  name: guid(identities[1].id, identities[0].id, identityOperatorRole)
  properties: {
    principalId: identities[0].properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', identityOperatorRole)
  }
}

resource gatewayOperator 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: identities[3]
  name: guid(identities[3].id, identities[2].id, identityOperatorRole)
  properties: {
    principalId: identities[2].properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', identityOperatorRole)
  }
}

output identity object = {
  controlPlane: {
    id: identities[0].id
    clientId: identities[0].properties.clientId
    principalId: identities[0].properties.principalId
  }
  kubelet: {
    id: identities[1].id
    clientId: identities[1].properties.clientId
    principalId: identities[1].properties.principalId
  }
  radius: {
    id: identities[2].id
    clientId: identities[2].properties.clientId
    principalId: identities[2].properties.principalId
  }
  gateway: {
    id: identities[3].id
    clientId: identities[3].properties.clientId
    principalId: identities[3].properties.principalId
  }
  certificateIssuer: {
    id: identities[4].id
    clientId: identities[4].properties.clientId
    principalId: identities[4].properties.principalId
  }
}
