// Radius 0.60.2 requires tls:true on port 10000 and URL-encoded passwords.
// The Python consumer must use the resulting URL once or decode the standalone password once.
// @secure() result prevents listKeys values from leaking into Azure deployment history.
param context object
param location string = resourceGroup().location
param privateEndpointSubnetId string
param privateDnsZoneId string
param skuName string = 'Balanced_B0'
@allowed(['Enabled', 'Disabled'])
param highAvailability string = 'Disabled'
param tags object = {}

var cacheName = 'amr-${uniqueString(context.resource.id, resourceGroup().id)}'
var requiredTags = union(tags, {
  SecurityControl: 'Ignore'
  project: 'radplanes'
  managedBy: 'radius-todolist-app'
  'radapp.io-environment': context.environment.id
  'radapp.io-application': context.application == null ? '' : context.application.id
  'radapp.io-resource': context.resource.id
})

resource cache 'Microsoft.Cache/redisEnterprise@2025-07-01' = {
  name: cacheName
  location: location
  tags: requiredTags
  sku: {
    name: skuName
  }
  properties: {
    highAvailability: highAvailability
    minimumTlsVersion: '1.2'
    publicNetworkAccess: 'Disabled'
  }
}
resource database 'Microsoft.Cache/redisEnterprise/databases@2025-07-01' = {
  parent: cache
  name: 'default'
  properties: {
    clientProtocol: 'Encrypted'
    port: 10000
    clusteringPolicy: 'NoCluster'
    evictionPolicy: 'VolatileLRU'
    accessKeysAuthentication: 'Enabled'
  }
}
resource endpoint 'Microsoft.Network/privateEndpoints@2024-07-01' = {
  name: 'pe-${cacheName}'
  location: location
  tags: requiredTags
  properties: {
    customNetworkInterfaceName: 'nic-${cacheName}'
    subnet: {
      id: privateEndpointSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: 'redis'
        properties: {
          privateLinkServiceId: cache.id
          groupIds: [
            'redisEnterprise'
          ]
        }
      }
    ]
  }
}
resource endpointNic 'Microsoft.Network/networkInterfaces@2024-07-01' existing = {
  name: 'nic-${cacheName}'
  // Radius reads existing resources eagerly, including ones only used as tag scopes.
  dependsOn: [
    endpoint
  ]
}
resource endpointNicTags 'Microsoft.Resources/tags@2021-04-01' = {
  scope: endpointNic
  name: 'default'
  properties: {
    tags: requiredTags
  }
  dependsOn: [
    endpoint
  ]
}
resource zoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-07-01' = {
  parent: endpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'redis'
        properties: {
          privateDnsZoneId: privateDnsZoneId
        }
      }
    ]
  }
}

@secure()
output result object = {
  // Azure removes the database and endpoint-owned NIC, DNS group, and tags with these parents.
  resources: [
    cache.id
    endpoint.id
  ]
  values: {
    host: cache.properties.hostName
    port: 10000
    username: ''
    tls: true
  }
  secrets: {
    password: uriComponent(database.listKeys().primaryKey)
  }
}
