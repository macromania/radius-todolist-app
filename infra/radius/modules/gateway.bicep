extension radius
extension gateways

param application string
param environment string
param apiService string
param challengeService string = 'challenge'
param phase string = 'challenge'
param certificateSecretUri string = ''

resource gateway 'Demo.Platform/gateways@2025-08-01-preview' = {
  name: 'gateway'
  properties: {
    application: application
    environment: environment
    apiService: apiService
    apiPort: 8088
    challengeService: challengeService
    challengePort: 8088
    phase: phase
    certificateSecretUri: certificateSecretUri
  }
}

output id string = gateway.id
