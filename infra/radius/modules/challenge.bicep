param application string
param environment string
param image string

module challenge './workload.bicep' = {
  name: 'challenge-workload'
  params: {
    application: application
    environment: environment
    name: 'challenge'
    image: image
    entrypoint: 'plane_demo.setup.acme_responder'
    serviceAccount: 'challenge'
    api: true
    tcpProbe: true
    volumes: [
      {
        name: 'challenges'
        configMap: {
          name: 'acme-challenges'
        }
      }
    ]
    mounts: [
      {
        name: 'challenges'
        mountPath: '/challenges'
        readOnly: true
      }
    ]
  }
}
