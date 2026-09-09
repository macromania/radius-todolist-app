# Azure infrastructure source and contracts

This is the source contract for the three-plane Azure integration gates. It is
**not deployment evidence**. Management bootstrap is the only direct AKS
deployment. Every child AKS is created by the custom cluster Recipe executed by
management Radius. Application and environment declarations remain separate.

## Entry point and allocation

`infra/bootstrap/azure.bicep` is subscription-scoped. Deploy its compiled ARM
JSON, not the Bicep source through the older `az bicep`. The trusted operator
needs permission to create the named project resource groups, identities, custom
roles, and scoped assignments. Runtime identities never get those role-grant
permissions.

The current `.azure/plan.md` selects `centralus`: the parent preflight found
PostgreSQL subscription restrictions in `eastus2` and `westus2`. A read-only
`az aks get-versions --subscription <subscription> --location centralus` probe
on 2026-09-09 confirmed **1.35.7**, which is the pinned default. Recheck regional
support before deploying later. Region and Kubernetes version remain parameters.

Required parameters:

| Parameter | Contract |
| --- | --- |
| `nameSalt` | Non-secret, run-unique salt for registry/vault names; retain for redeployment. |
| `operatorIp` | Preflight-validated bare public IPv4 address. Bootstrap appends `/32`; do not pass a CIDR. |
| `operatorObjectId` | Confirmed Entra user object ID; the template grants that user non-local-admin AKS access. |

Optional parameters include `projectName` (`radplanes`), `location` (`centralus`),
`kubernetesVersion` (`1.35.7`), `nodeVmSize` (`Standard_D4s_v5`), `nodeCount` (2),
`childSlots`, additional `tags`, and the two service-account subjects below.
The caller must validate IPv4 syntax/publicness, slot uniqueness and names, and
address-space conflicts before deployment. Bicep length constraints are not a
substitute for that validation.

The default order is management, shared-control, shared-data,
isolated-1-control, isolated-1-data. Keep this order on redeployment: it determines
the fixed subnet allocation. Append slots only after operator review. The
15-child-slot schema bound follows the subnet address allocation; it is not a
tenant or spending limit.

Bootstrap creates:

* `rg-radplanes-platform`: network, NAT, registry, private DNS, certificate vault.
* `rg-radplanes-management-cluster`: management AKS and its identities.
* `rg-radplanes-management-app`: initially empty, owned by management Radius.
* `rg-radplanes-<slot>-cluster` and `rg-radplanes-<slot>-app` for every child slot:
  identities/permissions only, **not child clusters or databases**.
* Four narrowly scoped custom role definitions at subscription scope. They
  grant permissions only through assignments at project groups, identities,
  or individual vault objects.

AKS creates `rg-radplanes-<slot>-nodes` when its cluster is created. Do not
precreate these managed node groups. Cluster and node-pool tags propagate to the
corresponding managed resources, including the node resource group.

## Bootstrap outputs

The ARM deployment has four ordinary, non-secret outputs:

| Output | Fields |
| --- | --- |
| `foundation` | `projectName`, `subscriptionId`, `tenantId`, `location`, `platformResourceGroup`, `virtualNetworkId`, `virtualNetworkName`, `egressIp`, `egressIpId`, `registryId`, `registryName`, `registryLoginServer`, `vaultId`, `vaultName`, `vaultUri`, `postgresqlDnsZoneId`, `redisDnsZoneId`, `vaultDnsZoneId`, `kubernetesVersion`, `nodeVmSize`, `nodeCount`, `authorizedIpRanges`, `roleDefinitionIds`, `tags` |
| `allocations` | Array of records described below; convert to a dictionary keyed by `slot` before passing to the cluster Recipe. |
| `coordinatorIdentity` | `id`, `clientId`, `principalId` |
| `managementCluster` | `id`, `name`, `resourceGroup`, `fqdn`, `oidcIssuer` |

Every allocation record contains:

```text
slot, clusterName, clusterResourceGroup, clusterResourceGroupId,
appResourceGroup, appResourceGroupId, nodeResourceGroup,
nodeSubnetId, nodeSubnetName, gatewaySubnetId, gatewaySubnetCidr,
privateEndpointSubnetId, postgresqlSubnetId,
apiPrivateIp, challengePrivateIp, certificateIssuerSubject,
certificateName, acmeStateSecretName,
identities.{controlPlane,kubelet,radius,gateway,certificateIssuer}
```

Each identity contains `id`, `clientId`, and `principalId`. No output contains a
kubeconfig, access token, password, certificate, or private key.
`roleDefinitionIds` contains `certificateImporter`, `acmeStateWriter`,
`childClusterRecipe`, and `childIdentityFederation`; keep them in the operator ownership manifest for
cleanup after their assignments are removed.

The stable role display names for `projectName=radplanes` are
`radplanes certificate importer`, `radplanes ACME state writer`,
`radplanes child cluster recipe`, and `radplanes child identity federation`,
respectively. Scripts should consume the
returned IDs rather than resolving roles by display name.

## Network and service defaults

The shared VNet is `10.64.0.0/16`. For role index `i`, beginning at zero:

| Purpose | Allocation |
| --- | --- |
| Nodes and private API/challenge load balancers | `10.64.i.0/24` |
| Dedicated Application Gateway subnet | `10.64.(16+i).0/24` |
| Private endpoints | `10.64.(32+i).0/27` |
| PostgreSQL-only delegated subnet | `10.64.(48+i).0/27` |
| Private API / challenge IP | `10.64.i.240` / `10.64.i.241` |

All node subnets use the project NAT gateway. Its static public IPv4 address and
the operator CIDR form the AKS API authorized ranges. This is shared project
egress, not a hostile-tenant network isolation boundary. Pods use Azure CNI
Overlay (`192.168.0.0/16`) and Cilium; Services use `172.20.0.0/16`.
Each AKS has two untainted `Standard_D4s_v5` system nodes, Standard Load Balancer, Entra Azure RBAC,
local accounts disabled, OIDC, and workload identity. No public node IPs, AGIC,
or Azure Envoy/Contour ingress is configured.

The small-node default follows the current AKS system-pool documentation,
updated 2026-08-21: at least four vCPUs per system node and two system nodes.
The old two-vCPU node SKU is therefore not reused. This is ten nodes / forty
vCPUs for the five-cluster allocation, before upgrades. The parent must validate
that actual quota and include upgrade surge capacity before deployment.
The optional read-only `az vm list-skus` probe produced no output over four
minutes and was stopped; SKU availability and quota are not claimed verified.

PostgreSQL uses delegated-subnet private access and the VNet-linked
`radplanes.postgres.database.azure.com` zone. It has no public firewall rule or
private endpoint. Redis uses `privatelink.redis.azure.net`; Key Vault uses
`privatelink.vaultcore.azure.net`. Both have private endpoints. The vault has
Azure RBAC, public access disabled, purge protection, and seven-day soft-delete
retention. A reused salt can conflict with its soft-deleted vault; do not purge
automatically to work around that.

The Standard registry's endpoint is public for operator publication, but
anonymous access and the admin account are not enabled. Images and Recipes
require authenticated pull. Public registry transport is not a public database
or a public application backend.

Every declared taggable resource receives `SecurityControl=Ignore`,
`project=radplanes`, and `managedBy=radius-todolist-app`. Extra tags cannot
override these values. Private-endpoint NIC names are deterministic and
`Microsoft.Resources/tags` assignments explicitly tag those generated NICs.
AKS has both cluster and node-pool tags; gateway Services also supply Azure
load-balancer resource tags. The installation layer must set the same tags on
StorageClasses before provisioning Radius's persistent data disks. Audit the
actual service-generated resources during the live gate; compilation does not
prove tag propagation.

## Identity boundaries and federation

| Identity | Permissions |
| --- | --- |
| Public management API | No Azure identity or administrative token from this template. |
| Coordinator | AKS Cluster User and AKS RBAC Cluster Admin at the allocated cluster groups, plus registry pull. No AKS ARM write, Contributor, vault write, or role grants. |
| Management Radius | Contributor on **management app group only**; custom AKS/deployment permission in child cluster groups; individual control-plane/kubelet identity assignment; individual child Radius/issuer identity federation; required subnet/DNS access and registry pull. |
| Child Radius | Contributor on its app group, its gateway subnet, its datastore subnet/private DNS dependency, VNet read, registry pull, and assignment of its gateway identity. No permission to update/delete its own AKS. |
| AKS control-plane identity | Network Contributor on its node subnet, VNet read, and Managed Identity Operator on its kubelet identity. |
| Kubelet | Registry pull only. Precreated, so neither Recipe nor coordinator runs `--attach-acr` or grants a pull role. |
| Gateway | Key Vault Secrets User on only `vault/secrets/gateway-<slot>`. |
| Certificate issuer | Certificate read/import/update on only `vault/certificates/gateway-<slot>`; Secrets User on only its matching backing secret; read/set on only `vault/secrets/acme-<slot>`. No vault-wide data rights, deletion, purge, role grants, or general vault administration. |

The AKS Recipe custom role permits `managedClusters/*` and nested deployments
in child cluster groups; it does not permit managed-identity writes. A separate
federation-only role is assigned **at each individual child Radius and issuer
identity**, not at the group or subscription. None of the runtime role definitions
or grants includes Owner, User Access Administrator, or
`Microsoft.Authorization/roleAssignments/write`.

Bootstrap binds management Radius's `applications-rp`, `bicep-de`, `ucp`, and
`dynamic-rp` service accounts in `radius-system`. The child cluster Recipe
creates the same four bindings using the new child OIDC issuer and preallocated
Radius identity. It also binds that child's issuer identity. There are no role
assignments inside any Recipe, including its inlined modules.
The management federation module and flat child Recipe both use
`@batchSize(1)`: four writes against one Radius identity must be serialized to
avoid Azure HTTP 409 conflicts (F004). The child issuer has one separate binding.

### Per-slot certificates in the one project vault (F005)

The project keeps **one** private Key Vault. The allocation supplies these
deterministic, versionless object names:

```text
certificateName     = gateway-<slot>
acmeStateSecretName  = acme-<slot>
```

For example, the shared control allocation uses `gateway-shared-control` and
`acme-shared-control`. The issuer must import under that exact certificate name.
The gateway's reference is `foundation.vaultUri + "secrets/" + certificateName`,
without a version. Keep account/certificate reuse material in the corresponding
ACME state secret; do not use a shared cross-slot account secret.
Apply the required project tags to the imported certificate and state secret
from the issuance layer as well.

`platform-access.bicep` supplies four grants per allocation:

| Principal | Exact ARM scope suffix under the vault ID | Role |
| --- | --- | --- |
| Slot gateway identity | `/secrets/gateway-<slot>` | Key Vault Secrets User |
| Slot issuer identity | `/certificates/gateway-<slot>` | Custom certificate importer: certificate read/import/update only |
| Slot issuer identity | `/secrets/gateway-<slot>` | Key Vault Secrets User: backing-secret read only |
| Slot issuer identity | `/secrets/acme-<slot>` | Custom ACME state writer: secret metadata/get/set only |

The tiny `vault-object-access.json` ARM module takes these full
`resourceId(...)` scopes and emits a `Microsoft.Authorization/roleAssignments`
resource. It does not invent a management-plane certificate API, create a
placeholder certificate, or retrieve secret material. Bicep inlines this ARM
module into the bootstrap template. There is no vault-scoped data assignment
and no secret-set permission on the gateway certificate's backing secret.
Child Radius can assign only its own gateway identity; obtaining that identity
must not expose another plane's certificate or ACME material.

**Object creation order is a required live gate.** Microsoft documents secret,
certificate, and key object-scope RBAC, including the exact secret scope syntax.
Its portal example starts with a previously created object; that documentation
does **not** establish that first-import/set works after assigning a role to a
not-yet-created object. This source declares the assignments before issuance.
The parent must prove assignment at the unused names, first certificate import,
first ACME secret set, same-slot reads, and denied cross-slot reads/imports/sets.
Allow bounded RBAC propagation time and record the actual denied operation and
scope when diagnosing a failure.
If prebinding or first creation is rejected, stop and report the failed gate.
Do not grant a vault-wide role or introduce more vaults as a fallback.

The read-only project-group probe returned `false` during this correction pass.
If an older bootstrap was deployed elsewhere or later, incremental ARM
deployment will **not** delete the previous vault-wide assignments. Before
acceptance, the parent must inspect and remove obsolete vault-scope assignments
for these gateway/issuer principals and verify no inherited data grant bypasses
the object scopes. This source update does not claim to revoke live assignments.

The default additional subjects are:

```text
system:serviceaccount:radplanes-management-management:provisioner
system:serviceaccount:radplanes-system:certificate-issuer
```

Override `coordinatorServiceAccountSubject` and
`certificateIssuerServiceAccountSubject` at bootstrap if installation names
differ. Run child issuance Jobs in their corresponding child cluster; their
identity is federated to that cluster, not management's issuer. The coordinator
can drive those Jobs through the child's Entra-authenticated API.

Install Radius 0.60.2 with workload identity and without Contour, targeting the
exact kubecontext. Register Azure credentials with its workload-identity method,
not an implicit global workspace:

```sh
rad --config PROJECT_CONFIG credential register azure wi \
  --workspace WORKSPACE --client-id RADIUS_CLIENT_ID --tenant-id TENANT_ID
```

The installer must annotate all four Radius service accounts with
`azure.workload.identity/client-id` and set the
`azure.workload.identity/use: "true"` label on their Deployment pod templates.
The parent investigation of Radius issue #12278 identified missing workload
identity projection setup, not a confirmed Recipe-engine defect. Verify actual
token projection and Azure access on the live installation.

Do not copy the old service-principal fallback or role-grant commands into a
runtime bootstrap. Public APIs/reconcilers must not receive the coordinator's
service account or Radius projected token. The only node identity is the
low-privilege kubelet identity; workload identity does not itself block IMDS.
The application installation and live negative-access tests must enforce and
check the intended pod permissions.

## Custom resource and Recipe contracts

All types use namespace `Demo.Platform` and API `2025-08-01-preview`. They use the
`Applications.Core` model: `environment` is required and `application` is the
owning Radius application, not a different provider model.

| Type | Resource inputs in `context.resource.properties` | Non-secret Recipe values |
| --- | --- | --- |
| `clusters` | `slot` | `clusterId`, `clusterName`, `resourceGroup`, `fqdn`, `oidcIssuer`, `bootstrapAccessRef`, `radiusIdentityId`, `radiusClientId` |
| `postgreSqlDatabases` | `databaseName` | `host`, `port`, `database`, `username`, `tlsRequired`, `serverId`, `setupSecretName` |
| `gateways` | `apiService`, `apiPort`, `challengeService`, `challengePort`, `phase`, optional `hostname`, optional `certificateSecretUri` | `host`, `url`, `gatewayId`, `apiBackendService`, `challengeBackendService`, `apiBackendIp`, `challengeBackendIp` |

`cluster.bicep` takes `allocations` as a **one-entry** dictionary keyed by slot,
plus `location`, `tenantId`, `kubernetesVersion`, `nodeVmSize`, `nodeCount`,
`authorizedIpRanges`, and optional `tags`. Only validated placement chooses the slot. The coordinator
must not accept arbitrary allocation dictionaries, resource groups, subnet IDs,
or identity IDs from the public request. `bootstrapAccessRef` is the AKS ID;
the authorized coordinator obtains user credentials separately.

### Per-slot management-Radius provisioning environment (F017)

The child cluster Recipe is now **flat**: one native AKS resource, four
serialized Radius federated credentials, and one issuer federated credential.
It creates no identities or Azure role grants and emits no nested
`Microsoft.Resources/deployments` resource or reference. The AKS settings remain
aligned with the management bootstrap helper; a compiled regression test
compares their authentication, networking, node-pool, and workload-identity
configuration. Do not reintroduce Bicep module calls for sharing that body.

The live F017 failure was an Azure-ID reference to
`Microsoft.Resources/deployments/radius-cluster-...` that the Radius deployment
engine could not find. The pinned Bicep driver initializes the **deployments**
provider scope to `/planes/radius/local/resourceGroups/<Radius group>`, then
sets the **Azure** provider scope separately from the environment. Our former
cross-resource-group module compiled references to deployments under an Azure
subscription/resource-group ID instead. This scope mismatch explains the
observed lookup failure; it is not a claim that all Radius nested modules are
unsupported.

For each allocated child slot, the parent driver must register a separate
provisioning environment **in the existing management Radius installation and
Radius resource group**:

| Setting | Exact contract for the default allocation |
| --- | --- |
| Environment name | `provision-<slot>` |
| Kubernetes compute resource | `self` — still the management cluster |
| Environment Kubernetes namespace | `radplanes-p-<slot>` |
| Radius application name | `cluster-<slot>` |
| Azure provider scope | `/subscriptions/<foundation.subscriptionId>/resourceGroups/<allocation.clusterResourceGroup>` |
| Recipe type | `Demo.Platform/clusters`, mapped to the new immutable flat-cluster Recipe reference |
| Recipe parameter `allocations` | `{ "<slot>": <that slot's bootstrap allocation> }`, not the complete allocation map |
| Other Recipe parameters | Explicit foundation `location`, `tenantId`, `kubernetesVersion`, `nodeVmSize`, `nodeCount`, `authorizedIpRanges`, and required tags |
| Private registry authentication | Copy the already working `recipeConfig.bicep.authentication` registry entry, using `RadiusSecretStore` with `azureWorkloadIdentity`, into **every** provisioning environment |

Validate the provider scope exactly against the selected allocation before
submitting the resource. Keep stable, unique Radius resource names from the
existing child-cluster application declaration. The default names above keep
the resulting `<environment namespace>-<application>` namespace within 63
characters; validate that bound when extending the operator allocation.
Do not repeatedly mutate the management application's environment to point it
at different cluster groups.

Deploy the **unchanged** child-cluster application Bicep against that slot's
provisioning environment and application name. Azure resource placement now
comes entirely from `providers.azure.scope`, not a cross-group module scope
inside the Recipe. Management's application environment stays scoped to its
management app group; each child's later application environment stays scoped
to that child's app group. Cluster creation still runs only through management
Radius using the preallocated management-Radius identity.

The new environment must retain the proven private ACR authentication entry:
the original environment's successful pull does not configure another
environment automatically. Publish this changed Recipe under a new locked tag
and verify its digest; do not reuse the failed artifact reference. The parent
owns cleanup of the failed Radius gate record and the new live create/delete
test. No direct AKS creation, source fork, extra controller, or service-principal
fallback is introduced.

`postgresql.bicep` takes `delegatedSubnetId`, `privateDnsZoneId`, optional
`location`, `skuName` (`Standard_D2ds_v5`), `skuTier` (`GeneralPurpose`), setup
`administratorLogin` (`plane_setup`), secure `administratorPassword`, and tags.
It creates PostgreSQL 16 with 32 GiB storage, seven-day backup retention,
require-secure-transport enabled, and no HA. The default setup password is a
random GUID with complexity characters, **not** a deterministic name hash.
The parent confirmed this General Purpose SKU and PostgreSQL 16 support in
Central US; both SKU parameters remain overridable.

Its complete `result` is a `secureObject`. Within that output, only
`result.secrets.password` contains the password; its schema is `readOnly` and
`x-radius-sensitive`. No ordinary result value contains a password.
Do not put an explicit password in readable environment Recipe parameters.
The default is generated inside the Recipe.

### PostgreSQL initialization Secret

The same mixed-provider Recipe creates an Opaque `core/Secret@v1` in
`context.runtime.kubernetes.namespace`, using the pinned Kubernetes 1.0.0
extension bundled with Radius 0.60.2 / Bicep 0.42.1. Its name is
`${context.resource.name}-setup`; `result.values.setupSecretName` exposes only
that non-secret name as a read-only custom-type property.

Its `stringData` contains exactly:

| Key | Value |
| --- | --- |
| `host` | Provider-returned PostgreSQL hostname |
| `port` | String `"5432"` |
| `database` | Requested database name |
| `username` | Setup administrator login |
| `password` | Generated secure setup password |

The Kubernetes Secret ID is included in `result.resources` alongside the Azure
server/database/configuration IDs. The Secret depends on database creation and
the TLS configuration. A short-lived initialization Job reads only this named
Secret, builds its `BOOTSTRAP_DSN` without logging it, and initializes separate
runtime roles. It must use `sslmode=verify-full`; a TLS boolean alone does not
enable certificate/hostname verification.

Radius 0.60.2's dynamic resource GET redacts the password; do not invent a
`listSecrets` route to retrieve it. The initialization path uses
`setupSecretName` and the actual in-cluster Secret instead. Do not connect
runtime containers directly to this setup resource: automatic connections
could inject its administrative credentials. Runtime containers receive only
their separately initialized role DSNs and never mount the setup Secret.
Limit Secret access to the initialization path; do not log rendered Secret
manifests, Secret bodies, or Job environments.

After successful initialization, the parent deletes the setup Job and its
setup Secret. **Reapplying the Recipe can regenerate the setup password and
recreate that Secret.** The runtime role DSNs are separate and remain unchanged.
The parent must detect and delete the recreated setup Secret again, even when
no initialization is needed; never race an initialization Job with Recipe
reapplication. Do not treat deleting the Secret once as permanent revocation.

This adds a PostgreSQL **mixed Azure/Kubernetes live gate**, not just an ARM
compile check: prove Secret creation in the right namespace, Job-only access,
successful private initialization, Secret deletion, safe recreation/deletion
on reapply, and Radius deletion after the setup Secret was already removed.
Inspect actual resource GETs and logs for credential leakage.

`redis.bicep` still backs `Applications.Datastores/redisCaches`. It takes
`privateEndpointSubnetId`, `privateDnsZoneId`, optional `location`,
`skuName` (`Balanced_B0`), `highAvailability` (`Disabled`), and tags. Its secure
result contains host/port/username and **`tls: true`** in `values`, and
`uriComponent(listKeys().primaryKey)` in `secrets.password`. Keep the application
connection named lowercase `redis`. Use the generated URL once, or decode the
standalone password exactly once. Never decode the whole URL.

### Gateway details

`gateway.bicep` takes `gatewaySubnetId`, `gatewaySubnetCidr`, `nodeSubnetName`,
`apiPrivateIp`, `challengePrivateIp`, `gatewayIdentityId`, optional `location`,
`apiHealthPath` (`/healthz`), and tags. `phase` and the non-secret certificate URI
default from the resource properties. HTTPS requires a nonempty, versionless
`https://VAULT.vault.azure.net/secrets/NAME[/]` reference. Neither certificate
bytes nor private keys belong in the custom type.

The two private Kubernetes `LoadBalancer` Services use:

```text
radapp.io/application = lowercase(context.application.name)
radapp.io/resource    = lowercase(apiService or challengeService)
```

These are Radius 0.60.2's actual container renderer selectors. The common
contract therefore supplies **Radius container resource names**, not an
arbitrary Kubernetes Service whose selectors the Recipe would have to discover.
Azure ILB annotations request the preallocated IPs in the named node subnet;
source ranges restrict those backends to the gateway subnet.

The Recipe creates one real Standard_v2 Application Gateway, capacity 1, and a
static Standard public IP with Azure DNS. `hostname`, if supplied, is an Azure
DNS **label preference**, not a custom domain or full hostname. Azure DNS label
reuse protection may alter the final name; always consume `result.values.host`.

* `challenge`: HTTP `/.well-known/acme-challenge/*` and the default HTTP route
  go only to the challenge responder. That responder must serve only exact
  installed tokens and return 404 for every other path. There is no HTTP API
  routing rule and no HTTPS listener.
* `https`: the HTTPS listener uses the versionless Key Vault secret and routes
  to the API backend. HTTP challenges still go to the responder; every other
  HTTP path redirects to HTTPS. Reapplying this same phase retains the declared
  listener, certificate reference, public IP, and fixed backend IPs.

The API probe requires HTTP 200 at `apiHealthPath`. The challenge probe requests
`/.well-known/acme-challenge/health-probe` and expects 404; do not install a token
with that name. A challenge responder with a special HTTP health endpoint that
returns 200 at another path would violate the default-404 bootstrap contract.

The mixed-provider template imports Kubernetes **1.0.0** and emits both
`core/Service@v1` resources and Azure network resources. The Recipe-local
`bicepconfig.json` explicitly selects `kubernetes: builtin:`. That extension is
bundled with the pinned Radius 0.60.2 Bicep 0.42.1 compiler, not a guessed OCI tag.
`result.resources` contains both Kubernetes IDs and Azure IDs.

## Compilation and evidence

The current folder map is in [README.md](../README.md). Only the three plane
declarations live in `infra/radius/apps/`; reusable deployment templates live
in `infra/radius/modules/`. Types define APIs, Recipes implement them, and
`infra/radius/environments/azure.bicep` selects the implementations. The local
three-plane environment is not implemented. Historical evidence below retains
the paths and source counts from its original run.

Run `make check-bicep` for the complete current compile set, including generated
extensions, applications, modules, and environment. This is source validation,
not a cloud deployment or a rerun of the historical integration gates.

On 2026-09-09, the source was validated with:

```text
rad                  v0.60.2 (a3916f884df2e412c4cb662db63132bcf1344ca8)
~/.rad/bin/bicep      0.42.1 (caea9302e8)
Kubernetes import    provider Kubernetes, version 1.0.0
```

From the repository root, the exact compile loop is:

```sh
for file in infra/bootstrap/*.bicep \
  infra/radius/recipes/azure/{cluster,postgresql,redis,gateway}.bicep; do
  printf '\n==> %s\n' "$file"
  ~/.rad/bin/bicep build "$file" --stdout >/dev/null || exit
done
```

Generate the three ignored local extensions before compiling application files:

```sh
mkdir -p infra/radius/types/.build
for type in clusters postgresql gateways; do
  TMPDIR="$PWD/infra/radius/types/.build" rad bicep publish-extension \
    --from-file "infra/radius/types/$type.yaml" \
    --target "infra/radius/types/$type.tgz" --force || exit
done
```

The aliases in `infra/radius/bicepconfig.json` are `clusters`, `postgresql`, and
`gateways`; applications declare the ones they use alongside `extension radius`.
All three YAML files generated extensions successfully. A scratch Bicep
consumer importing all three extensions and declaring all three types compiled
successfully, including optional empty challenge-phase hostname/certificate
fields. The CLI reports its documented experimental-feature warning.

The initial full compile finished at **2026-09-09T07:11:33Z**, exit 0. It covered
**13 Bicep source files** with no compiler or linter
diagnostics. Structural inspection of the compiled ARM verified one direct
management AKS, no role definitions/assignments in any runtime Recipe, secure
PostgreSQL/Redis results, private datastore access, explicit Redis TLS/encoding,
and the two Kubernetes Services plus real Application Gateway. These checks
prove source/serialization contracts, not running infrastructure.
After aligning PostgreSQL's defaults with the parent's verified regional SKU,
`~/.rad/bin/bicep build infra/radius/recipes/azure/postgresql.bicep --stdout`
passed again at **2026-09-09T07:13:17Z** with no diagnostics. Compiled JSON
assertions confirmed `Standard_D2ds_v5`, `GeneralPurpose`, PostgreSQL 16, and
the `secureObject` result.

### Review-correction validation (F003–F005)

The initial syntax checks did not detect the subsequently reviewed scoping,
federation concurrency, and vault permission findings. The current root uses
explicit `resourceGroup('rg-${prefix}-${slot}-cluster/app')` module scopes,
literal management group names outside loops, and explicit dependencies on
group creation. Do not simplify these back to `clusterGroups[i]`/`appGroups[i]`:
Bicep 0.42.1 generated unbound `copyIndex()` expressions in management references
and incorrect child scopes from that form.

Run the new cloud-free checks with the project interpreter:

```sh
uv run --no-sync python -m unittest discover -s infra/bootstrap/tests -v
uv run --no-sync ruff check infra/bootstrap/tests
uv run --no-sync ruff format --check infra/bootstrap/tests
```

These tests compile the actual bootstrap and cluster Recipe call sites, reject
unbound loop indices, check management versus child group references, verify
serial federation, and inspect every vault grant's object scope and data
actions. They also require the two deterministic names in allocation outputs
and exactly one vault with no placeholder secret/certificate material. ARM
validation and the object-before-creation/negative-authorization gate still
belong to the parent; a compiler success is not a substitute.

The corrected **13 Bicep files**, including the inlined object-scope ARM module,
compiled without diagnostics at **2026-09-09T08:05:06Z**. The regression suite
passed **9 tests** at **2026-09-09T08:07:11Z**, and Ruff checks and formatting
validation passed. The local `.gitignore` exception keeps
`vault-object-access.json` as source despite the repository's generated ARM
JSON ignore rule.

### PostgreSQL setup-Secret validation

The mixed PostgreSQL Recipe and a generated-type Bicep consumer of
`database.properties.setupSecretName` compiled successfully at
**2026-09-09T08:12:16Z**. The local PostgreSQL extension was regenerated with
`rad bicep publish-extension --from-file infra/radius/types/postgresql.yaml
--target infra/radius/types/postgresql.tgz --force`, using the project-local
`TMPDIR` shown above. Generated scratch files were removed.

At **2026-09-09T08:13:27Z**, all **10 compiled-infrastructure tests** and Ruff
checks/format validation passed. The added test requires Kubernetes import
1.0.0, the Opaque setup Secret's exact name/namespace and five string fields,
its server/database/TLS dependencies, lifecycle resource ID, secure password
parameter/result, and a non-secret `setupSecretName` output. These are local
source/serialization checks; no mixed-provider deployment or initialization
Job execution is claimed.

### Flat cluster Recipe validation (F017)

The flat Recipe compiled with Bicep 0.42.1 at
**2026-09-09T09:45:10Z**, template hash **`11784538854577107589`**.
Its only resource types are native AKS and managed-identity federated
credentials; the compiled JSON contains no `Microsoft.Resources/deployments`
declaration or reference, no identity creation, and no role assignment.
All eight existing non-secret result values are preserved.

The regression suite now includes a flat-template guard and comparison against
the working management AKS authentication/network/node settings. All **11 tests**,
Ruff checks, formatting validation, and a fresh flat-Recipe compile passed at
**2026-09-09T09:49:09Z**; the template hash was unchanged. The parent
must still publish the new artifact, create the per-slot environment with
private-registry authentication, and repeat the actual Radius-owned
create/delete gate. These source checks do not claim the live gate passed.

Azure MCP schema lookup for PostgreSQL returned an unsupported-schema error;
the official `2024-08-01` ARM reference and the pinned compiler supplied the
schema validation instead. MCP's subscription-policy lookup used a principal
without access and returned 403. The explicitly scoped Azure CLI read succeeded
and showed the three Defender protection initiatives for SQL/Arc SQL,
open-source relational databases, and data protection. No policy was changed.

## Required live gates and cleanup

No Azure deployment, role assignment, image publication, certificate issuance,
or live resource deletion is claimed by these source checks. Before expanding
to five clusters, the parent workflow must prove:

1. Radius identity creates a child AKS; coordinator AKS creation and runtime
   Azure role grants are denied. Child Radius cannot change its own AKS.
2. Entra access from a management pod, child Radius install without Contour,
   private registry pulls, and a real smoke container.
3. PostgreSQL initialization from its actual cluster and protected Radius
   setup Secret materialization, with no readable credential outputs. Prove
   mixed-provider creation/deletion and setup Secret cleanup after reapply;
   runtime applications must retain only separate role DSNs.
4. Mixed Azure/Kubernetes Recipe creation **and deletion**, ILB endpoints and
   selectors, challenge-only HTTP, public ACME issuance, private vault import,
   trusted HTTPS, negative demo-key checks, and idempotent HTTPS reapply.
   Prove per-slot object-scope prebinding/first creation and cross-slot denial
   before using real certificate/account material.
5. Generated NIC/node/disk tags and actual Cilium enforcement on a pooled
   database connection. A successful compile is not proof of any of these.

Delete child applications/gateways/datastores through child Radius before
deleting their clusters through management Radius. Remove management's
Radius-owned application resources before direct management/bootstrap teardown.
Keep identities, registry artifacts, DNS, and vault until dependents are gone.
After removing assignments, delete the four project custom role definitions
by the recorded IDs. Report the purge-protected vault's soft-deleted record
separately; it cannot be purged during retention and is not a running demo
resource.

## References

* [Radius 0.60.2 renderer labels](https://github.com/radius-project/radius/blob/v0.60.2/pkg/kubernetes/labels.go)
* [Radius 0.60 Kubernetes Bicep Recipe](https://github.com/radius-project/docs/blob/v0.60/docs/content/tutorials/create-recipe/recipes/bicep/kubernetes-postgresql.bicep)
* [Radius 0.60.2 sensitive custom schemas](https://github.com/radius-project/radius/blob/v0.60.2/test/functional-portable/dynamicrp/noncloud/resources/testdata/testresourcetypes.yaml)
* [Radius 0.60.2 Recipe provider-scope construction](https://github.com/radius-project/radius/blob/v0.60.2/pkg/recipes/driver/bicep/bicep.go)
* [Radius 0.60.2 default deployment scope](https://github.com/radius-project/radius/blob/v0.60.2/pkg/sdk/clients/providerconfig.go)
* [Radius deployment-engine scope architecture](https://github.com/radius-project/radius/blob/v0.60.2/docs/architecture/deployment-engine.md)
* [AKS precreated kubelet identity](https://learn.microsoft.com/azure/aks/pre-created-kubelet-managed-identity)
* [Current AKS system-pool sizing](https://learn.microsoft.com/azure/aks/use-system-pools)
* [AKS tagging and propagation](https://learn.microsoft.com/azure/aks/use-tags)
* [AKS managedClusters 2025-05-01](https://learn.microsoft.com/azure/templates/microsoft.containerservice/2025-05-01/managedclusters)
* [PostgreSQL Flexible Server 2024-08-01](https://learn.microsoft.com/azure/templates/microsoft.dbforpostgresql/2024-08-01/flexibleservers)
* [Application Gateway and private Key Vault](https://learn.microsoft.com/azure/application-gateway/key-vault-certs)
* [Key Vault object-scope role assignments](https://learn.microsoft.com/azure/key-vault/general/rbac-guide)
* [Key Vault supported RBAC scopes](https://learn.microsoft.com/azure/key-vault/general/rbac-migration)
* [Application Gateway 2024-07-01](https://learn.microsoft.com/azure/templates/microsoft.network/2024-07-01/applicationgateways)
