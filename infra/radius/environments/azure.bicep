extension radius

@description('Azure subscription that Recipes deploy resources into.')
param azureSubscriptionId string

@description('Azure resource group that Recipes deploy resources into. Deliberately not the group holding the AKS cluster: Radius has Contributor here and must not be able to reconfigure or delete the cluster it runs on.')
param azureResourceGroup string = 'rg-todolist-app'

@description('Kubernetes namespace that application resources are deployed into.')
param namespace string = 'todolist-azure'

@description('Custom Recipe reference, pinned by digest. Never reference a mutable tag: Radius resolves this at deploy time and executes it with its Contributor identity.')
param redisRecipeRef string

@description('Resource ID of the subnet the Redis private endpoint is created in.')
param privateEndpointSubnetId string

@description('Resource ID of the privatelink.redis.azure.net private DNS zone.')
param privateDnsZoneId string

resource azure 'Applications.Core/environments@2023-10-01-preview' = {
  name: 'azure'
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
    recipes: {
      'Applications.Datastores/redisCaches': {
        default: {
          templateKind: 'bicep'
          templatePath: redisRecipeRef
          parameters: {
            skuName: 'Balanced_B0'
            highAvailability: 'Disabled'
            privateEndpointSubnetId: privateEndpointSubnetId
            privateDnsZoneId: privateDnsZoneId
          }
        }
      }
    }
  }
}
