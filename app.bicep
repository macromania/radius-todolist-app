extension radius

@description('The Radius Application ID. Injected automatically by the rad CLI.')
param application string

@description('The Radius Environment ID. Injected automatically by the rad CLI.')
param environment string

@description('Port the application listens on. Overrides the image default of 3000.')
param appPort int = 35493

// Pinned by digest, not by the :latest tag. A mutable tag means a restart, a
// scaling event or a node replacement can run a different image with no change
// here and no review, and this container is handed the Redis credential.
// Resolve a new one with:
//   docker buildx imagetools inspect ghcr.io/radius-project/samples/demo:latest \
//     --format '{{.Manifest.Digest}}'
@description('Application image, pinned by digest.')
param image string = 'ghcr.io/radius-project/samples/demo@sha256:0ae87935398b92627ab73bbe2bdf43d777419076804ba1a6e346f5cb06de43ca'

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
      image: image
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
      // Readiness only. /healthz opens a Redis connection and issues a PING,
      // so wiring it to liveness would turn a brief Redis blip into a restart
      // storm across every replica. Readiness just takes the pod out of the
      // Service until Redis answers again, which is the behaviour we want.
      //
      // The thresholds are set explicitly rather than left to defaults: the
      // handler opens a fresh connection on every probe, and Kubernetes
      // defaults probe timeouts to 1 second, which is too short for that.
      readinessProbe: {
        kind: 'httpGet'
        path: '/healthz'
        containerPort: appPort
        initialDelaySeconds: 5
        periodSeconds: 15
        failureThreshold: 3
        timeoutSeconds: 5
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
