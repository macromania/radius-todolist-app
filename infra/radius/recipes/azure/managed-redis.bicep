// Radius Recipe: Azure Managed Redis (Microsoft.Cache/redisEnterprise) for
// Applications.Datastores/redisCaches.
//
// SCOPE: specific to Radius 0.60 and to the radius-project demo sample, because
// of the password encoding described below. Re-verify on any Radius upgrade.
//
// Why not the Recipe Radius ships? ghcr.io/radius-project/recipes/azure/rediscaches
// creates Microsoft.Cache/redis (Azure Cache for Redis), which is being retired:
// creation is blocked for new customers from 2026-04-01 and the Basic, Standard
// and Premium tiers retire 2028-09-30. The target subscription has no existing
// instances, so it very likely cannot create one at all.
//
// Three things here are easy to get wrong and each produces a deployment that
// looks healthy and fails later, so each is explained rather than left to the
// reader. All three were verified against a real Azure Managed Redis instance.
//
// 1. `tls: true` must be emitted explicitly. Radius otherwise infers TLS from
//    `port == 6380` (see computeSSL in pkg/datastoresrp/processors/rediscaches),
//    and this service uses port 10000, so the inferred value would be false and
//    Radius would build a plaintext redis:// URL against a TLS endpoint.
//    Measured against real Azure Managed Redis: rediss:// -> /healthz 200,
//    redis:// -> /healthz 500.
//
// 2. The password is percent-encoded. Radius concatenates it into a URL without
//    encoding, and Azure access keys are 44 characters of base64, which contains
//    '/' roughly half the time. An unencoded '/' terminates the URL authority
//    and the Redis client constructor throws "TypeError: Invalid URL".
//    Consumers therefore receive CONNECTION_..._PASSWORD percent-encoded, which
//    is surprising and is why it is written down here. A future Radius that
//    encodes the password itself would double-encode; that is what the upgrade
//    check exists for.
//
// 3. The result is @secure() so the access key is not retained in Azure
//    deployment history, where any Reader on the resource group could read it
//    without holding permission to call listKeys. Verified that Radius 0.60
//    consumes a secureObject result correctly. Note this needs the Bicep
//    compiler bundled with rad (0.42.1), which emits languageVersion 2.0; the
//    older az bicep 0.24.24 rejects it with BCP129.

@description('Radius-provided object describing the resource calling this Recipe.')
param context object

@description('Azure region. Defaults to the resource group region.')
param location string = resourceGroup().location

@description('Azure Managed Redis SKU. Balanced_B0 is the smallest, 0.5 GB.')
param skuName string = 'Balanced_B0'

@description('Enabled for production. Disabled halves the cost and is dev only.')
@allowed([
  'Enabled'
  'Disabled'
])
param highAvailability string = 'Disabled'

@description('Resource ID of the subnet the private endpoint is created in.')
param privateEndpointSubnetId string

@description('Resource ID of the privatelink.redis.azure.net private DNS zone.')
param privateDnsZoneId string

@description('User-defined tags merged with the Radius tracking tags.')
param tags object = {}

var radiusTags = {
  'radapp.io-environment': context.environment.id
  'radapp.io-application': context.application == null ? '' : context.application.id
  'radapp.io-resource': context.resource.id
}

var cacheName = 'amr-${uniqueString(context.resource.id, resourceGroup().id)}'

resource amr 'Microsoft.Cache/redisEnterprise@2025-07-01' = {
  name: cacheName
  location: location
  tags: union(tags, radiusTags)
  sku: {
    name: skuName
  }
  properties: {
    highAvailability: highAvailability
    minimumTlsVersion: '1.2'
    // No public endpoint at any point in this resource's life. Reaching it
    // requires the private endpoint below.
    publicNetworkAccess: 'Disabled'
  }
}

// The database must be named exactly 'default'; Azure rejects anything else.
// clusteringPolicy is NoCluster because OSS cluster mode additionally opens the
// 85xx port range, which complicates network rules for no benefit at this size.
// It is also immutable after creation except to and from NoCluster, so getting
// it wrong means deleting the cache and losing the data.
resource amrDatabase 'Microsoft.Cache/redisEnterprise/databases@2025-07-01' = {
  parent: amr
  name: 'default'
  properties: {
    clientProtocol: 'Encrypted'
    port: 10000
    clusteringPolicy: 'NoCluster'
    evictionPolicy: 'VolatileLRU'
    accessKeysAuthentication: 'Enabled'
  }
}

resource privateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'pe-${cacheName}'
  location: location
  tags: union(tags, radiusTags)
  properties: {
    subnet: {
      id: privateEndpointSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: 'plsc-${cacheName}'
        properties: {
          privateLinkServiceId: amr.id
          groupIds: [
            'redisEnterprise'
          ]
        }
      }
    ]
  }
}

// The zone must be privatelink.redis.azure.net. The legacy Azure Cache for
// Redis Enterprise offering shares both this resource type and the
// 'redisEnterprise' group ID but uses privatelink.redisenterprise.cache.azure.net.
// Picking that one silently breaks name resolution.
resource privateDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: privateEndpoint
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
  values: {
    host: amr.properties.hostName
    port: 10000
    username: ''
    tls: true
  }
  secrets: {
    password: uriComponent(amrDatabase.listKeys().primaryKey)
  }
}
