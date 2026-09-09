param context object
param location string = resourceGroup().location
param delegatedSubnetId string
@description('Linked private zone ending in .postgres.database.azure.com; not a private-endpoint zone.')
param privateDnsZoneId string
param skuName string = 'Standard_D2ds_v5'
@allowed(['Burstable', 'GeneralPurpose', 'MemoryOptimized'])
param skuTier string = 'GeneralPurpose'
param administratorLogin string = 'plane_setup'
@description('Generated setup-only credential. Reapply may rotate it; never put it in readable environment Recipe parameters.')
@secure()
param administratorPassword string = 'A!${newGuid()}z9'
param tags object = {}

// Pinned by Radius 0.60.2 / Bicep 0.42.1, as configured in this directory.
extension kubernetes with {
  kubeConfig: ''
  namespace: context.runtime.kubernetes.namespace
} as kubernetes

var serverName = 'pg-${uniqueString(context.resource.id, resourceGroup().id)}'
var requiredTags = union(tags, {
  SecurityControl: 'Ignore'
  project: 'radplanes'
  managedBy: 'radius-todolist-app'
  'radapp.io-environment': context.environment.id
  'radapp.io-application': context.application == null ? '' : context.application.id
  'radapp.io-resource': context.resource.id
})

resource server 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: serverName
  location: location
  tags: requiredTags
  sku: {
    name: skuName
    tier: skuTier
  }
  properties: {
    version: '16'
    createMode: 'Default'
    administratorLogin: administratorLogin
    administratorLoginPassword: administratorPassword
    authConfig: {
      passwordAuth: 'Enabled'
      activeDirectoryAuth: 'Disabled'
    }
    network: {
      delegatedSubnetResourceId: delegatedSubnetId
      privateDnsZoneArmResourceId: privateDnsZoneId
      publicNetworkAccess: 'Disabled'
    }
    storage: {
      storageSizeGB: 32
      autoGrow: 'Enabled'
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
  }
}

resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: server
  name: context.resource.properties.databaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}
resource tls 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: server
  name: 'require_secure_transport'
  properties: {
    value: 'on'
    source: 'user-override'
  }
}

// Initialization only. The parent deletes this Secret after the Job and after any reapply.
resource setup 'core/Secret@v1' = {
  metadata: {
    name: '${context.resource.name}-setup'
    namespace: context.runtime.kubernetes.namespace
  }
  type: 'Opaque'
  stringData: {
    host: server.properties.fullyQualifiedDomainName
    port: '5432'
    database: database.name
    username: administratorLogin
    password: administratorPassword
  }
  dependsOn: [
    tls
  ]
}

@secure()
output result object = {
  resources: [
    server.id
    database.id
    tls.id
    '/planes/kubernetes/local/namespaces/${setup.metadata.namespace}/providers/core/Secret/${setup.metadata.name}'
  ]
  values: {
    host: server.properties.fullyQualifiedDomainName
    port: 5432
    database: database.name
    username: administratorLogin
    tlsRequired: true
    serverId: server.id
    setupSecretName: setup.metadata.name
  }
  secrets: {
    password: administratorPassword
  }
}
