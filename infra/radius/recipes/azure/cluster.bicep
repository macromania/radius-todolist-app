targetScope = 'resourceGroup'

param context object
@description('One-entry operator-owned dictionary keyed by slot. The environment Azure provider scope must be that allocation clusterResourceGroup.')
param allocations object
param location string = resourceGroup().location
param tenantId string = subscription().tenantId
param kubernetesVersion string = '1.35.7'
param nodeVmSize string = 'Standard_D4s_v5'
@minValue(2)
param nodeCount int = 2
@minLength(1)
param authorizedIpRanges array
param tags object = {}

var slot = context.resource.properties.slot
var allocation = allocations[slot]
var requiredTags = union(tags, {
  SecurityControl: 'Ignore'
  project: 'radplanes'
  managedBy: 'radius-todolist-app'
  'radapp.io-environment': context.environment.id
  'radapp.io-application': context.application == null ? '' : context.application.id
  'radapp.io-resource': context.resource.id
})
var radiusAccounts = [
  'applications-rp'
  'bicep-de'
  'ucp'
  'dynamic-rp'
]

// Keep this native resource aligned with bootstrap/aks.bicep. Cross-RG modules
// compile to Azure deployment references, but Radius 0.60.2 scopes modules to its own plane.
resource cluster 'Microsoft.ContainerService/managedClusters@2025-05-01' = {
  name: allocation.clusterName
  location: location
  tags: requiredTags
  sku: {
    name: 'Base'
    tier: 'Free'
  }
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${allocation.identities.controlPlane.id}': {}
    }
  }
  properties: {
    kubernetesVersion: kubernetesVersion
    dnsPrefix: allocation.clusterName
    nodeResourceGroup: allocation.nodeResourceGroup
    enableRBAC: true
    disableLocalAccounts: true
    aadProfile: {
      managed: true
      enableAzureRBAC: true
      tenantID: tenantId
    }
    apiServerAccessProfile: {
      authorizedIPRanges: authorizedIpRanges
      enablePrivateCluster: false
    }
    identityProfile: {
      kubeletidentity: {
        resourceId: allocation.identities.kubelet.id
        clientId: allocation.identities.kubelet.clientId
        objectId: allocation.identities.kubelet.principalId
      }
    }
    oidcIssuerProfile: {
      enabled: true
    }
    securityProfile: {
      workloadIdentity: {
        enabled: true
      }
    }
    networkProfile: {
      networkPlugin: 'azure'
      networkPluginMode: 'overlay'
      networkDataplane: 'cilium'
      networkPolicy: 'cilium'
      loadBalancerSku: 'standard'
      outboundType: 'userAssignedNATGateway'
      podCidr: '192.168.0.0/16'
      serviceCidr: '172.20.0.0/16'
      dnsServiceIP: '172.20.0.10'
    }
    agentPoolProfiles: [
      {
        name: 'system'
        mode: 'System'
        type: 'VirtualMachineScaleSets'
        osType: 'Linux'
        osSKU: 'AzureLinux3'
        vmSize: nodeVmSize
        count: nodeCount
        maxPods: 110
        osDiskSizeGB: 64
        enableNodePublicIP: false
        vnetSubnetID: allocation.nodeSubnetId
        tags: requiredTags
      }
    ]
    autoUpgradeProfile: {
      upgradeChannel: 'none'
      nodeOSUpgradeChannel: 'NodeImage'
    }
  }
}

resource radiusIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = {
  name: last(split(allocation.identities.radius.id, '/'))
}
resource issuerIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = {
  name: last(split(allocation.identities.certificateIssuer.id, '/'))
}

// The identities and grants are bootstrap-owned. Only their bindings belong to this Recipe.
@batchSize(1)
resource radiusFederation 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2024-11-30' = [for account in radiusAccounts: {
  parent: radiusIdentity
  name: 'radius-${account}'
  properties: {
    issuer: cluster.properties.oidcIssuerProfile.issuerURL
    subject: 'system:serviceaccount:radius-system:${account}'
    audiences: [
      'api://AzureADTokenExchange'
    ]
  }
}]
resource issuerFederation 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2024-11-30' = {
  parent: issuerIdentity
  name: 'certificate-issuer'
  properties: {
    issuer: cluster.properties.oidcIssuerProfile.issuerURL
    subject: allocation.certificateIssuerSubject
    audiences: [
      'api://AzureADTokenExchange'
    ]
  }
}

var radiusFederationIds = [for (account, i) in radiusAccounts: radiusFederation[i].id]

output result object = {
  resources: concat([cluster.id], radiusFederationIds, [issuerFederation.id])
  values: {
    clusterId: cluster.id
    clusterName: cluster.name
    resourceGroup: allocation.clusterResourceGroup
    fqdn: cluster.properties.fqdn
    oidcIssuer: cluster.properties.oidcIssuerProfile.issuerURL
    bootstrapAccessRef: cluster.id
    radiusIdentityId: allocation.identities.radius.id
    radiusClientId: allocation.identities.radius.clientId
  }
}
