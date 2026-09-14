# Local Radius Recipes

These are the full-demo infrastructure implementations, not another application
definition. The shared applications, modules, custom type schemas, and Azure
Recipes remain unchanged. The [one-child gate](local.md) proved Radius-owned kind
creation and deletion on September 10, 2026. This extension is offline-validated;
the four persistent child slots and full tenant flow still require parent-run
live acceptance. Do not reset or reinstall the healthy management cluster to
publish these Recipes.

## Publication and provider contract

`scripts/operations/local/prepare.py` keeps `module_archive()` and `manifests(archive)`
compatible with the shared-control gate. `module_archive(name)` additionally
accepts `postgresql`, `redis`, and `gateway`.

Terraform stays under `infra/radius/recipes/local/`. The cluster shell helpers
live under `scripts/recipes/local/cluster/`; the packager inserts their bytes as
`node-address.sh` and `load-images.sh` at the archive root. Terraform's module
paths and the published Recipe contract are unchanged.

`recipe_manifests()` returns `(objects, recipes, modules)`:

- `objects`: one immutable ConfigMap, Deployment, and ClusterIP Service per
  module. Each server serves exactly `/<archive-sha256>.tar.gz`; other paths,
  queries, and directory listings are refused. Kubernetes projected-volume
  symlinks are followed only to a regular file inside `/module`; escaping links
  and payloads whose bytes do not match the expected SHA-256 are refused.
- `recipes`: Radius resource type to `default` Recipe definition, containing
  `templateKind: terraform` and its immutable HTTP `templatePath`.
- `modules`: keyed by `cluster`, `postgresql`, `redis`, and `gateway`. Each entry
  contains `url`, `sha256`, `moduleServer`, and `resourceType`.

`recipe_bundle()` includes these fields plus `sharedSourceHashes` and
`liveStatus: not-run`. The source hashes cover the identical application,
module, schema, and Azure Recipe files used with either environment. The CLI
`uv run python scripts/operations/local/recipe-bundle.py` emits this source-only JSON.
It reads neither deployment state nor credentials and runs no platform commands.
It takes no arguments; execution or module-selection flags are rejected. It
does not write `prepared.json` or deploy anything. Operator setup owns freezing
the returned bundle and publishing its objects.

Only explicit allowlisted Terraform sources, shell helpers, and provider locks
enter archives. `.terraform/`, Terraform state, tests, credentials, and unrelated
source never enter the ConfigMaps. Server identities include both the archive
and server-code digests. Publish each needed module's three objects in the
**consuming cluster's** `radius-system` namespace; DNS is cluster-local. Do not
point a child Recipe at a service that exists only in management.

| Resource type | Module | Additional Recipe parameters |
|---|---|---|
| `Demo.Platform/clusters` | `cluster` | `images`, optional empty list |
| `Demo.Platform/postgreSqlDatabases` | `postgresql` | `node_address` |
| `Applications.Datastores/redisCaches` | `redis` | None |
| `Demo.Platform/gateways` | `gateway` | `gateway_host_port` |

The provider registers the environment using the generic
`Applications.Core/environments` create command and protected JSON; its 2023 API
matches the environment type. The JSON declares Kubernetes `self` compute,
the application namespace, `recipes`, and `recipeConfig.env`. The existing
Docker/kind environment is supplied only for management; child environments
keep it empty. Custom child clusters still use the typed 2025 Bicep declaration,
never generic custom-resource creation.

`infra/radius/environments/local.bicep` is an optional equivalent declaration,
not a provider runtime dependency. Its parameters are `environmentName`,
`namespace`, `recipes`, and `recipeEnv={}`. It contains no cloud provider or
registry credential configuration.

Radius injects `context`. For application Recipes, `context.environment.name`
is the slot, `context.application.name` is the role (`management`, `control`, or
`data`), and `context.runtime.kubernetes.namespace` must be
`radplanes-local-<slot>-<role>`. The resource ID must use Radius group
`radplanes-local`. PostgreSQL is allowed only in management/control; Redis only
in data. The provider creates each namespace before application deployment.

For PostgreSQL only, the provider supplies the node's **actual, verified Kubernetes
InternalIP** as `node_address`, never a Docker name, host loopback, guessed address,
or public IP.
Recipe validation additionally requires RFC1918 IPv4. The gateway port must
match that slot's reservation, not merely fall within the reserved range.
Kubernetes provider configuration and the Terraform Secret backend come from
Radius; modules contain no host kubeconfig, local backend, cloud credential, or
provider-side cluster-creation shortcut.

Terraform runtime is pinned to **1.15.8**. Modules retain validator compatibility
with Terraform 1.14–1.15. Dependencies are exact: kind `0.11.0`, external
`2.3.5`, Kubernetes `2.38.0`, and random `3.7.2`, as needed by each module.
Committed provider locks include Linux arm64/amd64 and Darwin arm64. Radius
generates its own wrapper root, so a module lock is not a claim about that
wrapper's runtime lock.

Child Radius needs the canonical Terraform binary/layout before using these
Recipes: custom PostgreSQL/gateway execute in `dynamic-rp`; built-in Redis
executes in `applications-rp`. Set Terraform logging **OFF** and suppress
plaintext plan stdout there before credentials are generated. The parent owns
that installation/configuration. Do not add a Docker socket, derived kind
executor, or management encryption key to a child RP.

## Cluster and image distribution

| Slot | Cluster/context | Host API | Loopback gateway |
|---|---|---:|---:|
| Management, existing bootstrap | `radplanes-local-management` | 35495 | 35490 |
| Shared control | `radplanes-local-shared-control` | 35496 | 35491 |
| Shared data | `radplanes-local-shared-data` | 35497 | 35492 |
| Isolated control | `radplanes-local-isolated-1-control` | 35498 | 35493 |
| Isolated data | `radplanes-local-isolated-1-data` | 35499 | 35494 |

Only the four child slots are Recipe inputs. Management creation remains the
existing bootstrap exception. The kind provider creates children in the
management `dynamic-rp`, with one gateway mapping from loopback to NodePort
31480 and no PostgreSQL host mapping. Its kubeadm v1beta3 patch preserves the
`localhost`, `127.0.0.1`, and exact cluster-name API certificate SANs.

The existing ownership-checked node-address helper now accepts all four child
names. It reads the actual private kind-network address. The protected access
copy changes only endpoint/context selection and TLS server name; its CA, client
certificate/key, cluster/user references, and provider-original kubeconfig
remain intact. Results contain only:

```text
clusterId          kind://radplanes-local-<slot>
clusterName        radplanes-local-<slot>
bootstrapAccessRef kubernetes://radplanes-local-access/radplanes-local-<slot>-access#kubeconfig
```

The parent builds and independently inspects native API/provisioner images in
Docker Desktop; no registry publication is required. `images` permits at most two
distinct references of these forms, with lowercase 40–64-character hex tags:

```text
localhost/radplanes-plane-api:<source-hash>
localhost/radplanes-plane-provisioner:<source-hash>
```

The default empty list preserves the original gate. With images supplied,
`terraform_data.images` invokes `load-images.sh` **after** the kind resource
exists. The access Secret depends on successful completion. The helper checks
the exact child name and kind cluster/control-plane Docker labels, then streams
each exact image from `docker image save` to that node's
`ctr --namespace k8s.io images import -`. It checks both process exit statuses
and the loaded reference returned by child `ctr images list --quiet`.

A POSIX FIFO avoids relying on shell `pipefail` or Python. A 600-second watchdog
bounds ownership checks, both imports, and reference verification. Invalid
image inputs are rejected before that watchdog starts. Cleanup records
cancellation before signaling the watchdog, waits for it to reap its timer,
and removes its own private working files. This avoids leaving a timer holding
output pipes open when a POSIX shell exits during watchdog startup. Cleanup
terminates only recorded child PIDs. Stopping the Docker client does not promise
cancellation of an import already executing inside the node. No image archive
is saved to disk. Tests
execute this real helper against
offline Docker doubles, including producer/import failure, missing loaded
reference, label mismatch, injection refusals, and a hung command.

This uses only management's already accepted daemon/socket authority. There is
no host `kind create` for children, extra registry/host port, Docker creation in
Python, or shared management-key mount. The parent must still enforce inspected
image IDs and immutable tags. Name/label checks do not make daemon access a
security boundary. Failed or interrupted creation/import is not adopted,
retried, or repaired automatically.

## Persistent PostgreSQL

The custom type's `databaseName` is used unchanged. A singleton `postgres`
StatefulSet uses digest-pinned PostgreSQL 17.8, a `postgres-data` 1-GiB PVC on
`standard`, and SCRAM password authentication. No `trust` authentication or
database host mapping is configured. The provider must reject conflicting
existing resources instead of adopting them.

The pinned random provider generates a cryptographic password once in
Terraform state. Reapply does not regenerate it; the PVC preserves the database.
This is not a password-rotation or database-renaming interface. Changing
`databaseName`, replacing state, or removing the PVC needs an explicit operator
decision, not automatic recovery.

`postgres-credentials` is the **retained server credential**. Only the server
mounts it. `postgres-setup` is a separate initializer copy with keys
`host`, `port`, `database`, `username`, and `password`. The provider deletes
**only `postgres-setup`** after initialization and scrubs it again after Recipe
reapply. Deleting that copy must not change the running server password.

The exact shared output contract is:

```text
result.values.host            <actual node_address>
result.values.port            31543
result.values.database        <databaseName>
result.values.username        plane_setup
result.values.tlsRequired     false
result.values.serverId        kubernetes://<namespace>/statefulsets/postgres
result.values.setupSecretName postgres-setup
result.secrets.password       <generated password; Terraform-sensitive>
```

The private NodePort is 31543; the in-cluster `postgres` Service port is 5432.
The Kubernetes reference identifies the real StatefulSet, not a fake Azure
server. Setup credentials never belong in API or reconciler settings.

## Persistent Redis

The built-in resource keeps the existing lowercase `redis` connection.
Digest-pinned Redis 7.4 runs as a singleton StatefulSet with `redis-data`, a
1-GiB `standard` PVC, password authentication, and AOF (`appendfsync everysec`).
The server receives `redis.conf` from retained `redis-credentials`; the password
does not appear in process arguments or a ConfigMap. There is no NodePort, host
port, sidecar, or access logger.

```text
result.values.host       redis.<namespace>.svc.cluster.local
result.values.port       6379
result.values.username   ""
result.values.tls        false
result.secrets.password  <URI-component-encoded generated password>
result.secrets.url       redis://:<encoded-password>@<host>:6379
```

Both secret fields are Terraform-sensitive. Encoding occurs once; the existing
consumer uses the URL as-is or decodes the standalone password once. Password
state and PVC data survive unchanged Recipe reapply.

## HTTP gateway

The backend ClusterIP Service is `gateway-api`, selecting the exact Radius
0.60.2 labels `radapp.io/application=<role>` and
`radapp.io/resource=<apiService>`, with `apiPort` from the shared custom type.
A ConfigMap configures digest-pinned Envoy's `gateway` Deployment. The
`gateway` NodePort Service uses 31480 and the cluster's existing loopback mapping.
Envoy runs one worker with no admin listener or access log.

```text
result.values.host              127.0.0.1
result.values.url               http://127.0.0.1:<gateway_host_port>
result.values.gatewayId         kubernetes://<namespace>/services/gateway
result.values.apiBackendService gateway-api
```

The gateway routes through its backend Service's cluster DNS; it needs no
`node_address` parameter and emits no `apiBackendIp`.

Local intentionally ignores the shared type's Azure `phase`,
`challengeService`, and certificate reference: both Azure phases yield **local
HTTP**, never simulated HTTPS or a fabricated certificate. This requires no
environment branch in a shared application or module.

## Accepted local limitations and verification

Datastore transport is private but **not TLS**. Child datastore Secrets,
credentials in Terraform backend state, and local PVCs are not claimed to be
encrypted at rest. Management's proven Secret encryption remains unchanged;
its key is not copied into children. These are synthetic-data, trusted-operator
development limitations, not production security or hostile-tenant isolation.
Do not relax Azure's verified PostgreSQL/Redis TLS settings.

Run only the offline checks before the parent reviews and executes:

```sh
uv run python scripts/operations/local/validate.py
uv run pytest -q tests/operations/local
uv run ruff check scripts/operations/local tests/operations/local
shellcheck scripts/recipes/local/cluster/*.sh
```

The validator extracts each exact published archive into private project state,
uses `init -backend=false -lockfile=readonly`, runs `validate`, and runs tests
with every infrastructure provider mocked. It never runs a live Terraform
apply, Docker command, Kubernetes mutation, or cloud operation. Mock tests cover
all reservations, protected access, unchanged reapply passwords, persistent
workloads, exact outputs, encoding, routing, and invalid inputs. Python tests
cover the bundle CLI, source allowlists, immutable publication, helper execution,
and the validator's real command path.

These checks do not prove a live image import, persistent database restart,
Radius wrapper execution, or full-demo outage behavior. The parent must verify
those outcomes on the existing management deployment and newly Radius-created
children, then record new timestamped evidence separately from the passed
milestone-5 gate.
