param prefix string
param pairId string
@minValue(3)
@maxValue(13)
param allocationStart int
param foundation object

resource vnet 'Microsoft.Network/virtualNetworks@2024-07-01' existing = {
  name: foundation.virtualNetworkName
}
resource nat 'Microsoft.Network/natGateways@2024-07-01' existing = {
  name: 'nat-${prefix}'
}
resource gatewayNsg 'Microsoft.Network/networkSecurityGroups@2024-07-01' existing = {
  name: 'nsg-${prefix}-gateways'
}
var slots = [
  '${pairId}-control'
  '${pairId}-data'
]

resource nodes 'Microsoft.Network/virtualNetworks/subnets@2024-07-01' = [for (slot, i) in slots: {
  parent: vnet
  name: 'snet-${slot}-nodes'
  properties: {
    addressPrefix: '10.64.${allocationStart + i}.0/24'
    natGateway: {
      id: nat.id
    }
  }
}]
resource gateways 'Microsoft.Network/virtualNetworks/subnets@2024-07-01' = [for (slot, i) in slots: {
  parent: vnet
  name: 'snet-${slot}-gateway'
  properties: {
    addressPrefix: '10.64.${16 + allocationStart + i}.0/24'
    networkSecurityGroup: {
      id: gatewayNsg.id
    }
  }
}]
resource endpoints 'Microsoft.Network/virtualNetworks/subnets@2024-07-01' = [for (slot, i) in slots: {
  parent: vnet
  name: 'snet-${slot}-endpoints'
  properties: {
    addressPrefix: '10.64.${32 + allocationStart + i}.0/27'
    privateEndpointNetworkPolicies: 'Disabled'
  }
}]
resource postgres 'Microsoft.Network/virtualNetworks/subnets@2024-07-01' = [for (slot, i) in slots: {
  parent: vnet
  name: 'snet-${slot}-postgresql'
  properties: {
    addressPrefix: '10.64.${48 + allocationStart + i}.0/27'
    delegations: [
      {
        name: 'postgresql'
        properties: {
          serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers'
        }
      }
    ]
  }
}]

output allocations array = [for (slot, i) in slots: {
  slot: slot
  slotIndex: allocationStart + i
  certificateName: 'gateway-${prefix}-${slot}'
  acmeStateSecretName: 'acme-${prefix}-${slot}'
  nodeSubnetId: nodes[i].id
  nodeSubnetName: nodes[i].name
  gatewaySubnetId: gateways[i].id
  gatewaySubnetCidr: gateways[i].properties.addressPrefix
  privateEndpointSubnetId: endpoints[i].id
  postgresqlSubnetId: postgres[i].id
  apiPrivateIp: '10.64.${allocationStart + i}.240'
  challengePrivateIp: '10.64.${allocationStart + i}.241'
}]
