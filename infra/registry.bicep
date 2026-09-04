// Container registry for Radius Recipes.
//
// This is deployed separately from infra/main.bicep and before it, because the
// custom Redis Recipe has to be published and its digest recorded before
// environments/azure.bicep can reference it, and that reference is needed at
// the time the Azure Radius environment is created.
//
// Anonymous pull is enabled so the Radius control plane can fetch Recipes with
// no registry credentials, which removes a whole class of failure from the
// deploy path. That requires the Standard SKU; Basic does not support it.
// Anonymous pull makes Recipe templates world-readable. They contain no
// secrets, only template logic, so this is an accepted trade. Push still
// requires authentication.
//
// Recipes are always referenced by digest, never by tag, so that repointing a
// tag cannot cause the Radius identity to execute a different template.

@description('Azure region for the registry.')
param location string = resourceGroup().location

@description('Globally unique registry name. Lowercase alphanumeric only.')
param registryName string = 'acrtodolist${uniqueString(resourceGroup().id)}'

@description('Tags applied to the registry.')
param tags object = {}

resource registry 'Microsoft.ContainerRegistry/registries@2025-04-01' = {
  name: registryName
  location: location
  tags: tags
  sku: {
    name: 'Standard'
  }
  properties: {
    adminUserEnabled: false
    anonymousPullEnabled: true
    publicNetworkAccess: 'Enabled'
  }
}

output registryName string = registry.name
output loginServer string = registry.properties.loginServer
