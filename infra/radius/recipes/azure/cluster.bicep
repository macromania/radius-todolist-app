param context object
@description('Operator-owned dictionary keyed by slot. Build it from bootstrap allocations; never accept cloud IDs from the public API.')
param allocations object
param location string = resourceGroup().location
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

module cluster '../../../bootstrap/aks.bicep' = {
  name: 'radius-cluster-${uniqueString(context.resource.id)}'
  scope: resourceGroup(allocation.clusterResourceGroup)
  params: {
    clusterName: allocation.clusterName
    location: location
    kubernetesVersion: kubernetesVersion
    nodeVmSize: nodeVmSize
    nodeCount: nodeCount
    nodeSubnetId: allocation.nodeSubnetId
    nodeResourceGroup: allocation.nodeResourceGroup
    controlPlaneIdentityId: allocation.identities.controlPlane.id
    kubeletIdentity: allocation.identities.kubelet
    authorizedIpRanges: authorizedIpRanges
    tags: requiredTags
  }
}

// Identities and role assignments already exist. These bindings grant no new Azure roles.
module radiusFederation '../../../bootstrap/federation.bicep' = {
  name: 'radius-federation-${uniqueString(context.resource.id)}'
  scope: resourceGroup(allocation.clusterResourceGroup)
  params: {
    identityName: last(split(allocation.identities.radius.id, '/'))
    issuer: cluster.outputs.oidcIssuer
    bindings: [for account in radiusAccounts: {
      name: 'radius-${account}'
      subject: 'system:serviceaccount:radius-system:${account}'
    }]
  }
}
module issuerFederation '../../../bootstrap/federation.bicep' = {
  name: 'issuer-federation-${uniqueString(context.resource.id)}'
  scope: resourceGroup(allocation.clusterResourceGroup)
  params: {
    identityName: last(split(allocation.identities.certificateIssuer.id, '/'))
    issuer: cluster.outputs.oidcIssuer
    bindings: [
      {
        name: 'certificate-issuer'
        subject: allocation.certificateIssuerSubject
      }
    ]
  }
}

output result object = {
  resources: concat([cluster.outputs.clusterId], radiusFederation.outputs.resourceIds, issuerFederation.outputs.resourceIds)
  values: {
    clusterId: cluster.outputs.clusterId
    clusterName: cluster.outputs.clusterName
    resourceGroup: allocation.clusterResourceGroup
    fqdn: cluster.outputs.fqdn
    oidcIssuer: cluster.outputs.oidcIssuer
    bootstrapAccessRef: cluster.outputs.clusterId
    radiusIdentityId: allocation.identities.radius.id
    radiusClientId: allocation.identities.radius.clientId
  }
}
