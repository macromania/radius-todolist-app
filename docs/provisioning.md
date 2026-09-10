# Management provisioner and Azure plane deployment

These modules implement the administrative run path, not a resumable workflow.
Their unit tests exercise commands with controlled responses. They do **not**
prove the Azure integration gates or authorize a deployment.

## Entrypoints and ownership

* `python -m plane_demo.management.provisioner` runs only in management. It holds
  `provisioner_session()` for its lifetime, marks old running operations
  interrupted once, and claims pending work every five seconds.
* `uv run python operations/register-radius.py --slot SLOT --config FILE`
  registers types, credentials, and the environment in an **existing** cluster.
* `uv run python operations/deploy-plane.py --slot management --config FILE`
  initializes management PostgreSQL and deploys management workloads/gateway.
  Child slots are accepted for explicit administrative deployment, not creation.

Bootstrap and integration gates run first. The operator has already installed
management Radius, published reviewed Recipes and image digests, and created
the identity/network allocation. No command here grants Azure roles. Only
`ensure_child_cluster()` submits child creation, through management's Radius
`child-cluster.bicep`; it never executes direct AKS or kind creation.

The management provisioner uses its namespace-scoped service-account token and
the cluster CA to reach management Radius. Its protected management kubeconfig
references the rotating token file; it does not copy a token or use management
Entra cluster-admin credentials. It may read Radius pods/services/deployments
and port-forward in `radius-system`. Child access uses the preassigned
coordinator Azure identity and non-admin AKS user credentials. Each child
installer uses that slot's **different**, preassigned Radius identity.

Runtime management startup does **not** call `rad workspace create`: that command
performs Helm discovery by listing Secrets, which the restricted service account
intentionally cannot do. Instead, startup seeds only the known management entry
in the project-local Radius configuration:

```yaml
workspaces:
  default: radplanes-management
  items:
    radplanes-management:
      connection:
        context: radplanes-management
        kind: kubernetes
      scope: /planes/radius/local/resourceGroups/radplanes
      environment: /planes/radius/local/resourceGroups/radplanes/providers/Applications.Core/environments/management
```

Existing child workspace entries are preserved, including when the CLI last
wrote YAML. The protected file is written as JSON, which is valid YAML. Startup
then performs actual `rad group show` and `rad environment show` API reads and
verifies their IDs before claiming pending work. A failed read stops startup;
writing configuration alone is not readiness evidence. No Helm Secret-list
permission is granted. Workstation registration and child bootstrap retain
their CLI workspace checks under their already-authorized Entra access.

Every Radius call supplies `.state/azure/radius.yaml`, a workspace, and a
slot-specific `KUBECONFIG`. It also sets `HOME` to
`.state/azure/homes/radplanes-SLOT`, whose `.kube/config` symlinks to the exact
slot kubeconfig and whose `.rad/bin/bicep` symlinks to the original bundled
compiler. Register and deploy helpers use this same provider command path.
No project contexts or files are written into the operator's global kubeconfig.
An unexpected existing link, redirected home directory, missing kubeconfig, or
missing/non-executable bundled compiler fails before the Radius command.
Every kubectl call supplies its context and kubeconfig.
Runtime Azure CLI state lives in `.state/azure/az`; workstation commands retain
the operator's existing CLI login without changing the selected subscription.
Azure calls specify the configured subscription. Workload federation is
refreshed from the projected token before Azure commands.

Radius 0.60.2's workspace loader ignores `KUBECONFIG` and reads the recommended
home kubeconfig. A real installation therefore reported
`the kubeconfig does not contain a context called radplanes-management` despite
the correct environment variable. Scoped `HOME` addresses that loader, not a
global-context workaround. `Commands` captures the original Azure CLI cache
before overriding any command's `HOME`; an explicit `AZURE_CONFIG_DIR` is
retained, and runtime authentication replaces it with the protected project
cache. The installer uses the matching `operations/project.py` helper. The command
runner never changes its own process's `HOME`, and each slot gets a distinct
home.

The actual Radius 0.60.2 CLI path calls
`pkg/cli/kubernetes.NewCLIClientConfig` → `kubeutil.NewClientConfigFromLocal`.
The generic in-cluster-first helper is not this call path. The earlier F032
child-targeting warning was withdrawn after tracing those callers; its proposed
environment filtering was removed. The runner preserves inherited
`KUBERNETES_SERVICE_HOST`/`KUBERNETES_SERVICE_PORT` and Azure workload-identity
variables. The separately reproduced scoped-HOME requirement (F015) and
restricted-account workspace seeding (F031) remain in place. Offline environment
tests check our command construction, not proof of the vendor CLI's internal
dispatch or a deployed cluster's identity.

## Non-secret immutable operator configuration

`/etc/plane-demo/provisioning.json` is mounted from the immutable
`provisioning-settings` ConfigMap. `PROVISIONING_CONFIG` can select another file.
The file is loaded once, validated, and recursively frozen.

```json
{
  "version": 1,
  "foundation": {},
  "allocations": {},
  "coordinatorIdentity": {},
  "managementCluster": {},
  "recipes": {},
  "images": {
    "api": "REGISTRY.azurecr.io/api@sha256:DIGEST",
    "provisioner": "REGISTRY.azurecr.io/provisioner@sha256:DIGEST"
  }
}
```

The empty objects and uppercase words above describe the schema, not deployable
defaults. Populate `foundation`, `coordinatorIdentity`, and `managementCluster`
with the corresponding **unwrapped** `.state/azure/bootstrap.outputs.json`
objects. The project/region are `radplanes`/`centralus`. Keep all foundation
fields documented in [azure-infrastructure.md](azure-infrastructure.md).
Convert bootstrap's allocation array once:

```python
allocations = {allocation["slot"]: allocation for allocation in bootstrap["allocations"]}
```

Supply management, shared-control/shared-data, and isolated-1-control/data.
Additional operator-allocated complete pairs are supported; no tenant limit is
implemented. Never derive resource groups, subnets, or identities from a public
request. Pair placement must match the immutable database assignment.
Each allocation must include `certificateName=gateway-SLOT`,
`acmeStateSecretName=acme-SLOT`, its `identities.certificateIssuer`, and
`certificateIssuerSubject=system:serviceaccount:radplanes-system:certificate-issuer`.
These names must match the operator's object-scoped vault role assignments and
federated identity. A different subject or certificate/account name fails
configuration validation before deployment.

Network values must match the bootstrap allocation, not merely parse as strings.
Gateway CIDRs are canonical IPv4 `/24` networks in `10.64.16.0/20`. For gateway
`10.64.(16+i).0/24`, the node network is `10.64.i.0/24`, with API and challenge
addresses exactly `.240` and `.241`. Management occupies index zero; other slots
must use distinct indices. If supplied, `nodeSubnetCidr` must match that derived
network. Validation does not depend on JSON dictionary ordering.

`foundation.egressIp` is a bare IPv4 address. `authorizedIpRanges` must be a
nonempty list of explicit public IPv4 `/32` entries including that NAT address.
List every observed operator egress separately; do not broaden a CIDR to handle
multiple operator IPs. Invalid, private, multicast, IPv6, or broader ranges fail
before provisioning. These checks enforce the existing bootstrap address plan,
not an additional tenant limit.

`recipes` is the dictionary from `recipes.json`, with keys `cluster`,
`postgresql`, `gateway`, and `redis`. Each requires `reference` (registry/path:tag,
without `br:`) and `digest` (`sha256:` plus 64 lowercase hexadecimal digits).
Extra publication metadata is harmless. Registration verifies the actual
registry digest and both disabled tag attributes: write and delete. It never
publishes, unlocks, or overwrites a tag. Images require digest references from
the same project registry.
Image manifest entries may also be objects with a `reference` field; the loader
validates that reference and normalizes it to the digest-pinned string.

Cluster Recipes run only in management Radius, but not in the ordinary
management application's Azure scope. For every child slot, the driver first
creates environment `provision-SLOT` on workspace `radplanes-management`, with
Azure provider scope set to that allocation's **cluster resource group**.
Its Recipe map contains only `Demo.Platform/clusters`, with a **one-entry**
allocation dictionary. Recipe registry authentication uses management's Radius
identity, not the coordinator or the not-yet-installed child Radius identity.
The child-cluster declaration is deployed as application `cluster-SLOT` in that
environment; its namespace prefix is `radplanes-p-SLOT`. No child cluster is deployed
in the management application resource group.

The ordinary management/control/data Recipe maps contain only their required
datastore and gateway Recipes. Each receives its slot's subnet, fixed IP,
identity, DNS, location, and tag parameters. PostgreSQL
administrator passwords are **not** stored in environment Recipe parameters.
Environment registration also passes `registryHost=foundation.registryLoginServer`,
`radiusClientId=allocations[SLOT].identities.radius.clientId`, and
`azureTenantId=foundation.tenantId`. These configure the environment's
Recipe-specific OCI authentication through an `azureWorkloadIdentity` SecretStore
at `radius-system/ENVIRONMENT-registry-auth`, referenced by
`recipeConfig.bicep.authentication[registryHost].secret`.
Azure provider credential registration and AcrPull alone do not configure that
Recipe downloader. Use the slot's Radius identity, not the coordinator identity;
do not copy Docker authentication or enable anonymous registry access.
Workspaces are created first with context only and again after group/environment
creation with defaults. The Radius group is `radplanes`; an environment is its
slot; applications are `management`, `control`, or `data`. The cluster-only
environments/applications described above are separate management-Radius
administrative resources. Delete each `cluster-SLOT` application only after
the corresponding child's applications and datastores have been removed.
Deleting application `management` alone does not delete those child clusters.

Namespace examples:

* `radplanes-management-management`
* `radplanes-shared-control-control`
* `radplanes-shared-data-data`

An operator-config change requires an explicit replacement of the immutable
ConfigMap and a controlled provisioner restart when no operation is running.
Do not remove its PVC or reset operation rows to simulate recovery.

## Credential schema and initialization

The operator file defaults to `.state/azure/credentials.json`, mode `0600`
inside a `0700` directory. New role passwords and independent API keys are
generated once with `secrets.token_urlsafe(48)`. They are written atomically
before initialization and reused; missing retained credentials are an error,
not a reason to reset database passwords.

```json
{
  "version": 1,
  "planes": {
    "management": {
      "demoKey": "GENERATED_MANAGEMENT_KEY",
      "passwords": {
        "mgmt_api": "GENERATED_PASSWORD",
        "mgmt_provisioner": "GENERATED_PASSWORD",
        "cp_shared": "GENERATED_PASSWORD",
        "cp_isolated_1": "GENERATED_PASSWORD"
      },
      "database": {
        "host": "ACTUAL.postgres.database.azure.com",
        "port": 5432,
        "database": "management",
        "serverId": "ACTUAL_ARM_RESOURCE_ID"
      }
    }
  }
}
```

Each control slot has its own `demoKey`, `database`, and password entries
`cp_api`, `cp_reconciler`, `dp_reconciler`. Each data slot has an independent
`demoKey` and an empty `passwords` object. The operator does not need to
pre-generate this file: `deploy-plane.py` generates the management section and
records the real database output. Child sections are generated by the provisioner
on its PVC.

The runtime `provisioner-runtime` Secret contains:

* `PROVIDER=azure`; `local` fails startup until the real local integration gate.
* `MANAGEMENT_DSN` for **mgmt_provisioner**, not the API or setup login.
* `PROVISIONING_CONFIG=/etc/plane-demo/provisioning.json`.
* `PROVISIONING_CREDENTIALS_JSON`: a restricted seed with management's database
  object and only `mgmt_provisioner` plus the allocated child-reporting passwords.
  It has neither the management API password nor its demo key.

Instead of secret environment JSON, a manually installed runtime may mount the
same restricted seed at `/etc/plane-demo/credentials.json` and set
`PROVISIONING_CREDENTIALS` if needed. The entrypoint validates the management DSN
against its generated keyword-form libpq DSN and requires the coordinator's
matching workload-identity client ID, tenant ID, and projected federation token.
The seed is copied into protected `.state/azure/credentials.json` only on first
startup. Later starts require its management section to match; they preserve
already generated child credentials. Never mount the full operator file in an API.

All Azure DSNs use libpq escaping, `sslmode=verify-full`,
`sslrootcert=/etc/ssl/certs/ca-certificates.crt`, and a bounded connect timeout.
Runtime Secrets contain only their process's roles:

| Secret | Connections |
| --- | --- |
| `management-api-runtime` | management `mgmt_api`, management key |
| `provisioner-runtime` | management `mgmt_provisioner`, restricted seed/config |
| `control-api-runtime` | local `cp_api`, control key |
| `control-reconciler-runtime` | local `cp_reconciler`, parent allocated reporting login |
| `data-reconciler-runtime` | control `dp_reconciler`, pair/project/namespace |
| `data-api-runtime` | data key, pair/project/namespace; **no parent DSN** |

Redis is injected through the lowercase Radius `redis` connection, not duplicated
by this coordinator. Data API RBAC grants only ConfigMap `get`; data reconciler
gets only `get/create/patch` in that data namespace. Public management/control
accounts and the challenge account have no service-account token. The provisioner
PVC uses a dedicated Azure Disk StorageClass with the required resource tags.
The application declaration must retain one replica and a non-overlapping
`Recreate` strategy; the session lock is an additional safeguard, not a rollout
strategy.
Both management application deployments (challenge and HTTPS) receive
`provisionerWorkloadIdentity=true` and
`provisionerClientId=coordinatorIdentity.clientId`. This preserves the
provisioner's service-account annotation in Radius's declared workload base,
instead of relying solely on the earlier Kubernetes annotation. Public API
service accounts do not receive that identity.

### Fresh database protocol

1. Check immutable `database-initialized` ConfigMap. If present, verify the
   stored server/database matches retained credentials; skip Recipe and SQL
   initialization and finish deleting any success-path setup artifacts.
2. Without the marker, reject any retained database metadata, existing
   `database-init` Secret/Job, `postgres-setup` Secret, Radius PostgreSQL resource
   named `postgres`, or `SLOT-database-intent.json`. Radius inventory lookup
   failures also stop the operation; they are not treated as an absent database.
   Do not replay or change passwords, even if no credentials file existed when
   an earlier integration gate created the database.
3. Generate/retain runtime passwords, then exclusively create
   `.state/azure/SLOT-database-intent.json` with mode `0600`. Flush the file and
   directory before invoking the Recipe. The intent contains only slot,
   application, and resource identifiers, and remains after both success and
   failure. A crash after Recipe submission but before database metadata is
   saved therefore cannot cause a later invocation to replay the Recipe.
4. Deploy `database.bicep` through that plane's Radius. Require PostgreSQL
   properties `host`, `port`, `database`, `username`, `tlsRequired=true`,
   `serverId`, and **`setupSecretName`**. The Recipe must materialize the named
   Secret in the application namespace, with its `password` data field.
5. Read that Secret in memory, construct the escaped verified-TLS setup DSN, and
   submit a dedicated `database-init` Secret through kubectl stdin. No password
   or DSN enters a command argument, parameter file, inventory, or log.
6. Create a tokenless Job using the API image and
   `python -m plane_demo.setup.bootstrap`, `BOOTSTRAP_KIND`, `ROLE_PASSWORDS_JSON`, and
   management `PAIR_SLOTS_JSON` or control `PAIR_ID`. It has zero retries and a
   deadline. Follow [contracts.md](contracts.md) for the actual SQL contract.
7. Only after Job success, create the marker with server/database/setup-Secret
   identifiers; delete the Job and both setup/init Secrets. If cleanup is
   interrupted, the marker permits cleanup without rerunning initialization.

No setup password is retained in the credential file or a runtime Secret.
Failure after SQL succeeds but before the marker is persisted remains an
explicit operator-cleanup case, not automatically adopted state.
There is no `PROMOTE_GATE`, automatic adoption, or intent-reset option. Remove
a gate's database through Radius and verify its provider resources are gone
before deploying the actual application. A failed initialized-plane attempt
requires operator cleanup of its verified owned resources and corresponding
state; deleting only its intent file is not a safe retry procedure.

## Redis NIC metadata outside Recipe tracking

F054 changes the lifecycle of **new** Redis resources. Radius 0.60.2 appends the
Bicep response's `OutputResources` to the Recipe's explicit `result.resources`.
Listing only the cache and private endpoint therefore does not prevent a
declared `Microsoft.Resources/tags` extension from being tracked. That type has
no usable delete API version in the pinned Radius path.

The Redis Recipe now declares only the cache, database, private endpoint, and
DNS zone group. It neither declares an existing NIC nor emits a tags extension.
TLS, private access, and the secure percent-encoded password output are unchanged.
The separately packaged
`plane_demo.management.providers.redis_nic_tags` helper applies NIC metadata
through a short-lived Job, outside the Radius resource lifecycle.

`deploy_plane()` calls `tag_redis_nic()` after **both** data-application Radius
deployments, including an HTTPS reapply. Management/control deployment paths do
not run it. Metadata failure stops deployment rather than publishing an endpoint
or claiming infrastructure completion.

The Job uses the existing `radius-system/applications-rp` service account and
that slot's Radius workload identity. The caller verifies the account's name,
namespace, client-ID annotation, and tenant annotation against operator config.
The Job has `azure.workload.identity/use=true`, but no Kubernetes API token mount,
new RBAC, Azure role grants, or human credentials. Its helper validates downward
API namespace/account values and the projected Azure identity variables before
obtaining a token with `WorkloadIdentityCredential`. It is absent from the public
API image allowlist and included by the privileged provider-directory copy.

Before any PATCH, the helper verifies the app group's required tags, locates
exactly one cache carrying the expected Radius resource linkage, and reads the
actual cache and private endpoint. Both must be succeeded, in the expected
region/group, and carry the required project, application, environment, and
resource tags. The endpoint must target that cache's `redisEnterprise`
subresource with an approved connection and the allocated subnet. Its custom
NIC name and single NIC reference must agree with the Recipe's naming contract;
the NIC must be in the same app group and link back to that endpoint.

The only write is ARM `PATCH .../providers/Microsoft.Resources/tags/default`
using API `2021-04-01` and operation `Merge` on that verified NIC. Existing
conflicting ownership tags fail rather than being overwritten. Unrelated tags
and network properties are preserved; live readback confirms required tags and
unchanged network properties, ignoring read-only ETag revisions. An already
correct NIC needs no PATCH. Neither Redis access keys nor private logs are read.

ARM requests use a 60-second aggregate budget, at most 10 seconds per request
(5 seconds to connect), no redirects, and at most three verification reads.
Workload-token acquisition uses bounded transport timeouts with retries disabled.
The Job has zero retries and a 180-second deadline; the caller polls for at most
200 seconds with bounded Kubernetes requests. Successful Jobs are deleted;
failed/orphaned Jobs remain an explicit operator cleanup case. Safe error codes
are returned through the owned Pod's termination message, not arbitrary logs.

A parent-operated **fresh** lifecycle gate can use the same action after
deploying its separately named Radius Redis resource:

```python
provider.tag_redis_nic(
    "shared-data",
    resource_name="redis-lifecycle",
    application="redis-lifecycle",
    environment="shared-data",
)
```

The result contains only `cacheId`, `privateEndpointId`, and `nicId`. The gate
must use a newly published immutable Recipe and an inspected tool image that
contains this helper. Existing resources that already track the old tags
extension are **not** repaired by this change: redeployment may attempt to
garbage-collect that old record and fail. Do not reapply this Recipe to running
data planes as a recovery technique, edit backing stores, or force adoption.
Live create/tag/delete proof remains a separate parent-owned gate.

## In-cluster certificate issuance

The driver calls the parent-owned `operations/run-certificate-job.py` coordinator.
Only that wrapper creates issuance Jobs; the provider has no duplicate Job
implementation. The in-cluster `operations/issue-certificate.py` is never run
directly on the laptop.

The optional `certificateCommand` configuration is an argument-vector array.
Omit it or use `[]` for portable defaults:

* Inside `/app`: `["python", "/app/operations/run-certificate-job.py"]`.
* On the operator machine: the current Python executable and the absolute
  project path to `operations/run-certificate-job.py`.

When constructing management's ConfigMap, deployment fills an omitted/empty
`certificateCommand` with the explicit container command shown above. The
operator-side configuration remains unchanged.

An explicit override is trusted operator configuration, never a shell fragment
or tenant input. Avoid putting a workstation-only executable path into the
ConfigMap mounted inside the provisioner. The layout change does not rewrite
saved operator configuration or live ConfigMaps: before deploying the new image,
regenerate any override that still names `/app/scripts/run-certificate-job.py`
to use `/app/operations/run-certificate-job.py`. Existing image evidence applies
to the original layout, not the rebuilt image. The driver appends:

```text
--slot SLOT --context radplanes-SLOT --namespace APPLICATION_NAMESPACE
--kubeconfig PROJECT_STATE/SLOT.kubeconfig --domain ACTUAL_PROVIDER_HOST
--config PROJECT_STATE/provisioning.json
```

The Job runs in `radplanes-system` as `certificate-issuer`, matching the
operator-prebound federated subject. The wrapper creates that namespace and
service account, annotates the account with the allocation's issuer client ID
and tenant, and labels the Job pod `azure.workload.identity/use=true`.
An application-namespace RoleBinding grants this **system-namespace account**
only `get` and `patch` on the single `acme-challenges` ConfigMap. It has no
ConfigMap create/list permission, no Secret permission, and no Azure role grants.

Its command is:

```text
python /app/operations/issue-certificate.py
  --slot SLOT --domain ACTUAL_PROVIDER_HOST --namespace APPLICATION_NAMESPACE
  --vault-name ALLOCATED_VAULT --certificate-name gateway-SLOT
  --account-secret acme-SLOT
```

`--namespace` names the application's challenge ConfigMap namespace, **not**
the Job namespace. Ordinary plane deployment uses production issuance/reuse;
staging certificates cannot pass its final trusted-HTTPS check.

The issuer uses WorkloadIdentityCredential and the scoped Key Vault APIs,
Certbot HTTP-01, and the existing public-challenge hook. There is no setup
database credential, parent DSN, or demo key in the certificate Job. Job storage,
identity projection, polling, and cleanup belong to the wrapper.

The wrapper polls the Job and reads the issuer container's non-secret termination
message. It returns only this JSON on stdout:

```json
{"certificateSecretUri":"https://VAULT.vault.azure.net/secrets/gateway-SLOT"}
```

The driver checks the wrapper's exit status and accepts at most 4 KiB of JSON
with precisely the `certificateSecretUri` field. A missing/malformed result,
extra fields, a different vault/name, or a versioned URI fails provisioning.
Only the allocation's versionless certificate URI is accepted. No private
issuer logs are retrieved or parsed. The gateway host comes from Radius,
never a guessed DNS label.

Before invoking the wrapper, the driver rejects any pre-existing
`certificate-SLOT` Job, including an incomplete or failed attempt. It does not
ask the wrapper to reset or adopt that work. Cleanup remains an explicit
operator action. The wrapper deletes successful Jobs; the account and narrow
RoleBinding remain. Healthy reapplication invokes the wrapper again, while the
issuer itself reuses a valid certificate from Key Vault.

The application is deployed in challenge mode, the in-cluster Job issues its
certificate, and Radius reapplies the same application in HTTPS mode with the
returned URI. The driver checks the returned host/certificate and uses ordinary
trusted `curl` against `/livez` (no key, `-k`, or custom CA). This is administrative
liveness, **not** tenant-ready or Redis-health evidence. A successful reapply
uses the retained HTTPS URI rather than downgrading to challenge mode.

## Completion, failures, and output

An available database pair reuses its validated IDs/endpoints without any
Radius calls or child redeployment. A new pair creates control and data cluster
resources sequentially, reads their actual IDs, obtains child access, installs
child Radius, initializes credentials/databases, deploys applications, and
completes certificates. Only then does `OperationStore.complete()` mark the
pair available. The control child independently pulls its tenant and reports
creation; the parent never sends tenant configuration to a child HTTP API.

Stable stage/error identifiers enter `OperationStore.observe()`. Sanitized
subprocess stderr retains operator diagnostics separately. Commands use argument
vectors, explicit exit checks, and bounded timeouts. Each command checks the
**same** lock-holding PostgreSQL connection before and during execution. A lost
connection terminates the owned command process group and exits the provisioner;
it does not reconnect. Already accepted remote Azure deployments or Kubernetes
Jobs may finish after local command termination. Inspect those resources before
manual cleanup; the coordinator does not promise remote cancellation or rollback.

Healthy output is `.state/azure/endpoints.json`:

```json
{
  "management": {"url": "https://ACTUAL.cloudapp.azure.com", "key_file": "management.key"},
  "pairs": {
    "shared": {
      "control": {"url": "https://ACTUAL.cloudapp.azure.com", "key_file": "shared-control.key"},
      "data": {"url": "https://ACTUAL.cloudapp.azure.com", "key_file": "shared-data.key"}
    }
  }
}
```

This is the existing `harness/api.py` contract. Key files are separate and
protected. `SLOT-endpoint.json` retains the non-secret HTTPS URL/certificate URI.
The CLI prints only slot/URL JSON. `SLOT-certificate.json` is written immediately
after issuance, before the HTTPS deployment, so a subsequent administrative
reapply cannot downgrade an already-issued gateway after a failed health check.
The provider's child endpoint/key files live on the management PVC, but operator
access does not require copying them. `harness/export-state.py` independently
discovers actual gateway/PIP DNS, cluster and namespace UIDs, and workload names,
and reads only each named API runtime Secret's `DEMO_KEY` into protected operator
state. It never reads `credentials.json`.

Run `uv run python harness/export-state.py --watch --timeout 7200` during
onboarding. Wait for a fresh `.state/azure/export-status.json` reporting
`ready_for_onboarding: true` before beginning the API scenario; the exporter
continues publishing child targets as they appear. Missing resources remain
pending, while authentication or scope errors fail. See the
[acceptance exporter contract](../tests/harness/README.md) for `--once`, status fields,
and target-ownership checks. Export readiness is not proof that acceptance
passed. Never dump the full credentials file or Kubernetes Secrets into terminal
evidence.

Run source verification without cloud access:

```sh
uv run ruff check src/plane_demo/management operations/deploy-plane.py \
  operations/register-radius.py tests/unit/test_provisioner.py
mkdir -p .state/check/tmp
TMPDIR="$PWD/.state/check/tmp" uv run pytest tests/unit/test_provisioner.py
```
