param application string
param environment string
param image string
param gatewayPhase string = 'challenge'
param certificateSecretUri string = ''

module api '../modules/workload.bicep' = {
  name: 'control-api'
  params: {
    application: application
    environment: environment
    name: 'control-api'
    image: image
    entrypoint: 'plane_demo.control.api'
    serviceAccount: 'control-api'
    runtimeSecretName: 'control-api-runtime'
    api: true
  }
}

module reconciler '../modules/workload.bicep' = {
  name: 'control-reconciler'
  params: {
    application: application
    environment: environment
    name: 'control-reconciler'
    image: image
    entrypoint: 'plane_demo.control.reconciler'
    serviceAccount: 'control-reconciler'
    runtimeSecretName: 'control-reconciler-runtime'
  }
}

module challenge '../modules/challenge.bicep' = {
  name: 'control-challenge'
  params: {
    application: application
    environment: environment
    image: image
  }
}

module gateway '../modules/gateway.bicep' = {
  name: 'control-gateway'
  params: {
    application: application
    environment: environment
    apiService: 'control-api'
    phase: gatewayPhase
    certificateSecretUri: certificateSecretUri
  }
}
