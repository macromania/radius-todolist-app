@description('Management-bootstrap AKS implementation. Keep the flat child cluster Recipe aligned; Radius 0.60.2 cannot resolve our cross-RG module references.')
param clusterName string
param location string
param kubernetesVersion string
param nodeVmSize string
@minValue(2)
param nodeCount int = 2
param nodeSubnetId string
param nodeResourceGroup string
param controlPlaneIdentityId string
param kubeletIdentity object
@minLength(1)
param authorizedIpRanges array
param tenantId string = subscription().tenantId
param tags object

resource cluster 'Microsoft.ContainerService/managedClusters@2025-05-01' = {
  name: clusterName
  location: location
  tags: tags
  sku: {
    name: 'Base'
    tier: 'Free'
  }
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${controlPlaneIdentityId}': {}
    }
  }
  properties: {
    kubernetesVersion: kubernetesVersion
    dnsPrefix: take(clusterName, 54)
    nodeResourceGroup: nodeResourceGroup
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
        resourceId: kubeletIdentity.id
        clientId: kubeletIdentity.clientId
        objectId: kubeletIdentity.principalId
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
        osDiskType: 'Managed'
        upgradeSettings: {
          maxSurge: '1'
        }
        enableNodePublicIP: false
        vnetSubnetID: nodeSubnetId
        tags: tags
      }
    ]
    autoUpgradeProfile: {
      upgradeChannel: 'none'
      nodeOSUpgradeChannel: 'NodeImage'
    }
  }
}

output clusterId string = cluster.id
output clusterName string = cluster.name
output fqdn string = cluster.properties.fqdn
output oidcIssuer string = cluster.properties.oidcIssuerProfile.issuerURL
output nodeResourceGroup string = nodeResourceGroup
