# Management provisioner and Azure plane deployment

These modules implement the administrative run path, not a resumable workflow.
Their unit tests exercise commands with controlled responses. They do **not**
prove the Azure integration gates or authorize a deployment.

## Entrypoints and ownership

* `python -m plane_demo.management.provisioner` runs only in management. It holds
  `provisioner_session()` for its lifetime, marks old running operations
  interrupted once, and claims pending work every five seconds.
* `make bootstrap` completes the selected foundation and management Radius.
* `make build` builds and inspects the selected artifacts. Azure runs bootstrap
  first; local runs build first.
* `make deploy-management` discovers inputs and deploys management. On Azure it
  stages one owned, suspended Job and waits for completion. On local it registers
  prepared Recipes and enters the namespace-owned operator Lease.
* `deploy-plane.py --slot management --config FILE` is the Azure Job's internal
  command. It accepts the Job's owned inputs, requires its guard and uses service
  credentials. Child deployment and legacy workstation credential files are
  not supported by this entrypoint.

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

Every Radius call supplies a configuration in its private temporary workspace,
a slot-specific workspace name and `KUBECONFIG`. Its scoped `HOME` exposes the
exact kubeconfig and original bundled Bicep compiler through checked symlinks.
Register and deploy helpers use this same provider command path.
No project contexts or files are written into the operator's global kubeconfig.
An unexpected existing link, redirected home directory, missing kubeconfig, or
missing/non-executable bundled compiler fails before the Radius command.
Every kubectl call supplies its context and kubeconfig.
Runtime Azure CLI state lives in the disposable provider workspace; workstation commands retain
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
cache. The installer uses the matching `scripts/operations/project.py` helper. The command
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

## Public identity and discovered configuration

The immutable `provisioning-settings` ConfigMap contains only public starting
settings from `.env`. The worker consumes them as environment variables, not a
mounted inventory file. `PROVISIONING_CONFIG` and credential-seed files cannot
activate the worker.

Startup checks the actual namespace owner, Radius environment and deployed
images. Azure reads the exact selected subscription deployment outputs and
registry APIs using its preassigned identity. Local reads consumed Recipe
bindings, image IDs, node address, Kubernetes Service and CA. The resulting
`OperatorConfig` or `LocalConfig` is validated and frozen in memory. Any
temporary parameter/configuration files are command inputs, not discovery or
recovery authority.

The Azure bootstrap-output fields remain defined in
[azure-infrastructure.md](azure-infrastructure.md). The caller converts the
allocation array to a slot-keyed object; the operator does not assemble it by
hand.

Supply management, shared-control/shared-data, and isolated-1-control/data.
Additional operator-allocated complete pairs are supported; no tenant limit is
implemented. Never derive resource groups, subnets, or identities from a public
request. Pair placement must match the immutable database assignment.
For a selected `.env` identity, `STEM` is `PROJECT-DEPLOYMENT-azure`.
Each allocation includes `certificateName=gateway-STEM-SLOT`,
`acmeStateSecretName=acme-STEM-SLOT`, its `identities.certificateIssuer`, and
`certificateIssuerSubject=system:serviceaccount:STEM-system:certificate-issuer`.
These names must match the operator's object-scoped vault role assignments and
federated identity. A different subject or certificate/account name fails
configuration validation before deployment.
Selected configuration is required by the normal operator and worker entrypoints.

The shared typed identity lives in
`plane_demo.management.providers.identity`; `scripts/operations/config.py`
retains `.env` I/O and re-exports its public types. Selected identity is distinct
from discovered configuration. Administrative DTOs may include public
`bootstrapIdentity` settings, but must not include optional demo-key values.
Providers derive Radius groups, contexts, namespaces, and project labels from
that identity and use an explicit temporary workspace. The worker has no working
PVC or durable workstation-directory dependency.

Azure Radius installation uses the native CLI helper:

```text
bash scripts/operations/install-radius.sh
  --context CONTEXT --kubeconfig WORKSPACE/SLOT.kubeconfig
  --config WORKSPACE/radius.yaml --workspace-root WORKSPACE
  --client-id RADIUS_CLIENT_ID --tenant-id TENANT_ID
```

It preserves the selected Azure CLI cache while using a disposable private HOME.
Every cluster command names its kubeconfig/context, and every Radius command
names its configuration. It verifies client/tenant identity projection for all
four Radius service accounts before returning success.

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

The discovered `recipes` dictionary has keys `cluster`,
`postgresql`, `gateway`, and `redis`. Each requires `reference` (registry/path:tag,
without `br:`) and `digest` (`sha256:` plus 64 lowercase hexadecimal digits).
Selected registration verifies the effective ACR ABAC repository-permission
boundary before checking canonical Recipe tags and digests. Radius 0.60.2 does
not support digest-only Recipe references. Canonical `radius-recipes/*` writes
are restricted to trusted ARM import; publisher data writes are limited to
runtime image and staging repositories. Reversible tag locks are not this
boundary. Images require digest references from the same project registry.
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
environment; its namespace prefix is `STEM-p-INDEX`, where child indexes are 1-4.
No child cluster is deployed
in the management application resource group.

Management exposes all four Recipe bindings for worker discovery. Control and
data expose their required datastore and gateway Recipes. Each receives its slot's subnet, fixed IP,
identity, DNS, location, and tag parameters. PostgreSQL
administrator passwords are **not** stored in environment Recipe parameters.
Environment registration also passes `registryHost=foundation.registryLoginServer`,
`radiusClientId=allocations[SLOT].identities.radius.clientId`, and
`azureTenantId=foundation.tenantId`. These configure the environment's
Recipe-specific OCI authentication through an `azureWorkloadIdentity` SecretStore
at `radius-system/ENVIRONMENT-registry-auth`, referenced by
`recipeConfig.bicep.authentication[registryHost].secret`.
Azure provider credential registration alone does not configure that
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

A public identity change requires explicit replacement of the immutable
ConfigMap and a controlled provisioner restart when no operation is running.
Do not reset operation rows to simulate recovery.

## Credential schema and initialization

`StoredCredentials` reads Azure values from the selected Key Vault and local
values from owned immutable Kubernetes Secrets. Keys and role passwords are
created once under the administrative writer guard, then reused. Missing
credentials for an existing plane fail rather than rotating an established
database password. Optional `.env` demo keys are persisted into the real owner
before public configuration is handed to the worker.

Management retains `mgmt_api`, `mgmt_provisioner` and the allocated control
reporting passwords. Each control slot retains `cp_api`, `cp_reconciler` and
`dp_reconciler`; every API slot has an independent demo key. Database connection
metadata is read through Radius when a DSN is needed, not stored alongside
these credentials. Private kubeconfigs, decoded SDK certificates and CLI caches
are removed with their command workspace.

The runtime `provisioner-runtime` Secret contains:

* `PROVIDER=azure` or `PROVIDER=local`, matching the selected public identity.
* `MANAGEMENT_DSN` for **mgmt_provisioner**, not the API or setup login.

It contains no credential replay seed or full provisioning inventory. Existing
obsolete fields are removed with Secret UID/resourceVersion preconditions.
Startup checks its injected DSN against the service-owned password and current
database binding. Azure also requires the exact coordinator workload identity.
An empty worker workspace is normal, not a password-recovery mechanism.

All Azure DSNs use libpq escaping, `sslmode=verify-full`,
`sslrootcert=/etc/ssl/certs/ca-certificates.crt`, and a bounded connect timeout.
Runtime Secrets contain only their process's roles:

| Secret | Connections |
| --- | --- |
| `management-api-runtime` | management `mgmt_api`, management key |
| `provisioner-runtime` | management `mgmt_provisioner` and provider selection |
| `control-api-runtime` | local `cp_api`, control key |
| `control-reconciler-runtime` | local `cp_reconciler`, parent allocated reporting login |
| `data-reconciler-runtime` | control `dp_reconciler`, pair/project/namespace |
| `data-api-runtime` | data key, pair/project/namespace; **no parent DSN** |

Redis is injected through the lowercase Radius `redis` connection, not duplicated
by this coordinator. Data API RBAC grants only ConfigMap `get`; data reconciler
gets only `get/create/patch` in that data namespace. Public management/control
accounts and the challenge account have no service-account token. The provisioner
does not use a working-state PVC.
The application declaration must retain one replica and a non-overlapping
`Recreate` strategy; the session lock is an additional safeguard, not a rollout
strategy.
Both management application deployments (challenge and HTTPS) receive
`provisionerWorkloadIdentity=true` and
`provisionerClientId=coordinatorIdentity.clientId`. This preserves the
provisioner's service-account annotation in Radius's declared workload base,
instead of relying solely on the earlier Kubernetes annotation. Public API
service accounts do not receive that identity.

### Database initialization and observation

1. Query Radius for the current PostgreSQL resource. A lookup error is not
   absence. Before using its connection properties, check readiness,
   application/environment ownership, database name, and setup username.
2. For an existing database, run a short-lived observation Job using its
   existing runtime login. `BOOTSTRAP_MODE=observe` selects a read-only
   transaction that verifies the committed schema version, source, configuration,
   and catalog fingerprint. It receives no role-password bundle and performs no
   DDL. Missing credentials, missing metadata, and schema drift stop the
   operation rather than triggering initialization.
3. For an absent database, reject surviving init/setup resources and either
   plane runtime Secret. Generate or retain runtime passwords, then exclusively
   create the actual `database-init` Secret before invoking the Recipe. This
   resource records that initialization started; no workstation intent file is
   required. A repeated invocation cannot overwrite it and replay the submission.
4. Deploy `database.bicep` through that plane's Radius. Require PostgreSQL
   properties `host`, `port`, `database`, `username`, `tlsRequired=true`,
   `serverId`, and **`setupSecretName`**. The Recipe must materialize the named
   Secret in the application namespace, with its `password` data field.
5. Read that Secret in memory, construct the escaped verified-TLS setup DSN, and
   add it to the existing `database-init` Secret through kubectl stdin. The setup
   password and DSN do not enter command arguments, parameter files, inventories,
   or logs.
6. Create a tokenless Job using the API image and
   `python -m plane_demo.setup.bootstrap`, `BOOTSTRAP_KIND`, `ROLE_PASSWORDS_JSON`, and
   management `PAIR_SLOTS_JSON` or control `PAIR_ID`. It has zero retries and a
   deadline. Follow [contracts.md](contracts.md) for the actual SQL contract.
7. The bootstrap transaction commits schema metadata with its DDL. After Job
   success, delete the Job and setup/init Secrets. A later invocation can
   verify the committed database and finish cleanup without replaying DDL.
   Observation Jobs use unique names and remove their temporary Job/Secret.
   Neither a ConfigMap nor an operator file is initialization authority.

No setup password is retained in the service credential store or a runtime Secret.
An incomplete or unversioned database is not adopted. A matching committed
database is observed, not initialized again. The provisioner still marks
interrupted operations explicitly and never resumes them automatically.
Operator cleanup must follow actual Radius/Kubernetes ownership; deleting a
workstation file cannot authorize a retry.

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
For this Central US allocation, accept Azure's canonical `centralus` and
verified display name `Central US`: Managed Redis returns the latter while
network resources return the former. Other regions still fail ownership checks.

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

The driver calls the parent-owned `scripts/operations/run-certificate-job.py` coordinator.
Only that wrapper creates issuance Jobs; the provider has no duplicate Job
implementation. The in-cluster `scripts/operations/issue-certificate.py` is never run
directly on the laptop.

The optional `certificateCommand` configuration is an argument-vector array.
Omit it or use `[]` for portable defaults:

* Inside `/app`: `["python", "/app/scripts/operations/run-certificate-job.py"]`.
* On the operator machine: the current Python executable and the absolute
  project path to `scripts/operations/run-certificate-job.py`.

API-discovered runtime configuration uses the portable in-container default.
The public identity ConfigMap contains no executable paths.

An explicit override is trusted operator configuration, never a shell fragment
or tenant input. Avoid putting a workstation-only executable path into the
runtime. Existing image evidence applies to its original source revision,
not to a newly rebuilt image. The driver appends:

```text
--slot SLOT --context STEM-SLOT --namespace APPLICATION_NAMESPACE
--kubeconfig WORKSPACE/SLOT.kubeconfig --domain ACTUAL_PROVIDER_HOST
--config WORKSPACE/provisioning.json
```

For a selected identity, the Job runs in `STEM-system` as `certificate-issuer`, matching the
operator-prebound federated subject. The wrapper creates that namespace and
service account, annotates the account with the allocation's issuer client ID
and tenant, and labels the Job pod `azure.workload.identity/use=true`.
An application-namespace RoleBinding grants this **system-namespace account**
only `get` and `patch` on the single `acme-challenges` ConfigMap. It has no
ConfigMap create/list permission, no Secret permission, and no Azure role grants.

Its command is:

```text
python /app/scripts/operations/issue-certificate.py
  --slot SLOT --domain ACTUAL_PROVIDER_HOST --namespace APPLICATION_NAMESPACE
  --vault-name ALLOCATED_VAULT --certificate-name gateway-STEM-SLOT
  --account-secret acme-STEM-SLOT --project-name PROJECT --resource-prefix STEM
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

This is the existing `scripts/harness/api.py` contract. Key files are separate and
protected. `SLOT-endpoint.json` retains the non-secret HTTPS URL/certificate URI.
The CLI prints only slot/URL JSON. `SLOT-certificate.json` is written immediately
after issuance, before the HTTPS deployment, so a subsequent administrative
reapply cannot downgrade an already-issued gateway after a failed health check.
The provider's child endpoint/key files live on the management PVC, but operator
access does not require copying them. `scripts/harness/export-state.py` independently
discovers actual gateway/PIP DNS, cluster and namespace UIDs, and workload names,
and reads only each named API runtime Secret's `DEMO_KEY` into protected operator
state. It never reads `credentials.json`.

Run `uv run python scripts/harness/export-state.py --watch --timeout 7200` during
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
uv run ruff check src/plane_demo/management scripts/operations/deploy-plane.py \
  scripts/operations/register-radius.py tests/unit/test_provisioner.py
mkdir -p .state/check/tmp
TMPDIR="$PWD/.state/check/tmp" uv run pytest tests/unit/test_provisioner.py
```
