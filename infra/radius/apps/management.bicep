param application string
param environment string
param image string
param provisionerImage string
param provisionerEnabled bool = true
param ownershipLabels object = {
  'plane-demo/project': 'radplanes'
}
param provisionerWorkloadIdentity bool = false
param provisionerClientId string = ''
param gatewayPhase string = 'challenge'
param certificateSecretUri string = ''

module api '../modules/workload.bicep' = {
  name: 'management-api'
  params: {
    application: application
    environment: environment
    name: 'management-api'
    image: image
    ownershipLabels: ownershipLabels
    entrypoint: 'plane_demo.management.api'
    serviceAccount: 'management-api'
    runtimeSecretName: 'management-api-runtime'
    api: true
  }
}

module provisioner '../modules/workload.bicep' = if (provisionerEnabled) {
  name: 'management-provisioner'
  params: {
    application: application
    environment: environment
    name: 'provisioner'
    image: provisionerImage
    ownershipLabels: ownershipLabels
    entrypoint: 'plane_demo.management.provisioner'
    serviceAccount: 'provisioner'
    runtimeSecretName: 'provisioner-runtime'
    runtimeConfigMapName: 'provisioning-settings'
    workloadIdentity: provisionerWorkloadIdentity
    automountToken: true
    recreate: true
    identityClientId: provisionerClientId
  }
}

module challenge '../modules/challenge.bicep' = {
  name: 'management-challenge'
  params: {
    application: application
    environment: environment
    image: image
    ownershipLabels: ownershipLabels
  }
}

module gateway '../modules/gateway.bicep' = {
  name: 'management-gateway'
  params: {
    application: application
    environment: environment
    apiService: 'management-api'
    phase: gatewayPhase
    certificateSecretUri: certificateSecretUri
  }
}
