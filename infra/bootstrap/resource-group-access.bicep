@description('Trusted bootstrap only. Each entry has principalId, principalType and roleDefinitionGuid.')
param assignments array

resource grants 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for assignment in assignments: {
  name: guid(resourceGroup().id, assignment.principalId, assignment.roleDefinitionGuid)
  properties: {
    principalId: assignment.principalId
    principalType: assignment.principalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', assignment.roleDefinitionGuid)
  }
}]
