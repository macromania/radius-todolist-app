extension radius

param environmentName string
param namespace string
param azureSubscriptionId string
param azureResourceGroup string
param recipes object
param registryHost string
param radiusClientId string
param azureTenantId string

resource registryAuth 'Applications.Core/secretStores@2023-10-01-preview' = {
  name: '${environmentName}-registry-auth'
  properties: {
    resource: 'radius-system/${environmentName}-registry-auth'
    type: 'azureWorkloadIdentity'
    data: {
      clientId: {
        value: radiusClientId
      }
      tenantId: {
        value: azureTenantId
      }
    }
  }
}

resource environment 'Applications.Core/environments@2023-10-01-preview' = {
  name: environmentName
  properties: {
    compute: {
      kind: 'kubernetes'
      resourceId: 'self'
      namespace: namespace
    }
    providers: {
      azure: {
        scope: '/subscriptions/${azureSubscriptionId}/resourceGroups/${azureResourceGroup}'
      }
    }
    recipes: recipes
    recipeConfig: {
      bicep: {
        authentication: {
          '${registryHost}': {
            secret: registryAuth.id
          }
        }
      }
    }
  }
}
