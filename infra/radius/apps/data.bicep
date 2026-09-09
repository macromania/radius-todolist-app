extension radius

param application string
param environment string
param image string
param gatewayPhase string = 'challenge'
param certificateSecretUri string = ''

resource redis 'Applications.Datastores/redisCaches@2023-10-01-preview' = {
  name: 'redis'
  properties: {
    application: application
    environment: environment
  }
}

module api './workload.bicep' = {
  name: 'data-api'
  params: {
    application: application
    environment: environment
    name: 'data-api'
    image: image
    entrypoint: 'plane_demo.data_api'
    serviceAccount: 'data-api'
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

module reconciler './workload.bicep' = {
  name: 'data-reconciler'
  params: {
    application: application
    environment: environment
    name: 'data-reconciler'
    image: image
    entrypoint: 'plane_demo.data_reconciler'
    serviceAccount: 'data-reconciler'
    runtimeSecretName: 'data-reconciler-runtime'
    automountToken: true
  }
}

module challenge './challenge.bicep' = {
  name: 'data-challenge'
  params: {
    application: application
    environment: environment
    image: image
  }
}

module gateway './gateway.bicep' = {
  name: 'data-gateway'
  params: {
    application: application
    environment: environment
    apiService: 'data-api'
    phase: gatewayPhase
    certificateSecretUri: certificateSecretUri
  }
}
