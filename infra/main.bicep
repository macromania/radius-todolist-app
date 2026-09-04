// Platform infrastructure for radius-todolist-app.
//
// This template creates everything that is not owned by Radius: the network,
// the private DNS zone that makes the private endpoint resolvable, log storage,
// and the AKS cluster itself.
//
// It deliberately does NOT create the Azure Managed Redis instance. That is
// created by the Radius Recipe in recipes/azure-managed-redis.bicep, into a
// separate resource group (rg-todolist-app), because Radius holds Contributor
// on that group and must not be able to reconfigure or delete the cluster it
// runs on.

targetScope = 'resourceGroup'

@description('Azure region for all platform resources.')
param location string = resourceGroup().location

@description('Name of the AKS cluster.')
param clusterName string = 'aks-todolist'

@description('Name of the virtual network.')
param vnetName string = 'vnet-todolist'

@description('Entra object ID of the human operator who administers the cluster.')
param operatorObjectId string

@description('Entra tenant ID.')
param tenantId string = subscription().tenantId

@description('Kubernetes version.')
param kubernetesVersion string = '1.35'

@description('VM size for both node pools.')
param nodeVmSize string = 'Standard_D2s_v5'

@description('Node count for the system pool.')
param systemNodeCount int = 2

@description('Minimum and maximum node count for the autoscaling user pool.')
param userNodeMin int = 2
param userNodeMax int = 4

// Availability zones are immutable after node pool creation, so this has to be
// right the first time. Do not assume all three exist: AKS preflight rejected
// zone '1' in eastus2 for this subscription with "The supported zones for
// location 'eastus2' are '3,2'", even though `az vm list-skus` lists 1, 2 and 3
// for the VM size. Check with a deployment what-if before changing region.
@description('Availability zones for both node pools.')
param availabilityZones array = [
  '2'
  '3'
]

@description('Tags applied to every resource here.')
param tags object = {
  project: 'radius-todolist-app'
  managedBy: 'infra/main.bicep'
}

// ---------------------------------------------------------------- networking

// Address plan. The node subnet holds AKS nodes. Pod addresses come from the
// overlay pod CIDR and are not part of this virtual network, which is the point
// of overlay mode: the node subnet does not have to be large.
var vnetCidr = '10.42.0.0/16'
var nodeSubnetCidr = '10.42.0.0/22'
var privateLinkSubnetCidr = '10.42.4.0/24'

// These two are overlay ranges and must not overlap the virtual network.
var podCidr = '192.168.0.0/16'
var serviceCidr = '172.16.0.0/16'
var dnsServiceIp = '172.16.0.10'

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: vnetName
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        vnetCidr
      ]
    }
    subnets: [
      {
        name: 'snet-nodes'
        properties: {
          addressPrefix: nodeSubnetCidr
        }
      }
      {
        name: 'snet-privatelink'
        properties: {
          addressPrefix: privateLinkSubnetCidr
          // Required so a private endpoint can be created in this subnet by a
          // principal that does not own the whole network.
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}

resource nodeSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' existing = {
  parent: vnet
  name: 'snet-nodes'
}

resource privateLinkSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' existing = {
  parent: vnet
  name: 'snet-privatelink'
}

// The zone name must be privatelink.redis.azure.net for Azure Managed Redis.
// The legacy Azure Cache for Redis Enterprise offering shares the same resource
// type and private endpoint group ID but uses a different zone
// (privatelink.redisenterprise.cache.azure.net); using that one here would
// silently break name resolution.
resource redisPrivateDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.redis.azure.net'
  location: 'global'
  tags: tags
}

resource redisDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: redisPrivateDnsZone
  name: 'link-${vnetName}'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

// ---------------------------------------------------------------- monitoring

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2025-02-01' = {
  name: 'log-todolist'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

// ------------------------------------------------------------------ identity

// User-assigned rather than system-assigned. A system-assigned identity does
// not exist until the cluster exists, so role assignments referencing it cannot
// be made in the same Bicep pass.
resource clusterIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: 'id-${clusterName}'
  location: location
  tags: tags
}

// The cluster identity needs to manage the node subnet it is given.
var networkContributorRoleId = '4d97b98b-1d4f-4787-a291-c67834d212e7'

resource identityNetworkContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vnet
  name: guid(vnet.id, clusterIdentity.id, networkContributorRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', networkContributorRoleId)
    principalId: clusterIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ----------------------------------------------------------------------- aks

resource aks 'Microsoft.ContainerService/managedClusters@2026-06-01' = {
  name: clusterName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${clusterIdentity.id}': {}
    }
  }
  properties: {
    kubernetesVersion: kubernetesVersion
    dnsPrefix: clusterName
    enableRBAC: true

    // Entra ID for authentication, Azure RBAC for authorization, and no local
    // certificate-based admin accounts. Reaching this cluster therefore
    // requires kubelogin plus an Azure role assignment (granted below).
    disableLocalAccounts: true
    aadProfile: {
      managed: true
      enableAzureRBAC: true
      tenantID: tenantId
    }

    // Both must be set, and together: the OIDC issuer is what Milestone 6
    // federates the Radius service accounts against.
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
      // Must be set explicitly. Setting networkPlugin alone silently selects
      // the legacy node-subnet mode for backward compatibility.
      networkPluginMode: 'overlay'
      networkPolicy: 'azure'
      loadBalancerSku: 'standard'
      outboundType: 'loadBalancer'
      podCidr: podCidr
      serviceCidr: serviceCidr
      dnsServiceIP: dnsServiceIp
    }

    agentPoolProfiles: [
      {
        name: 'system'
        mode: 'System'
        osType: 'Linux'
        osSKU: 'AzureLinux3'
        vmSize: nodeVmSize
        count: systemNodeCount
        vnetSubnetID: nodeSubnet.id
        availabilityZones: availabilityZones
        // Keep application workloads off the system pool.
        nodeTaints: [
          'CriticalAddonsOnly=true:NoSchedule'
        ]
      }
      {
        name: 'user'
        mode: 'User'
        osType: 'Linux'
        osSKU: 'AzureLinux3'
        vmSize: nodeVmSize
        enableAutoScaling: true
        minCount: userNodeMin
        maxCount: userNodeMax
        count: userNodeMin
        vnetSubnetID: nodeSubnet.id
        availabilityZones: availabilityZones
      }
    ]

    // Container Insights. omsagent is current, not deprecated: what retired in
    // 2024 was the standalone Log Analytics VM extension, a different thing.
    // useAADAuth avoids the workspace key.
    //
    // Managed Prometheus is deliberately not enabled here. It needs an Azure
    // Monitor Workspace plus data collection endpoint, rule and association,
    // which is a meaningful amount of extra surface and cost for a spike whose
    // subject is Radius and Redis, not observability. Container Insights gives
    // the application logs this project actually needs.
    addonProfiles: {
      omsagent: {
        enabled: true
        config: {
          logAnalyticsWorkspaceResourceID: logAnalytics.id
          useAADAuth: 'true'
        }
      }
    }
  }
  dependsOn: [
    identityNetworkContributor
  ]
}

// Creating a cluster does not grant permission to use it. With Azure RBAC
// enabled, kubectl needs an explicit role assignment or every command returns
// "nodes is forbidden".
var aksRbacClusterAdminRoleId = 'b1ff04bb-8a4e-4dc4-8eb5-8693973ce19b'

resource operatorClusterAdmin 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: aks
  name: guid(aks.id, operatorObjectId, aksRbacClusterAdminRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', aksRbacClusterAdminRoleId)
    principalId: operatorObjectId
    principalType: 'User'
  }
}

// ------------------------------------------------------------------- outputs

output clusterName string = aks.name
output oidcIssuerUrl string = aks.properties.oidcIssuerProfile.issuerURL
output privateEndpointSubnetId string = privateLinkSubnet.id
output redisPrivateDnsZoneId string = redisPrivateDnsZone.id
output vnetId string = vnet.id
