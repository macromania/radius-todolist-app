extension radius

param application string
param environment string
param name string
param image string
param entrypoint string
param serviceAccount string
param runtimeServiceAccount string = serviceAccount
param runtimeSecretName string = ''
param runtimeConfigMapName string = ''
param settings object = {}
param ownershipLabels object = {
  'plane-demo/project': 'radplanes'
}
param volumes array = []
param mounts array = []
param connections object = {}
param workloadIdentity bool = false
param automountToken bool = false
param api bool = false
param tcpProbe bool = false
param recreate bool = false
param identityClientId string = ''

var accountBase = {
  apiVersion: 'v1'
  kind: 'ServiceAccount'
  metadata: {
    name: serviceAccount
    annotations: empty(identityClientId) ? {} : {
      'azure.workload.identity/client-id': identityClientId
    }
  }
}
var deploymentBase = {
  apiVersion: 'apps/v1'
  kind: 'Deployment'
  metadata: {
    name: name
  }
  spec: {
    template: {
      spec: {
        serviceAccountName: serviceAccount
      }
    }
    strategy: {
      type: 'Recreate'
    }
  }
}

resource workload 'Applications.Core/containers@2023-10-01-preview' = {
  name: name
  properties: {
    application: application
    environment: environment
    container: union({
      image: image
      command: [
        'python'
        '-m'
        entrypoint
      ]
      env: union({
        LISTEN_PORT: {
          value: '8088'
        }
      }, settings)
      ports: api ? {
        http: {
          containerPort: 8088
        }
      } : {}
    }, api ? {
      livenessProbe: union({
        kind: tcpProbe ? 'tcp' : 'httpGet'
        containerPort: 8088
        initialDelaySeconds: 5
        periodSeconds: 20
        failureThreshold: 3
        timeoutSeconds: 5
      }, tcpProbe ? {} : {
        path: '/livez'
      })
    } : {})
    connections: connections
    extensions: [
      {
        kind: 'manualScaling'
        replicas: 1
      }
      {
        kind: 'kubernetesMetadata'
        labels: union(ownershipLabels, {
          'azure.workload.identity/use': workloadIdentity ? 'true' : 'false'
          'plane-demo/component': name
        })
      }
    ]
    runtimes: {
      kubernetes: {
        base: recreate ? '${string(accountBase)}\n---\n${string(deploymentBase)}' : string(accountBase)
        pod: {
          serviceAccountName: runtimeServiceAccount
          automountServiceAccountToken: automountToken
          securityContext: {
            runAsNonRoot: true
            runAsUser: 10001
            runAsGroup: 10001
            fsGroup: 10001
            fsGroupChangePolicy: 'OnRootMismatch'
            seccompProfile: {
              type: 'RuntimeDefault'
            }
          }
          containers: [
            {
              name: name
              envFrom: concat(empty(runtimeSecretName) ? [] : [
                {
                  secretRef: {
                    name: runtimeSecretName
                  }
                }
              ], empty(runtimeConfigMapName) ? [] : [
                {
                  configMapRef: {
                    name: runtimeConfigMapName
                  }
                }
              ])
              volumeMounts: mounts
              resources: {
                requests: {
                  cpu: '100m'
                  memory: '128Mi'
                }
                limits: {
                  cpu: '1'
                  memory: '512Mi'
                }
              }
              securityContext: {
                allowPrivilegeEscalation: false
                capabilities: {
                  drop: [
                    'ALL'
                  ]
                }
              }
            }
          ]
          volumes: volumes
        }
      }
    }
  }
}

output id string = workload.id
