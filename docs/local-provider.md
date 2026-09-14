# Full local provider contract

The one-child Radius executor gate passed on September 10, 2026. This document
describes the full-demo implementation. The fresh five-cluster scenario,
parent outages, corrected API identity, and owner-ordered teardown are verified;
see [the gate evidence](local.md) and [FINDINGS.md](../FINDINGS.md) for exact run scopes.

## Fresh local run

For the self-paced manual walkthrough, use
[RUN_LOCAL_SCENARIOS.md](../RUN_LOCAL_SCENARIOS.md). The sequence below describes
the provider stages and the automated acceptance alternative.

The local tools resolve Docker Desktop's `desktop-linux` context before
switching subprocesses to a project-scoped HOME. They pin the returned local
Unix socket for the process instead of using an account-specific path or the
global current context. Remote endpoints and missing contexts fail explicitly;
there is no fallback to another runtime. Use project Python 3.13, kind 0.31.0, Radius 0.60.2 with
Bicep 0.42.1, and the reserved ports in `ports.env`.

Preserve this checkout's historical `.state/local`. All local commands use that
fixed path within their own checkout, so run the fresh procedure from a **new
clone or worktree with no `.state/local`**. For example, after verifying the old
project clusters are gone:

```sh
git worktree add --detach ../radius-three-plane-fresh HEAD
cd ../radius-three-plane-fresh
```

Checkouts share the same Docker daemon, cluster names, and ports; do not run
two deployments concurrently. An interrupted or failed attempt requires explicit
ownership review. These commands do not retry, adopt, or clear its state.

```sh
uv sync --locked
make check
make local-prepare
make local-executor-build CONFIRM_LOCAL=yes
make local-executor-inspect CONFIRM_LOCAL=yes
make local-runtime-build CONFIRM_LOCAL=yes
make local-runtime-inspect CONFIRM_LOCAL=yes
make local-bootstrap CONFIRM_LOCAL=yes
make local-install-radius CONFIRM_LOCAL=yes
make local-runtime-load CONFIRM_LOCAL=yes
make local-setup CONFIRM_LOCAL=yes
make local-deploy-management CONFIRM_LOCAL=yes
uv run --no-sync python harness/local/export-state.py --watch --timeout 7200
```

Keep the exporter in a separate terminal or attached background process. Once
`export-status.json` reports `ready_for_onboarding: true`, run
`make local-test CONFIRM_LOCAL=yes` from another terminal. Completion must say
`outcome: passed`; submission is not proof. Then follow
[full local cleanup](local-cleanup.md) after preserving acceptance and restored
fault evidence. Image preparation never publishes to an external registry.

## Run path and ownership

Management runs the existing singleton provisioner with `PROVIDER=local`.
Startup loads `LocalConfig`, uses `.state/local/credentials.json`, proves its
management service-account username, CA, node address, and `kube-system`
namespace UID, then reads the actual management Radius group/environment and
the immutable Recipe ConfigMaps. It does not use an Azure identity or inherit
Azure, Docker, Terraform, or proxy environment settings.

The singleton lock, interrupted-operation marking, five-second poll interval,
and session guards are unchanged. Every command checks that same lock-holding
connection before spawning and during execution. Failed/interrupted cluster
creation, child installation, SQL initialization, or management setup is not
automatically retried, adopted, or reset.

Only management Radius creates child clusters, using the existing
`modules/child-cluster.bicep` and its registered `2025-08-01-preview` type.
The provider never invokes kind, Docker, Terraform apply, or direct cluster
creation. `rad deploy` waits for completion; the provider then requires a
successful custom-resource state and exact allocation outputs. Cluster-only
environments are `provision-SLOT`, applications are `cluster-SLOT`, and the
Radius group is `radplanes-local`.
Environment registration uses the existing `Applications.Core/environments`
2023 API, as the gate did. This generic create path is never used for the
2025 custom cluster type; no new environment-specific application template is
required.

The shared pair is reused without infrastructure calls. The isolated tenant
uses the distinct `isolated-1-control` and `isolated-1-data` allocations.
Management readiness still means the control reconciler created its tenant
record. Control/data poll their parents; no API pushes tenant configuration.

## Immutable configuration

`PROVISIONING_CONFIG` defaults to `/etc/plane-demo/provisioning.json`. The
operator writes the same schema to `.state/local/provisioning.json` and mounts
it through immutable `provisioning-settings`:

```json
{
  "version": 1,
  "provider": "local",
  "projectName": "radplanes",
  "allocations": {
    "management": {
      "slot": "management",
      "clusterName": "radplanes-local-management",
      "context": "radplanes-local-management",
      "gatewayPort": 35490,
      "apiPort": 35495
    }
  },
  "recipes": {},
  "images": {},
  "managementCluster": {}
}
```

This abbreviated example is not deployable. All five exact entries are required:

| Slot | Gateway host port | Kubernetes host API port |
|---|---:|---:|
| management | 35490 | 35495 |
| shared-control | 35491 | 35496 |
| shared-data | 35492 | 35497 |
| isolated-1-control | 35493 | 35498 |
| isolated-1-data | 35494 | 35499 |

Every cluster name and context is `radplanes-local-SLOT`. Application namespaces
are `radplanes-local-SLOT-ROLE`; environments are `SLOT`, applications are
`management`, `control`, or `data`. No Azure foundation or fake ARM IDs appear.

`recipes` has `cluster`, `postgresql`, `redis`, and `gateway`. Each entry has:

* `reference`: `http://local-module-HASH.radius-system.svc.cluster.local:18080/SHA.tar.gz`
* `digest`: `sha256:SHA`
* `moduleServer`: `local-module-HASH`

`HASH` is the first 20 hexadecimal digits of the archive plus server-source
SHA-256. `SHA` is the archive's full SHA-256. Startup reads the named immutable
ConfigMap and checks both hashes. Child clusters receive those verified,
source-only ConfigMaps and static module servers, not management credentials.
Radius injects `context`. Additional Recipe parameters are exactly `images`
for clusters, `node_address` for PostgreSQL, none for Redis, and
`gateway_host_port` for gateways. The gateway routes to its own backend Service
DNS and does not accept a node-address parameter.

`images.api` and `images.provisioner` each contain `reference` and `imageId`.
References are `localhost/radplanes-plane-ROLE:FULL_COMMIT`, with the same
40-digit source revision. IDs are the expected inspected Docker image IDs.
The parent/operator builds, inspects, and loads management images separately.
The cluster Recipe receives both references and streams them from the Radius
executor's daemon into child containerd; there is no host child-image loading.

`managementCluster` has `clusterId=kind://radplanes-local-management`,
`uid` (the actual `kube-system` namespace UID), `nodeAddress`, `serviceAddress`
(the Kubernetes service IPv4 address), and `caSHA256` (the CA bytes).
These values are read from the existing, protected management kubeconfig and
cross-checked with the bootstrap record, not guessed from a previous run.

## Access and Radius installation

Runtime management access references its rotating projected `tokenFile` and
cluster CA, with an explicit expected HTTPS server and namespace. It uses a
seeded project workspace rather than Helm's Secret-listing discovery.
Every Radius call supplies a project configuration, slot-specific workspace,
scoped home directory, and kubeconfig. The home exposes only the exact
kubeconfig and bundled Bicep 0.42.1 compiler.

Child output must include:

* `clusterId=kind://radplanes-local-SLOT`
* `clusterName=radplanes-local-SLOT`
* `bootstrapAccessRef=kubernetes://radplanes-local-access/radplanes-local-SLOT-access#kubeconfig`

The worker may get only the four named access Secrets. It checks their names,
namespace, slot labels, Radius ownership annotation, and UID. A child kubeconfig
must contain exactly one static certificate-authenticated context, CA data,
`https://PRIVATE_NODE_IP:6443`, and `tls-server-name=radplanes-local-SLOT`.
Exec plugins, proxy overrides, plaintext endpoints, and TLS bypasses are rejected.
Actual `/readyz`, node identity, and cluster UID reads precede installation.
Protected `SLOT-cluster.json` records retain the non-secret ownership proof.

Children install stock Radius 0.60.2 without a Docker image/socket overlay.
Both Terraform-capable RPs receive a checksum-pinned Terraform 1.15.8 layout
under `/terraform`, `terraform.logLevel: OFF`, and error-level RP logging.
A restricted init container seeds only the Terraform volume; it has no daemon
mount or service-account token. Automatic token mounting is disabled for these
Pods; a rotating token/CA projection is mounted only in the main RP container.
Management setup changes only its applications RP's Terraform layout;
the proven management dynamic RP and Docker mount are preserved.

## Databases, workloads, and endpoints

The three existing `infra/radius/apps/*.bicep` files remain the only plane
declarations. Local deployment skips certificate administration. Their challenge
containers remain harmless, and the local gateway Recipe uses HTTP regardless
of the Azure challenge/HTTPS phase properties.

The existing SQL bootstrap Job, role names, credentials, runtime Secrets, and
initialization markers are shared with Azure. Local PostgreSQL outputs must
match the plane's private node IP, port `31543`, database name, `plane_setup`,
`tlsRequired: false`, `kubernetes://NAMESPACE/statefulsets/postgres`, and
`postgres-setup`. Only that temporary setup Secret and bootstrap Job/Secret
are removed. The retained `postgres-credentials` server Secret and PVC are not reset.

Local credentials explicitly record `provider: local` and
`database.tlsRequired: false`; DSNs require `sslmode=disable` and port `31543`.
Azure remains the default credential mode, requires verified TLS, and retains
its `.postgres.database.azure.com:5432` restrictions. Missing TLS metadata never
opts an Azure database into plaintext. Runtime API and provisioner secrets and
images remain separate; APIs never receive setup credentials or child access.

Redis remains the lowercase `redis` connection, password authenticated, with
the existing encoded-password contract. Its local TLS setting is explicitly
false; Azure's TLS setting remains true. Local traffic is trusted synthetic
demo traffic, not hostile-tenant isolation.
The stock applications RP lacks PVC lifecycle permission. Data prerequisites
grant it only through `redis-recipe-storage` in that data namespace, bound to
`radius-system:applications-rp`. Existing StatefulSet permission is reused;
no cluster-wide role or runtime API permission is added.

Public endpoint records always contain `http://127.0.0.1:ALLOCATED_PORT`. A
management worker Pod checks administrative liveness through the child's
private node address on port `31480`; it must not try the Mac's loopback URL.
That internal health address never replaces the operator's public URL.
The provider writes only `SLOT-endpoint.json` and protected `SLOT.key` files.
The exporter is the sole publisher of aggregate `endpoints.json`; the local
provider neither creates nor reads or modifies that exporter-owned file.

## Operator stages

### Terraform state identity

Radius 0.60.2's [Kubernetes backend implementation][radius-backend] includes the
**application name** when deriving a Recipe's state Secret name:

```text
input = lowercase(environmentName + "-" + applicationName + "-" + resourceId)
secretName = "tfstate-default-" + SHA256(input).hex()[0:40]
namespace = "radius-system"
```

For a full-demo child cluster, `environmentName` is `provision-SLOT` and
`applicationName` is `cluster-SLOT`, on management Radius. For a child
datastore/gateway, these are `SLOT` and `ROLE`, on that child's Radius.
The gate omitted an application; only that case omits `applicationName + "-"`
from the hash input. Do not reuse the gate's two-part hash for full-demo apps.

The backend can recognize legacy SHA-1 state from older Radius versions.
These fresh local deployments expect the SHA-256 form; unexpected legacy,
missing, replaced, or partial state requires explicit review, not adoption.
Cleanup must capture and verify actual Secret UID, lineage, labels, and
resource ownership before mutation. The provider's cluster record does not
claim a Terraform state UID or a live full-demo state proof.

[radius-backend]: https://github.com/radius-project/radius/blob/v0.60.2/pkg/recipes/terraform/config/backends/kubernetes.go

### Management setup and deployment

After parent-reviewed native image inspection/loading and the existing
management bootstrap, preview or execute these **separate** stages:

```sh
uv run python operations/local/setup-demo.py
uv run python operations/local/deploy-demo.py

uv run python operations/local/setup-demo.py --execute
uv run python operations/local/deploy-demo.py --execute
```

Setup obtains the source-only bundle from `recipe-bundle.py`, builds the exact
config, publishes module servers, and registers the management environment/types.
Deployment initializes management PostgreSQL through Radius and the existing
SQL Job, creates the `standard` provisioner PVC and restricted service accounts,
and deploys the existing management app. `OnRootMismatch` and private state-file
modes are preserved. Neither stage creates a cluster or builds an image.

Each stage writes an exclusive intent before mutation and a separate completion
record only after its actual checks. Existing intents block automatic replay.
The full tenant/outage harness is a later, explicit acceptance stage. A successful
offline test or management deployment does not claim that acceptance passed.

Offline coverage is in `tests/unit/test_local_provider.py` together with the
unchanged Azure behavior tests in `tests/unit/test_provisioner.py`. These checks
mock external commands and never contact Docker, Kubernetes, or Azure.
