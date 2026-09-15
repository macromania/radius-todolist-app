targetScope = 'subscription'

param bootstrapDeploymentName string
param coordinatorPrincipalId string

var reader = 'acdd72a7-3385-48ef-bd42-f606fba81ae7'

// Reference the existing parent deployment record; do not redeploy it or read the whole subscription.
#disable-next-line no-deployments-resources
resource bootstrapRecord 'Microsoft.Resources/deployments@2025-04-01' existing = {
  name: bootstrapDeploymentName
}

resource deploymentRead 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: bootstrapRecord
  name: guid(bootstrapRecord.id, coordinatorPrincipalId, reader)
  properties: {
    principalId: coordinatorPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', reader)
    description: 'Read only the selected bootstrap deployment and its outputs.'
  }
}
