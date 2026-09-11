extension radius

param environmentName string
param namespace string
param recipes object
param recipeEnv object = {}

resource environment 'Applications.Core/environments@2023-10-01-preview' = {
  name: environmentName
  properties: {
    compute: {
      kind: 'kubernetes'
      resourceId: 'self'
      namespace: namespace
    }
    recipes: recipes
    recipeConfig: {
      env: recipeEnv
    }
  }
}
