extension radius
extension clusters

param application string
param environment string
param slot string

resource cluster 'Demo.Platform/clusters@2025-08-01-preview' = {
  name: slot
  properties: {
    application: application
    environment: environment
    slot: slot
  }
}

output clusterId string = cluster.id
