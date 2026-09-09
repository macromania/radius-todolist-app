param prefix string
param location string
param tags object

resource coordinator 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: 'id-${prefix}-coordinator'
  location: location
  tags: tags
}

output identity object = {
  id: coordinator.id
  clientId: coordinator.properties.clientId
  principalId: coordinator.properties.principalId
}
