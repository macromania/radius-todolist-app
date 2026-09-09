param prefix string
param location string
param tags object
@allowed(['coordinator', 'harness'])
param purpose string = 'coordinator'

resource coordinator 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: 'id-${prefix}-${purpose}'
  location: location
  tags: tags
}

output identity object = {
  id: coordinator.id
  clientId: coordinator.properties.clientId
  principalId: coordinator.properties.principalId
}
