# Full local provider contract

The one-child Radius executor gate passed on September 10, 2026. This document
describes the current provider contract. Earlier five-cluster scenarios and
teardown do not prove the state-removal refactor; its fresh live run is still
pending. See [the historical gate](local.md) and [FINDINGS.md](../FINDINGS.md)
for exact revision scopes.

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

Preserve historical `.state/local` archives. Current commands do not read them.
A new clone or worktree needs only source, the selected `.env`, and access to
the same Docker Desktop daemon. For example:

```sh
git worktree add --detach ../radius-three-plane-fresh HEAD
cd ../radius-three-plane-fresh
```

Checkouts share the same Docker daemon and reserved ports; do not run
two deployments concurrently. An interrupted or failed attempt requires explicit
ownership review. These commands do not retry, adopt, or clear its state.

```sh
uv sync --locked
make check
make init ENV=local
make build CONFIRM_LOCAL=yes
make inspect-build
make bootstrap CONFIRM_LOCAL=yes
make deploy-management CONFIRM_LOCAL=yes
uv run --no-sync python scripts/harness/local/export-state.py --watch --timeout 7200
```

Keep the exporter in a separate terminal or attached background process. Once
the management API and worker are ready, run
`make local-test CONFIRM_LOCAL=yes` from another terminal. Completion must say
`outcome: passed`; submission is not proof. Then follow
[full local cleanup](local-cleanup.md) after preserving acceptance and restored
fault evidence. Image preparation never publishes to an external registry.

## Run path and ownership

Management runs the existing singleton provisioner with `PROVIDER=local`.
Startup discovers `LocalConfig` through current APIs, reads owned Kubernetes
credential Secrets, and proves its
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
Radius group is the selected `STEM`.
Environment registration uses the existing `Applications.Core/environments`
2023 API, as the gate did. This generic create path is never used for the
2025 custom cluster type; no new environment-specific application template is
required.

Shared reuse observes current cluster/gateway owners without creating resources.
The isolated tenant
uses the distinct `isolated-1-control` and `isolated-1-data` allocations.
Management readiness still means the control reconciler created its tenant
record. Control/data poll their parents; no API pushes tenant configuration.

## Public identity and discovered configuration

`provisioning-settings` contains only public starting settings and is injected
into the worker environment. There is no mounted provisioning inventory or
file credential seed. Let `STEM` mean `PROJECT-DEPLOYMENT-local`. The worker
reads all consumed Recipe bindings and runtime image IDs from management Radius,
and current node/Service/namespace/CA data from Kubernetes.

The fixed slot allocation is:

| Slot | Gateway host port | Kubernetes host API port |
|---|---:|---:|
| management | 35490 | 35495 |
| shared-control | 35491 | 35496 |
| shared-data | 35492 | 35497 |
| isolated-1-control | 35493 | 35498 |
| isolated-1-data | 35494 | 35499 |

Every cluster name and context is `STEM-SLOT`. Application namespaces
are `STEM-SLOT-ROLE`; environments are `SLOT`, applications are
`management`, `control`, or `data`. No Azure foundation or fake ARM IDs appear.
An application environment's namespace prefix is `STEM-SLOT`; Radius appends
the application name. Child provisioning uses the shorter `STEM-p-INDEX` prefix
so the final namespace stays within Kubernetes's length limit.

`recipes` has `cluster`, `postgresql`, `redis`, and `gateway`. Each entry has:

* `reference`: `http://local-module-HASH.radius-system.svc.cluster.local:18080/SHA.tar.gz`
* `digest`: `sha256:SHA`
* `moduleServer`: `local-module-HASH`

`HASH` is the first 20 hexadecimal digits of the archive plus server-source
SHA-256. `SHA` is the archive's full SHA-256. Startup reads the named immutable
ConfigMap and checks both hashes. Child clusters receive those verified,
source-only ConfigMaps and static module servers, not management credentials.
Radius injects `context`. Cluster parameters carry the selected resource/group/
access names, runtime image references and IDs, and prepared dependency images.
PostgreSQL uses `node_address`; gateways use `gateway_host_port`.
The gateway routes to its own backend Service
DNS and does not accept a node-address parameter.

`images.api` and `images.provisioner` each contain `reference` and `imageId`.
References are `localhost/STEM-ROLE:FULL_COMMIT`, with the same
40-digit source revision. IDs are the expected inspected Docker image IDs.
Build prepares and inspects all images; bootstrap loads management images.
The cluster Recipe receives both references and streams them from the Radius
executor's daemon into child containerd; there is no host child-image loading.

The in-memory `managementCluster` has `clusterId=kind://STEM-management`,
`uid` (the actual `kube-system` namespace UID), `nodeAddress`, `serviceAddress`
(the Kubernetes service IPv4 address), and `caSHA256` (the CA bytes).
These values come from current APIs and fresh, privately scoped access. An
optional `DEMO_REVISION` pin must match the consumed bindings and images.

## Access and Radius installation

Runtime management access references its rotating projected `tokenFile` and
cluster CA, with an explicit expected HTTPS server and namespace. It uses a
seeded project workspace rather than Helm's Secret-listing discovery.
Every Radius call supplies a project configuration, slot-specific workspace,
scoped home directory, and kubeconfig. The home exposes only the exact
kubeconfig and bundled Bicep 0.42.1 compiler.

Child output must include:

* `clusterId=kind://STEM-SLOT`
* `clusterName=STEM-SLOT`
* `bootstrapAccessRef=kubernetes://STEM-access/STEM-SLOT-access#kubeconfig`

The worker may get only the four named access Secrets. It checks their names,
namespace, slot labels, Radius ownership annotation, and UID. A child kubeconfig
must contain exactly one static certificate-authenticated context, CA data,
`https://PRIVATE_NODE_IP:6443`, and `tls-server-name=STEM-SLOT`.
Exec plugins, proxy overrides, plaintext endpoints, and TLS bypasses are rejected.
Actual `/readyz`, node identity, and cluster UID reads precede installation.
Current API owners, not workstation cluster records, determine re-entry.

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

The SQL bootstrap Job, role names, credentials, runtime Secrets, and read-only
schema observation are shared with Azure. Committed database metadata is the
initialization authority, not a workstation file or ConfigMap. Local PostgreSQL
outputs must
match the plane's private node IP, port `31543`, database name, `plane_setup`,
`tlsRequired: false`, `kubernetes://NAMESPACE/statefulsets/postgres`, and
`postgres-setup`. Only that temporary setup Secret and bootstrap Job/Secret
are removed. The retained `postgres-credentials` server Secret and PVC are not reset.

Local database bindings explicitly require `tlsRequired: false`;
DSNs require `sslmode=disable` and port `31543`.
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
Operator endpoint/API commands discover this URL and their own key for each
invocation. Optional stdout reports are observations, not inventories required
by another command.

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

After image inspection and management bootstrap, the normal
`make deploy-management CONFIRM_LOCAL=yes` performs Recipe setup and deployment.
They can also be inspected as separate administrative stages:

```sh
uv run python scripts/operations/local/setup-demo.py
uv run python scripts/operations/local/deploy-demo.py

uv run python scripts/operations/local/setup-demo.py --execute
uv run python scripts/operations/local/deploy-demo.py --execute
```

Setup consumes the operator image's verified prepared bundle, publishes module
servers and registers management bindings. Deployment observes that setup,
regenerates required ignored Bicep extensions, and enters the public
`local_operator_provider` factory. Its namespace-owned bootstrap Lease prevents
concurrent operators and is never automatically taken over. Service-owned keys
are seeded before the normal provider initializes SQL and deploys management.

The provider has no working-state PVC. Temporary access, SDK certificate files
and CLI homes are removed on exit. The shared command supervisor terminates
owned descendants on timeout. `OnRootMismatch` and private credential modes
remain intact. Neither setup nor management deployment creates a child cluster
or builds an image.

Radius resources, their Terraform state and committed SQL metadata determine
progress. An interrupted Lease or contradictory resources require explicit
review, not deletion of a workstation intent. The tenant/outage harness remains
a separate opt-in acceptance stage.

Offline coverage is in `tests/unit/test_local_provider.py` together with the
unchanged Azure behavior tests in `tests/unit/test_provisioner.py`. These checks
mock external commands and never contact Docker, Kubernetes, or Azure.
