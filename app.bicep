extension radius

@description('The Radius Application ID. Injected automatically by the rad CLI.')
param application string

@description('The Radius Environment ID. Injected automatically by the rad CLI.')
param environment string

@description('Port the application listens on. Overrides the image default of 3000.')
param appPort int = 35493

// This file is identical for every environment. If it ever needs a conditional
// on the environment name, the design has failed.
//
// The Redis resource names no Recipe on purpose: Radius resolves one from the
// environment, which is what makes the same file produce a Redis pod on kind
// and Azure Managed Redis on AKS.
resource redis 'Applications.Datastores/redisCaches@2023-10-01-preview' = {
  name: 'todolist-cache'
  properties: {
    application: application
    environment: environment
  }
}

resource demo 'Applications.Core/containers@2023-10-01-preview' = {
  name: 'demo'
  properties: {
    application: application
    container: {
      image: 'ghcr.io/radius-project/samples/demo:latest'
      ports: {
        web: {
          containerPort: appPort
        }
      }
      env: {
        PORT: {
          value: '${appPort}'
        }
      }
    }
    // The connection name must stay lowercase 'redis'. Radius uppercases it to
    // build CONNECTION_REDIS_*, which is what this application reads. Renaming
    // it to 'cache' would yield CONNECTION_CACHE_* and the application would
    // silently fall back to storing todos in process memory.
    connections: {
      redis: {
        source: redis.id
      }
    }
  }
}
