param application string
param environment string
param image string
param provisionerImage string
param provisionerWorkloadIdentity bool = false
param provisionerClientId string = ''
param gatewayPhase string = 'challenge'
param certificateSecretUri string = ''

module api './workload.bicep' = {
  name: 'management-api'
  params: {
    application: application
    environment: environment
    name: 'management-api'
    image: image
    entrypoint: 'plane_demo.management_api'
    serviceAccount: 'management-api'
    runtimeSecretName: 'management-api-runtime'
    api: true
  }
}

module provisioner './workload.bicep' = {
  name: 'management-provisioner'
  params: {
    application: application
    environment: environment
    name: 'provisioner'
    image: provisionerImage
    entrypoint: 'plane_demo.provisioner'
    serviceAccount: 'provisioner'
    runtimeSecretName: 'provisioner-runtime'
    workloadIdentity: provisionerWorkloadIdentity
    automountToken: true
    recreate: true
    identityClientId: provisionerClientId
    volumes: [
      {
        name: 'provisioning'
        configMap: {
          name: 'provisioning-settings'
        }
      }
      {
        name: 'state'
        persistentVolumeClaim: {
          claimName: 'provisioner-state'
        }
      }
    ]
    mounts: [
      {
        name: 'provisioning'
        mountPath: '/etc/plane-demo'
        readOnly: true
      }
      {
        name: 'state'
        mountPath: '/app/.state'
      }
    ]
  }
}

module challenge './challenge.bicep' = {
  name: 'management-challenge'
  params: {
    application: application
    environment: environment
    image: image
  }
}

module gateway './gateway.bicep' = {
  name: 'management-gateway'
  params: {
    application: application
    environment: environment
    apiService: 'management-api'
    phase: gatewayPhase
    certificateSecretUri: certificateSecretUri
  }
}
