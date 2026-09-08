extension radius

@description('Kubernetes namespace that application resources are deployed into.')
param namespace string = 'todolist-local'

@description('Radius Recipe pack version. Must match the radius extension in bicepconfig.json.')
param recipeVersion string = '0.60'

// Local development environment. Redis is a pod in this cluster, created by the
// Recipe that Radius ships for local development. That Recipe produces an
// unauthenticated, non-TLS Redis and a `redis-cli MONITOR` sidecar that writes
// every command to pod stdout. Acceptable on a single-user kind cluster with no
// real data; do not put anything sensitive in this environment.
//
// The spelling of templateKind and templatePath matters. The README in
// radius-project/recipes shows them hyphenated and shows the type as
// Applications.Core/redisCaches. All three are wrong and will not deploy.
resource local 'Applications.Core/environments@2023-10-01-preview' = {
  name: 'local'
  properties: {
    compute: {
      kind: 'kubernetes'
      resourceId: 'self'
      namespace: namespace
    }
    recipes: {
      'Applications.Datastores/redisCaches': {
        default: {
          templateKind: 'bicep'
          templatePath: 'ghcr.io/radius-project/recipes/local-dev/rediscaches:${recipeVersion}'
        }
      }
    }
  }
}
