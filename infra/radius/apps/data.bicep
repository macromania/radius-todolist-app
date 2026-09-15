extension radius

param application string
param environment string
param image string
param ownershipLabels object = {
  'plane-demo/project': 'radplanes'
}
param gatewayPhase string = 'challenge'
param certificateSecretUri string = ''

resource redis 'Applications.Datastores/redisCaches@2023-10-01-preview' = {
  name: 'redis'
  properties: {
    application: application
    environment: environment
  }
}

module api '../modules/workload.bicep' = {
  name: 'data-api'
  params: {
    application: application
    environment: environment
    name: 'data-api'
    image: image
    ownershipLabels: ownershipLabels
    entrypoint: 'plane_demo.data.api'
    serviceAccount: 'data-api'
    runtimeServiceAccount: 'data-api-runtime'
    runtimeSecretName: 'data-api-runtime'
    api: true
    automountToken: true
    connections: {
      redis: {
        source: redis.id
      }
    }
  }
}

module reconciler '../modules/workload.bicep' = {
  name: 'data-reconciler'
  params: {
    application: application
    environment: environment
    name: 'data-reconciler'
    image: image
    ownershipLabels: ownershipLabels
    entrypoint: 'plane_demo.data.reconciler'
    serviceAccount: 'data-reconciler'
    runtimeSecretName: 'data-reconciler-runtime'
    automountToken: true
  }
}

module challenge '../modules/challenge.bicep' = {
  name: 'data-challenge'
  params: {
    application: application
    environment: environment
    image: image
    ownershipLabels: ownershipLabels
  }
}

module gateway '../modules/gateway.bicep' = {
  name: 'data-gateway'
  params: {
    application: application
    environment: environment
    apiService: 'data-api'
    phase: gatewayPhase
    certificateSecretUri: certificateSecretUri
  }
}
