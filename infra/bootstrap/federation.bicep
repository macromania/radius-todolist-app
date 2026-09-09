@description('Identity must already exist in this deployment resource group.')
param identityName string
param issuer string
@description('Entries contain name and full system:serviceaccount:<namespace>:<name> subject.')
param bindings array

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = {
  name: identityName
}

// Azure rejects concurrent federated-credential writes to the same identity with HTTP 409.
@batchSize(1)
resource credentials 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2024-11-30' = [for binding in bindings: {
  parent: identity
  name: binding.name
  properties: {
    issuer: issuer
    subject: binding.subject
    audiences: [
      'api://AzureADTokenExchange'
    ]
  }
}]

output resourceIds array = [for (binding, i) in bindings: credentials[i].id]
