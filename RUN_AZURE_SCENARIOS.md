# Run Azure scenarios

Run each block from a Bash terminal at the repository root. This walkthrough
creates an Azure deployment, onboards shared and isolated tenants, changes
configuration, and tests what happens when a parent database becomes unreachable.
Stop at each checkpoint before continuing.

A fresh live end-to-end verification run of the current implementation remains
outstanding. The checkpoints describe expected results to verify.

Run this guide from the repository root. The order is:

1. [Prepare the workspace](#1-prepare-the-workspace).
2. [Prepare the default Azure environment](#2-prepare-the-default-azure-environment).
3. [Run the manual scenarios](#3-run-the-manual-scenarios).
4. [Clean up Azure](#4-clean-up-azure).

Do not run the [automated alternative](#automated-checks) alongside these manual
requests; it uses the same tenant names. The
[local guide](RUN_LOCAL_SCENARIOS.md) runs the equivalent demo on Docker Desktop.

## What the three planes do

```text
Tenant request -> management API -> management PostgreSQL
                                         ^
                                         | control reconciler pulls and reports
Configuration -> control API    -> control PostgreSQL
                                         ^
                                         | data reconciler pulls and reports
Application   -> data API       -> local ConfigMap + Redis
```

The operator prepares environments before accepting tenants. Management Radius
creates child clusters; each child's Radius deploys its applications and
dependencies. A Recipe is the provider-specific template Radius executes.
There is no permanent Azure infrastructure provisioner in the tenant request path.

Each plane has its own cluster. Shared tenants reuse a control/data pair;
an isolated tenant receives an unused pair prepared explicitly by the operator.
Reconciler processes poll their parent
databases and report local progress. An API request does not push configuration
through all three planes.

### Azure resource groups

Default bootstrap creates four groups with the prefix `rg-<project>-<deployment>-azure-`:

| Suffix | Resources |
|---|---|
| `platform` | Shared networking, registry, private DNS and default Key Vault |
| `management` | Management AKS, managed identities, PostgreSQL and Application Gateway |
| `shared-control` | Shared control AKS, identities, PostgreSQL and Application Gateway |
| `shared-data` | Shared data AKS, identities, Redis, private endpoint and Application Gateway |

AKS adds a separate `*-nodes` group per cluster, giving seven groups for the
three-cluster default. An explicit isolated addition creates two plane groups
and their two node groups. No isolated group, cluster, database, identity or
subnet is created by default.

Management Radius still owns child clusters. Each child's Radius owns its
applications. Management/control application roles permit PostgreSQL and gateway
operations; data application roles permit Redis, gateway, private-endpoint and
NIC operations. These roles cannot manage AKS, replace identities, grant roles
or delete groups. Identity attachment and federation retain separate,
resource-scoped grants.

| Check | Meaning |
|---|---|
| Management `onboarding_status: ready` | Control created the tenant record |
| Environment `stage: available` | Operator setup completed for that pair |
| Management `provisioning_status: succeeded` | The tenant was assigned prepared capacity; no infrastructure work was queued |
| Control `data_config.status: applied` | Data applied the requested ConfigMap version |
| Data returns message, version, and counter | The request used local configuration and Redis |
| `/healthz` returns 200 | The API process is alive; this is not dependency readiness |

## 1. Prepare the workspace

Use a clean checkout of committed source. Keep that source revision unchanged
through build, deployment, and the scenarios.

Use Bash for the commands below. Have Git, `uv`, `jq`, `curl`, ShellCheck,
and the following tools installed:

| Tool | Version |
|---|---|
| Project Python | 3.13, through `uv` |
| Radius | 0.60.2 |
| Radius Bicep | 0.42.1, at `$HOME/.rad/bin/bicep`, not `az bicep` |
| kubectl | 1.35.7 |
| Terraform for offline checks | 1.14-1.15; CI/runtime use 1.15.8 |

Azure also needs `az`, `helm`, and `kubelogin`. Sign in with an interactive
Azure user account, not a service principal. The required roles are listed below.
The workstation must reach the AKS APIs. Azure builds and image verification
run in ACR Tasks; Docker Desktop is not required for this Azure workflow.

```bash
uv sync --locked
git status --short
make check-bicep
```

`git status` should be clean. `make check-bicep` compiles infrastructure and
generates type extensions; it does not deploy resources. `make check` runs the
full source checks if you want that checkpoint before deploying.

Make workflows separate sections with headings and rules, and align entities
with their messages. Progress has no bracketed label or icon. Styled terminals
show green check marks for completed operations; plain output uses `OK`.
Warnings and errors remain explicit. Progress reports phase starts, Job state
changes, and completion without elapsed-time counters or durations.
`COLOR=always` forces styling; `COLOR=never` or nonempty `NO_COLOR`
disables it. Redirected output is plain by default. Status goes to stderr;
JSON/API stdout, native diagnostics, and complete build/push logs are preserved.

### Select operator configuration

Use the same signed-in account for bootstrap and the rest of this walkthrough.
Your account needs one of these options:

| Roles required before bootstrap | Scope |
|---|---|
| **Owner** | Selected Azure subscription |
| **Contributor** plus **User Access Administrator** | Selected Azure subscription |

Contributor alone, or Contributor plus Role Based Access Control Administrator,
cannot create the demo's custom role definitions.

Bootstrap automatically grants that account these additional roles:

| Roles granted by bootstrap | Scope |
|---|---|
| Azure Kubernetes Service Cluster User Role and Azure Kubernetes Service RBAC Cluster Admin | Demo clusters |
| Container Registry Repository Writer and Container Registry Data Importer and Data Reader | Demo registry |

Keep the repository-writer condition that bootstrap applies; do not add an
unrestricted writer grant. If your subscription roles require PIM activation,
activate them before running the demo. Role assignment conditions must permit
the demo's grants, even when your role is Owner.

The grant check also needs Microsoft Graph **Application.Read.All** or an
equivalent delegated permission to read the Radius identities' group memberships.
Azure Owner does not grant this Graph permission. If your tenant blocks the
read, ask its administrator to review access; the demo will not bypass the check.

This is the simple administrative setup for the full demo, not a least-privilege
production configuration. Use a dedicated demo subscription or temporary access.

### Select deployment identity

Use a **new deployment name** for this workflow. Existing on-demand deployments,
including prior `plane-v2` foundations, and `*-cluster` / `*-app` deployments are
not migrated. Keep their matching checkout and private `.env`
for operation and cleanup. The new command path rejects old or mixed layouts.

Choose the subscription and a short project/deployment name:

```bash
make init ENV=azure
make show-config
```

> **Notice:** Azure can retain old subscription deployment records after their
> resource groups are deleted. If bootstrap reports `Old or incomplete resource-group layout`,
> use a fresh `--deployment` name, such as `learning2`, in the explicit initialization
> command below. Do not bypass the layout guard.

Initialization writes or replaces the private, git-ignored `.env`. The default
uses the active Azure subscription, project `radplanes`, deployment `learning`,
and location `centralus`. To supply the selection explicitly:

```bash
make init ENV=azure \
  ARGS='--subscription <subscription-uuid> --location centralus --project demo --deployment team'
```

Check `make show-config` before creating resources. It treats `.env` as data and
redacts keys. To supply a demo key, add `--demo-key-from-env SLOT=VARIABLE` to
the initialization arguments, with the value already set privately in that
environment variable. Never put the key itself in `ARGS` or shell history.

Initialization replaces the file; it does not append or merge previous settings.
Identical inputs produce one assignment per key. Switching environments removes
old Azure settings and keys. Invalid inputs or repeated credential slots leave
the previous private file unchanged.

By default, bootstrap creates the deployment's shared Key Vault. To use an
existing vault, add `--key-vault NAME` during initialization. It must belong to
the selected subscription/tenant, sit outside this deployment's resource groups,
and already use RBAC, private access, soft delete and purge protection. Its
firewall must deny public access while allowing `AzureServices` for Application
Gateway certificates. Bootstrap checks compatibility without changing the vault.

Later commands read `.env`; passing a different `ENV` does not retarget them.
Stable application credentials belong in Key Vault. Each process receives only
its own runtime settings. Access and endpoints come from current APIs, and
temporary CLI files are discarded. PostgreSQL owns tenant and operation records;
Radius and Kubernetes own infrastructure and fault progress.

## 2. Prepare the default Azure environment

One bootstrap command drives the default foundation, verified artifact build,
management deployment and shared environment preparation. It discovers the
inputs; you do not assemble an inventory by hand.

### Create the foundation

Bootstrap first creates management AKS, networking, registry, vault integration
and scoped identities. It builds and inspects artifacts, then installs management
Radius and deploys management and the shared control/data pair,
including Radius, PostgreSQL, Redis, gateways, certificates and workloads.

```bash
make bootstrap CONFIRM_AZURE=yes
```

Before resource creation, bootstrap checks and registers the providers used by
the foundation and later child Recipes: Network, Compute, Storage, ContainerService,
ManagedIdentity, ContainerRegistry, KeyVault, DBforPostgreSQL, and Cache. These are
subscription-wide registrations and remain after cleanup. The operator needs the
providers' `/register/action` permissions; Contributor and Owner include them.
The command never grants itself permissions or changes the default subscription.
The warning `Registering is still on-going` is expected while Azure registers a
provider asynchronously. A successful registration command can return no output;
bootstrap checks its exit code, then queries provider state separately.
After registration starts, ARM validation checks the selected deployment before
creation; a provider need not finish registering in every unrelated region first.

NAT and Application Gateway use Azure-assigned Standard Static addresses, not
customer-owned IP ranges. However, inherited Azure Policies can append
`FirstPartyUsage` IP tags that require
`Microsoft.Network/AllowBringYourOwnPublicIpAddress`. Every confirmed Azure
bootstrap checks this feature and automatically requests registration when it is
unregistered. It waits for `Registered`, then refreshes `Microsoft.Network` before
ARM validation or resource creation. Feature registration is subscription-wide
and remains after cleanup; bootstrap does not modify or exempt Azure Policies.

Denied registration, an unexpected response, or a registration timeout stops
bootstrap without claiming success. A `Pending` feature needs Azure service
approval, not repeated deployment attempts. If deployment still reports a BYOIP
requirement, inspect the failed public IP's Activity Log for policy append events
and ask the policy owner to review the required allocation. Bootstrap prints the
selected deployment name and a scoped inspection command when creation fails.

#### Choose resource sizes and tiers

Before creating resources, bootstrap checks regional service support and asks
for explicit sizing choices. It saves the completed resource-sizing selection
in the private `.env`, preserving credentials. Cancelling this menu leaves the
previous file unchanged. AKS VM and PostgreSQL compute selection follow.

| Resource | Supported demo choices | Saved setting |
|---|---|---|
| AKS nodes per cluster | 2, 3, 4 | `AZURE_NODE_COUNT` |
| AKS control-plane tier | Free, Standard | `AZURE_AKS_TIER` |
| AKS managed OS disk | 64, 128, 256 GiB | `AZURE_NODE_OS_DISK_GB` |
| PostgreSQL storage | Advertised managed-disk sizes from 32, 64, 128, 256 GiB | `AZURE_POSTGRES_STORAGE_GB` |
| Managed Redis | Published Balanced B0, B1, B3, B5, B10, B20 offers | `AZURE_REDIS_SKU` |
| Application Gateway instances per plane | 1, 2, 3 | `AZURE_GATEWAY_CAPACITY` |
| Container Registry tier | Basic, Standard, Premium | `AZURE_REGISTRY_SKU` |
| Key Vault tier | standard, premium; an external vault retains its actual tier | `AZURE_KEY_VAULT_SKU` |

The menus cover demo-compatible choices, not every Azure service configuration.
AKS, gateways, registry and vault use provider metadata for regional service
support. PostgreSQL storage uses the subscription's service capabilities. Redis
uses Azure's public regional retail offers and shows indicative unit-hour prices.
These are **not live Redis capacity or subscription quota checks**. Azure's Redis
scaling-SKU API requires an existing cache, so it is not used for pre-creation
discovery. A published offer can still fail with `InsufficientCapacity`.

Redis remains non-clustered, with TLS enabled and HA disabled. Its offered
Balanced sizes range from 0.5 GB to 24 GB, within the demo's non-clustered limit.
Application Gateway remains Standard_v2 without WAF. Public IPs, NAT and load
balancers remain Standard. DNS, identities, role assignments and private
endpoints have no compute size to select.

Management and child Recipes receive the chosen sizes explicitly. Named
isolated additions inherit the base choices and recheck advertised support.
`make show-config` displays the saved choices. Repeating `make init` resets them.
Bootstrap never substitutes a size or region, resizes existing resources, or
replays a failed administrative Job automatically. Foundations without the
complete sizing profile require their matching checkout or a fresh deployment
name; changing `.env` is not a recovery procedure for a failed deployment.

#### Choose node capacity

Before creating the foundation, bootstrap reads current VM availability,
capabilities, and vCPU quotas for the selected subscription and region. On the
first run, choose one of up to three eligible sizes by entering its number.
No background timer prints into the selection prompt.
Enter `q` to cancel without deploying the foundation. The menu shows vCPUs,
memory, and estimated Linux retail compute costs per VM and for the full demo.
Disks and other Azure services are extra. Choices are ranked by available prices;
if pricing cannot be read, the menu reports that and ranks by CPU and memory.

The selector offers x64 D/E sizes with 4-16 vCPUs and 16-64 GiB RAM, compatible
with Generation 2 Azure Linux images and managed OS disks. The node pools remain
non-zonal, so zone-only SKU restrictions do not exclude a regional choice.
Initial quota must cover three clusters and one additional upgrade node per cluster.
At two four-core nodes per cluster, this means 24 vCPUs plus 12 for upgrades.
Existing owned running nodes are not counted twice; other subscription usage
still reduces available quota. Missing capacity information or no eligible
choice stops bootstrap before foundation creation.

Bootstrap saves only the chosen size as `AZURE_NODE_VM_SIZE` in the private
`.env`, preserving the other settings and credentials. Later runs recheck and
reuse it while it remains eligible. Management and child clusters use the same
selection. Rerunning `make init` resets this choice with the rest of the starting
configuration. Bootstrap does not automatically resize an existing foundation.
An eligible SKU is not a reservation or a guarantee of allocation.

#### Choose PostgreSQL compute

Bootstrap also reads PostgreSQL Flexible Server capabilities for the selected
subscription and region, then asks you to choose one of up to three eligible
SKUs. This is a separate check from AKS VM availability. It runs before ARM
validation and foundation creation, not during tenant onboarding.

The menu preserves the demo's General Purpose tier and PostgreSQL 16. It offers
2-8 vCores and 8-64 GiB RAM, shows advertised zones, and ranks choices by vCores,
RAM and name, not price. Management and prepared control databases use the same
selection. Each database uses the selected initial storage size; database compute and storage
costs are separate from the AKS estimates.

Choose a number, or enter `q` to cancel before creating the foundation.
Bootstrap saves `AZURE_POSTGRES_SKU` and
`AZURE_POSTGRES_TIER` together in the private `.env`, preserving credentials and
the AKS choice. Later runs recheck and reuse an eligible saved choice. Invalid,
restricted or missing capability data stops bootstrap rather than selecting a
default. `make show-config` displays both compute selections.

The foundation records the PostgreSQL choice, and management and child Radius
Recipes receive it explicitly. There is no hardcoded database SKU fallback.
Bootstrap refuses to change an existing foundation's selection or resize a
database. Existing foundations without this selection need a **fresh deployment
name** for the new workflow; they are not automatically adopted or migrated.
Repeating `make init` resets both compute choices with the other starting settings.

The capability catalog does not reserve hardware or guarantee quota. Azure can
still return `SkuNotAvailable` when it allocates a server later. If that happens,
stop and read the failed operation's logs. Do not submit another tenant or reset
database state to force a retry. Review another advertised SKU for a fresh run,
or ask Azure support to confirm capacity.

Checkpoint: bootstrap returns `environment_prepared` for `shared`. Management,
shared control and shared data are deployed. No tenants or isolated resources
have been created.

Bootstrap and subsequent build/deployment commands check the selected Radius
identities' grants, including inherited and group-based assignments. Missing
reads, unexpected grants or changed custom roles fail explicitly. These are
point-in-time grant checks, not a substitute for the manual scenarios.

The consolidated layout still needs a fresh manual end-to-end run. In particular,
verify that the Redis NIC metadata Job completes under the data Radius identity,
preserves existing tags and changes no network settings. It uses the pinned
network-interface tag PATCH API, not generic group-wide tag permission. Azure
NIC write permission is not tag-only permission, and this API does not document
an atomic merge guarantee; do not run concurrent NIC tag writers during the demo.
If it fails, inspect the error rather than granting Contributor or broad tag
access.

### Build and inspect artifacts

Bootstrap builds Recipes and API/private-operator images. The private image
retains the `plane-provisioner` repository name but runs only administrative Jobs
on Azure. A separate Linux ACR
task verifies each candidate's contents using pinned Docker/Python tooling and
trusted inspection code, not code from the candidate image. It creates and exports
a stopped, network-isolated container; the candidate is never started.
Your workstation uploads the small verification context and retrieves a small
verification report, rather than downloading the application images.

```bash
make inspect-build
```

`make build CONFIRM_AZURE=yes` remains available for a separate artifact
checkpoint, but is not an additional required step after successful bootstrap.

Require successful inspection before deployment. The API image excludes
provider tools and deployment credentials. The separate provisioner image has
those administrative tools. Recipe publication checks registry permissions;
do not overwrite a tag or bypass an inspection failure to continue.

Build stores the ACR refresh token in a private, temporary Docker-format
configuration using its `identitytoken` field. Radius uses this configuration
to exchange the refresh token for repository-scoped access tokens.
Build does not write registry credentials to the workstation's credential
helper or permanent Docker configuration. Temporary credentials are removed
when the command ends. Remote verification uses the ACR task's caller-scoped
registry access; workstation credentials are not uploaded in its context.

Verification reports are bound to the candidate digest, selected source, verifier
code, and authenticated ACR run. ARM-owned verification receipts allow reuse on
retry. `make inspect-build` revalidates that recorded evidence without submitting
a task; missing or stale evidence requires a confirmed `make build`. A failed or
unidentifiable verification is not silently resubmitted or treated as success.
Only a verified report permits image promotion and final build-provenance recording.
Existing build proofs remain bound to the same immutable image digest, ACR build
run, and source fingerprint. Remote reports retain their own filesystem
measurements; raw Docker-export metadata hashes are not compared across Docker
Desktop and the ACR build agent.

`image_contains_operator_state` reports an escaped path inside the candidate,
never its contents. In particular, tool-version checks must not leave Azure CLI
profiles in the image. The provisioner Dockerfile uses a disposable
`AZURE_CONFIG_DIR` for its version probe. Existing images keep their original
contents and must be rebuilt from a new source revision to incorporate that fix.

Image builds run in the foreground with their complete native build/push logs.
After completion, the command matches its unique staging-image tag against
authenticated ACR run history and reads that exact run again before inspection.
It does not trust the latest run or a mutable tag's current digest as build
evidence.

Run one build command per deployment at a time. Before submission, build writes
a source-bound pending receipt in the registry's ARM tags. Retrying the same
revision reuses that receipt and any matching completed run, then resumes image
inspection, promotion, locking, and the final provenance record. It does not
queue another build merely because a local command stopped. An ambiguous or
still-unidentifiable submission stops with its receipt intact rather than
guessing or resubmitting. Pending receipts are not completed build proofs.

For an older run without a receipt, explicitly select recovery:

```bash
make build CONFIRM_AZURE=yes ARGS='--recover-build api=RUN_ID'
```

Set `DEMO_REVISION` in `.env` to that run's source commit first if it differs from
`HEAD`. Recovery checks the run's revision-tagged output, creation time, platform,
and recorded Dockerfile instructions against the selected committed source.
The complete image inspection still runs before promotion or provenance
recording. Unsupported or mismatching logs, source, or image contents stop
recovery. Existing verified proofs and different canonical image digests are
never replaced. This explicit legacy recovery path is limited to API builds;
new API and provisioner builds both use resumable receipts. Recovery cannot be
combined with `--inspect` or `--recipes-only`.

### Inspect setup completion

Bootstrap starts management PostgreSQL and its API through `deploy-management`,
then prepares the shared pair through `prepare-shared`. There is no running
Azure provisioner Deployment.

```bash
make kube ARGS='management get job/deploy-management'
make kube ARGS='management logs job/deploy-management --all-containers=true'
make kube ARGS='management get job/prepare-shared'
make kube ARGS='management logs job/prepare-shared --all-containers=true'
make report
```

The command waits for the Job and workloads. Require `Complete=True`;
`Failed=True` is a deployment failure, not permission to resubmit blindly.
The Job owns its temporary inputs; Key Vault and PostgreSQL own credentials
and initialization progress.

Require three endpoints and `shared.stage: available` in the report before
onboarding. Repeating bootstrap observes completed phases without recreating
the foundation or administrative Jobs. Failed or interrupted setup retains its
Lease, attempt record and diagnostics for explicit recovery; do not delete those
records or reset tenant rows to force replay.

## 3. Run the manual scenarios

### Inspect management

The Make commands read `.env` and discover current access. They manage their own
shells, temporary kubeconfigs and credential permissions:

```bash
make report
make kube ARGS='management get pods,pvc'
make kube ARGS='management logs job/prepare-shared --tail=20'
make api ARGS='management GET /healthz'
```

Initially the report contains three prepared endpoints and no tenants.
Require shared environment availability and HTTP 200 before onboarding. There are no shell
functions to define, detached worktrees to create, or provisioning files to
assemble. `make fault-status ARGS='SLOT COMPONENT'` reads a fault's Kubernetes
journal; the running fault helper performs the network checks.

### Prepare direct API requests

Read the management URL and demo key into shell variables. The key is captured
without printing the Secret or putting its value in shell history:

```bash
set -o pipefail
MANAGEMENT_URL=$(make endpoints ARGS=management | jq -er '.url') || exit 1
MANAGEMENT_KEY=$(make kube ARGS='management get secret management-api-runtime -o json' | \
  jq -er '.data.DEMO_KEY | @base64d') || exit 1
```

The first admission uses two direct `curl` requests. Other examples use
`make api` to discover the selected plane's current endpoint and key per call.
For that helper, pass the target, method and path through `ARGS`, and pipe JSON
request bodies into it so message text is not interpreted by Make. Keep the
inner quotes shown around query URLs and variable arguments.

Successful reads return HTTP 200; tenant acceptance returns 202. The helper
prints status to stderr and JSON to stdout. Expected 401/404/409/422/503 checks
return nonzero, so run the blocks individually rather than as one unattended
script. Compare the displayed responses at each checkpoint; two matching error
responses do not prove unchanged application state.

For a new terminal, return to this checkout, run `make show-config`, and repeat
the setup above before using `curl`.
Resume with checkpoint reads, not deployment commands or tenant POSTs.
Restore any active fault or paused workload before taking a break.

### A. Provision the first shared tenant

You follow one request from acceptance into the prepared shared environment
and a working data response.

```bash
make api ARGS='management GET /healthz'
make api ARGS='management GET /tenants/shared-a'
```

Expect 200, then 404. Send the POST once. Copy the returned `operation_id` into
`<operation-id>` in the second request:

```bash
curl -i -sS --fail-with-body "$MANAGEMENT_URL/tenants" \
  -H "X-Demo-Key: $MANAGEMENT_KEY" \
  -H 'Content-Type: application/json' \
  --data '{"tenant_id":"shared-a","isolation":"shared","initial_message":"alpha"}'

curl -i -sS --fail-with-body "$MANAGEMENT_URL/operations/<operation-id>" \
  -H "X-Demo-Key: $MANAGEMENT_KEY"
```

Expect HTTP 202 with an operation ID from the POST, then HTTP 200 with the
operation's status from the GET. Its infrastructure status should already be
`succeeded`, with stage `available`. Control-record readiness follows asynchronously.

#### Optional admission checks

Duplicate requests must return 409 without creating another operation:

```bash
printf '%s\n' '{"tenant_id":"shared-a","isolation":"shared","initial_message":"alpha"}' | \
  make api ARGS='management POST /tenants'
```

Before preparing any isolated environment, verify that isolated admission does
not start infrastructure work:

```bash
printf '%s\n' '{"tenant_id":"capacity-check","isolation":"isolated","initial_message":"not accepted"}' | \
  make api ARGS='management POST /tenants'
make api ARGS='management GET /tenants/capacity-check'
```

Expect 503 `allocation_unavailable`, then 404. No tenant, operation, cluster or
database should be created. Skip this check if unused isolated capacity already
exists, because that request would then be accepted.

#### Follow provisioning

Repeat these reads until control reports the tenant record:

```bash
make api ARGS='management GET /tenants/shared-a' | jq '{pair_id, provisioning_status, provisioning_stage, onboarding_status, control_record}'
make kube ARGS='shared-control logs deployment/control-reconciler --tail=20'
```

No infrastructure should change during admission. Require both
`provisioning_status: succeeded` and `onboarding_status: ready`.
Stop on `failed` or `interrupted`; do not reset state to force a retry.

HTTP 200 and `api completed` mean the status request succeeded, not that
the tenant is ready. Infrastructure command failures now belong to setup Jobs,
not tenant operations. Read their logs before accepting tenants; never reset
database status values to bypass a failed setup.

```bash
make report
make api ARGS='control:shared GET /tenants/shared-a'
make api ARGS='data:shared GET /tenants/shared-a'
make kube ARGS='shared-data get configmap tenant-shared-a -o json' | jq .data
```

Expect three discovered endpoints. Control should become `applied`; data should
return `alpha`, version 1, and counter 0. Match the `onboarding_id` across
the three APIs.

Read the logical placement and cluster UIDs for the reuse check in section B.
Management does not store or return endpoint URLs; endpoint and cluster
discovery use provider APIs.

```bash
make api ARGS='management GET /tenants/shared-a' | jq '{tenant_id, pair_id}'
for slot in shared-control shared-data; do
  printf '%s ' "$slot"
  make kube ARGS="'$slot' get namespace kube-system -o json" | jq -er .metadata.uid
done
```

### B. Reuse the pair while data reconciliation is paused

This separates management readiness from data configuration application while
checking that the same cluster pair is reused.

Pause only the shared data reconciler:

```bash
make kube ARGS='shared-data scale deployment/data-reconciler --current-replicas=1 --replicas=0'
make kube ARGS='shared-data get pods -l plane-demo/component=data-reconciler'
```

Wait until no matching Pods remain, including terminating Pods. Then:

```bash
printf '%s\n' '{"tenant_id":"shared-b","isolation":"shared","initial_message":"bravo"}' | \
  make api ARGS='management POST /tenants'
make api ARGS='management GET /tenants/shared-b'
make api ARGS='control:shared GET /tenants/shared-b'
make api ARGS='data:shared GET /tenants/shared-b'
```

Wait for management readiness and provisioning success. Control should have
`bravo` but report data as `pending`, with no applied version. Data should return
404. This shows that management readiness does not wait for data application.

Restore the reconciler before leaving this section, even after an error:

```bash
make kube ARGS='shared-data scale deployment/data-reconciler --current-replicas=0 --replicas=1'
make kube ARGS='shared-data rollout status deployment/data-reconciler --timeout=60s'
make api ARGS='control:shared GET /tenants/shared-b'
make api ARGS='data:shared GET /tenants/shared-b'
```

Wait for `applied` and the `bravo` response, then compare placement:

```bash
make api ARGS='management GET /tenants/shared-a' | jq '{tenant_id, pair_id}'
make api ARGS='management GET /tenants/shared-b' | jq '{tenant_id, pair_id}'
make report | jq '.endpoints | keys'
for slot in shared-control shared-data; do
  printf '%s ' "$slot"
  make kube ARGS="'$slot' get namespace kube-system -o json" | jq -er .metadata.uid
done
```

Require matching `pair_id` values, unchanged cluster UIDs from section A, and
still three slots. The new shared tenant added records, not another cluster pair.

### C. Provision an isolated tenant

This section is optional. Skip isolated commands in later sections if you want
only the shared demo. Before accepting an isolated tenant, add its environment
using the same bootstrap workflow. This example names the environment `1` to
retain the `isolated-1` API targets used below; descriptive names such as `blue`
are also supported.

```bash
make bootstrap CONFIRM_AZURE=yes ARGS='--isolated 1'
make kube ARGS='management get job/prepare-isolated-1'
make kube ARGS='management logs job/prepare-isolated-1 --tail=40'
make report
```

Require `isolated-1.stage: available`. The command creates only that pair's
foundation, clusters and dependencies; it does not redeploy management or the
shared applications. New environments inherit the base compute selections and
recheck node capacity. Names use 1-12 lowercase letters/digits with internal
hyphens; the address layout supports six isolated pairs. Allocation indices and
retired names are not reused.

The tenant receives an available unused isolated pair. The two pairs must not
serve each other's tenant records.

```bash
printf '%s\n' '{"tenant_id":"isolated-c","isolation":"isolated","initial_message":"charlie"}' | \
  make api ARGS='management POST /tenants'
make api ARGS='management GET /tenants/isolated-c'
make kube ARGS='isolated-1-control logs deployment/control-reconciler --tail=20'
```

Wait for provisioning success and management readiness:

```bash
make report
make api ARGS='control:isolated-1 GET /tenants/isolated-c'
make api ARGS='data:isolated-1 GET /tenants/isolated-c'
for slot in management shared-control shared-data isolated-1-control isolated-1-data; do
  printf '%s ' "$slot"
  make kube ARGS="'$slot' get namespace kube-system -o json" | jq -er .metadata.uid
done
```

Expect five slots with five distinct cluster UIDs. The isolated tenant has
different control/data endpoints and its own PostgreSQL/Redis instances.
Wait for `charlie`, version 1, on data.

Check that the wrong pair does not host the records:

```bash
make api ARGS='control:shared GET /tenants/isolated-c'
make api ARGS='data:shared GET /tenants/isolated-c'
make api ARGS='control:isolated-1 GET /tenants/shared-a'
make api ARGS='data:isolated-1 GET /tenants/shared-a'
```

Expect 404 for each. These use valid plane keys and demonstrate placement,
not production tenant authentication.

### D. Update configuration and compare counters

Change tenant configuration through control, then check that data applies it
without resetting counters.

```bash
make api ARGS='data:shared GET /tenants/shared-a'
make api ARGS='data:shared GET /tenants/shared-b'
make api ARGS='data:isolated-1 GET /tenants/isolated-c'
make api ARGS='data:shared POST /tenants/shared-a/counter'
make api ARGS='data:shared POST /tenants/shared-a/counter'
```

Each POST must increment only `shared-a` by one. GET must not increment.
Read the other two tenants again; their counters should be unchanged.
Use the values you observed, rather than assuming zero after repeated commands.

Update each tenant through control:

```bash
printf '%s\n' '{"message":"alpha-v2"}' | \
  make api ARGS='control:shared PUT /tenants/shared-a/configuration'
printf '%s\n' '{"message":"bravo-v2"}' | \
  make api ARGS='control:shared PUT /tenants/shared-b/configuration'
printf '%s\n' '{"message":"charlie-v2"}' | \
  make api ARGS='control:isolated-1 PUT /tenants/isolated-c/configuration'
make api ARGS='control:shared GET /tenants/shared-a'
make api ARGS='data:shared GET /tenants/shared-a'
make api ARGS='data:shared GET /tenants/shared-b'
make api ARGS='data:isolated-1 GET /tenants/isolated-c'
make kube ARGS='shared-data get configmap tenant-shared-a -o json' | jq .data
```

Each PUT creates the next desired version. Wait for matching data messages and
versions and control's `applied` reports. Counters must survive the update.
Management supplied the initial message; control owns subsequent changes.

### E. Observe polling, history, and access

Check that repeated polling preserves control's changes, then inspect paginated
events and API access.

Read the responses after updates have applied, then compare them with the
responses after a successful poll:

```bash
POLL_FROM=$(uv run --no-sync python -c \
  'from datetime import UTC, datetime; print(datetime.now(UTC).isoformat())')
make api ARGS='management GET /tenants/shared-a'
make api ARGS='control:shared GET /tenants/shared-a'
make api ARGS='data:shared GET /tenants/shared-a'
sleep 15
make kube ARGS="shared-control logs deployment/control-reconciler '--since-time=$POLL_FROM' --timestamps=true --tail=20"
make api ARGS='management GET /tenants/shared-a'
make api ARGS='control:shared GET /tenants/shared-a'
make api ARGS='data:shared GET /tenants/shared-a'
```

Require a successful `control_poll` after `$POLL_FROM`, with `succeeded` greater
than zero and `failed=0`, plus unchanged tenant responses. That shows control retained
its updates while polling management. No successful poll means no proof yet.

Read a timeline in small pages:

```bash
make api ARGS="control:shared GET '/tenants/shared-a?limit=2'" | jq '{timeline, next_after_event_id}'
```

If `next_after_event_id` is not `null`, replace `<event-id>` with that value:

```bash
make api ARGS="control:shared GET '/tenants/shared-a?limit=2&after_event_id=<event-id>'"
```

Continue with each returned cursor until `null`. IDs increase but need not be
consecutive. Management reports control-record creation; control reports
configuration changes and data application.

Check rejected requests:

```bash
curl -q -sS -o /dev/null -w 'HTTP %{http_code}\n' "$(make endpoints ARGS=management | jq -er '.url')/tenants/shared-a"
curl -q -sS -o /dev/null -w 'HTTP %{http_code}\n' \
  -H 'X-Demo-Key: wrong' "$(make endpoints ARGS=management | jq -er '.url')/tenants/shared-a"
printf '%s\n' '{"tenant_id":"Bad ID","isolation":"shared","initial_message":"invalid"}' | \
  make api ARGS='management POST /tenants'
printf '%s\n' '{"tenant_id":"shared-a","isolation":"shared","initial_message":"must not replace alpha"}' | \
  make api ARGS='management POST /tenants'
```

Expect 401, 401, 422, and 409. The duplicate must not reset control's configuration
or the counter. Repeat the curl checks for the child API URLs listed by:

```bash
make endpoints ARGS=all
```

Inspect the data API identity without displaying Secrets:

```bash
make kube ARGS='shared-data get deployment data-api -o json' | jq -er .spec.template.spec.serviceAccountName
DATA_NS=$(make kube ARGS='shared-data get deployment data-api -o json' | jq -er '.metadata.namespace')
API_ID="system:serviceaccount:$DATA_NS:data-api-runtime"
make kube ARGS="shared-data auth can-i get configmaps '--as=$API_ID'"
make kube ARGS="shared-data auth can-i get secret/data-reconciler-runtime '--as=$API_ID'"
make kube ARGS="shared-data auth can-i list secrets '--as=$API_ID'"
```

Expect `data-api-runtime`, then `yes`, `no`, `no`. A denial exits nonzero.
An operator impersonation error is not a successful denial check.
The full in-Pod/named-permission check is available as
[`DATA_API_PERMISSIONS_PROBE`](scripts/harness/test-e2e.py).

### F. Block the management database link

An existing control/data pair should keep accepting configuration changes and
serving requests while management PostgreSQL is unreachable.

Finish onboarding and inspect all five endpoints before faults. Open a second
terminal in the same checkout. It reads the same `.env`; no export is needed.
Do not stop an API to simulate a database outage.
The Azure fault helper uses Cilium policy to block the actual private parent
database connection.

In the second terminal:

```bash
make fault CONFIRM_AZURE=yes \
  ARGS='--slot shared-control --component control-reconciler --duration 300'
```

This blocks shared control's connection to management PostgreSQL, not the
management API or the isolated pair. The helper verifies the target and both
fresh/existing database connections, holds the fault, and restores it.

In the main terminal:

```bash
FAULT_SLOT=shared-control
FAULT_COMPONENT=control-reconciler
make fault-status ARGS="'$FAULT_SLOT' '$FAULT_COMPONENT'" | jq '{slot, component, outcome, blocked_at, restored}'
```

Require `shared-control`, `control-reconciler`, and `blocked_verified` before
continuing. While the helper holds the fault:

```bash
make api ARGS='management GET /tenants/shared-a'
printf '%s\n' '{"message":"without-management"}' | \
  make api ARGS='control:shared PUT /tenants/shared-a/configuration'
make api ARGS='data:shared GET /tenants/shared-a'
make api ARGS='data:shared POST /tenants/shared-a/counter'
```

The new message should reach data through control's own database. Keep reading
and incrementing for at least 60 seconds, then:

```bash
make api ARGS='management GET /tenants/shared-a'
```

Compare the management response with the one displayed before the control update.
Expect no new management reports during blockage. After the fault terminal finishes:

```bash
make fault-status ARGS="'$FAULT_SLOT' '$FAULT_COMPONENT'" | jq '{outcome, restored, physical_restored, restoration_started_at, restored_at}'
RESTORE_FROM=$(make fault-status ARGS="$FAULT_SLOT $FAULT_COMPONENT" | jq -er '.restoration_started_at')
make kube ARGS="shared-control logs deployment/control-reconciler '--since-time=$RESTORE_FROM' --timestamps=true --tail=20"
```

Require `verified_and_restored`, both restoration flags true, and a successful
control poll after restoration. Recovery must not add a duplicate
`control_record_created` event.

### G. Block control, queue updates, and restart data

Data should keep serving its applied configuration through the outage and an
API restart, then apply the newest control version after reconnection.

Start only after the preceding fault is restored. Read the current data response
and identify its applied message, version, and counter:

```bash
make api ARGS='data:shared GET /tenants/shared-a'
```

In the fault terminal:

```bash
make fault CONFIRM_AZURE=yes \
  ARGS='--slot shared-data --component data-reconciler --duration 300'
```

In the main terminal, select the owning journal:

```bash
FAULT_SLOT=shared-data
FAULT_COMPONENT=data-reconciler
make fault-status ARGS="'$FAULT_SLOT' '$FAULT_COMPONENT'" | jq '{slot, component, outcome, blocked_at, restored}'
```

Require `shared-data`, `data-reconciler`, and `blocked_verified`. Now data cannot
read control PostgreSQL.

```bash
printf '%s\n' '{"message":"queued-first"}' | \
  make api ARGS='control:shared PUT /tenants/shared-a/configuration'
printf '%s\n' '{"message":"queued-latest"}' | \
  make api ARGS='control:shared PUT /tenants/shared-a/configuration'
make api ARGS='control:shared GET /tenants/shared-a'
make api ARGS='data:shared GET /tenants/shared-a'
make api ARGS='data:shared POST /tenants/shared-a/counter'
```

Control should report the latest version as `pending`. Data must keep returning
the message and version observed before the outage while its counter still works.

Replace only the data API while the link remains blocked:

```bash
make kube ARGS='shared-data get pods -l plane-demo/component=data-api -o custom-columns=NAME:.metadata.name,UID:.metadata.uid'
make kube ARGS='shared-data rollout restart deployment/data-api'
make kube ARGS='shared-data rollout status deployment/data-api --timeout=30s'
make kube ARGS='shared-data get pods -l plane-demo/component=data-api -o custom-columns=NAME:.metadata.name,UID:.metadata.uid'
make api ARGS='data:shared GET /tenants/shared-a'
```

Require a new Pod UID and the old applied message/version with the current
counter. Keep requests going for at least 60 seconds of verified blockage.
Do not replace the data reconciler during the fault; the helper checks its Pod identity.

After restoration:

```bash
make fault-status ARGS="'$FAULT_SLOT' '$FAULT_COMPONENT'" | jq '{outcome, restored, physical_restored}'
make api ARGS='control:shared GET /tenants/shared-a'
make api ARGS='data:shared GET /tenants/shared-a'
make kube ARGS='shared-data get configmap tenant-shared-a -o json' | jq .data
```

Require `queued-latest` and control `applied`. Both queued versions should have
`configuration_updated` events, but the skipped `queued-first` version must not
have `config_applied`.

The helper allows 60-600 seconds; use `--duration 600` for a longer explanation.
If the timer expires early, confirm restoration and repeat rather than claiming
later observations happened during the outage. The automated recovery target
is 30 seconds from restoration start; manual observation alone is not a benchmark.

#### Interrupted fault

You restore the exact recorded fault without deleting unrelated rules or
replacing the cluster.

Prefer letting the timer finish. Ctrl-C requests restoration; still inspect
the journal. If the process has stopped without confirmed restoration:

```bash
make fault CONFIRM_AZURE=yes \
  ARGS="--slot $FAULT_SLOT --component $FAULT_COMPONENT --restore"
make fault-status ARGS="'$FAULT_SLOT' '$FAULT_COMPONENT'" | jq '{outcome, restored, physical_restored}'
```

Do not use `kill -9`, replace the faulted reconciler, or delete its cluster to
clear a fault. Do not proceed until the original link is confirmed restored.

## 4. Clean up Azure

### Remove only an unused isolated environment

This deletes only the selected isolated pair's applications, clusters, groups
and subnet resources. It refuses an environment assigned to a tenant and never
removes the default management/shared environment. Tenant deletion is not
implemented, so a pair already used by these scenarios requires the normal
whole-demo cleanup instead.

```bash
make environment-clean-plan ARGS='--isolated blue'
make environment-clean CONFIRM_AZURE=yes ARGS='--isolated blue'
```

Review the exact target before execution. Cleanup serializes with setup and
atomically disables admission before deleting anything. Credential/certificate
objects and allocation tombstones are retained; the result is
`isolated_environment_removed`, not a claim that every historical object is gone.

### Remove the whole demo

Cleanup destroys the selected demo's application data. Restore faults and paused
workloads, and finish or inspect active provisioning before proceeding.
Check that `.env` still selects Azure:

```bash
make show-config
make clean-plan
make clean CONFIRM_AZURE=yes
make verify-clean
```

Review the deletion plan before `make clean`. The cleaner quiesces management,
deletes data/control applications through child Radius, deletes the children
through management Radius, then removes management and its foundation. It reads
current Azure, Radius and Kubernetes owners; a healthy management API/database
or saved cleanup report is not required.

Checkpoint: `make verify-clean` returns `status: clean` for
`scope: owned-active-resources`. Review retained objects and soft-deleted vaults
separately. An external vault, its unrelated objects and assignments, and any
roles still needed by them remain. An owned vault's recovery record can remain
until its retention period ends; `purged: false` is not active-deployment residue.

If an owner is unavailable or a Recipe left orphaned resources, stop and inspect
the failure. Do not substitute direct AKS deletion, edit ownership records, or
reset database rows to force cleanup.

### After a failed bootstrap

Failed or canceled ARM deployments may have null or missing outputs. The
consolidated layout requires verified `plane-v2` outputs before retrying bootstrap
or authorizing cleanup. Missing outputs do not mean no resources exist. Retain
those resources and inspect the failed operation rather than adopting them from
names or tags alone.

When complete layout outputs are available, preview owner-ordered cleanup:

```bash
make clean-plan
make clean-azure CONFIRM_AZURE=yes
make verify-clean
```

Orphaned resources, mismatched metadata, and active deployments block deletion.
A non-ready management AKS or unavailable Radius owner is retained for
investigation, not deleted through a fallback. External vaults and their objects
remain protected. Cleanup rechecks the bootstrap record before Azure mutations
and never reports clean until active-resource absence is verified.

### Optional: retain the foundation

To remove applications and child clusters but keep management AKS, registry,
networking and identities, use the normal cleaner's narrower mode:

```bash
uv run --no-sync python scripts/operations/clean-azure.py --radius-only
CONFIRM_AZURE=yes uv run --no-sync python scripts/operations/clean-azure.py --radius-only --execute
```

Its success is `radius_resources_removed`, not a clean whole environment.
Full `make verify-clean` is only appropriate after full cleanup.
Plane groups and their preallocated identities remain after radius-only cleanup.
The cleaner checks application-resource absence separately from those retained
resources; it does not require an empty plane group.

## Troubleshooting

| Where the run stops | What to inspect |
|---|---|
| Bootstrap or access | Check `.env`, Azure login, permissions and AKS reachability. Do not broaden database access or change global contexts. |
| Artifact inspection | Check the source revision and selected registry. Do not overwrite/unlock artifacts to bypass a mismatch. |
| Management deployment | Read `job/deploy-management` status and logs with `make kube`. A failed or interrupted Job is not automatically replayed. |
| Tenant stays pending | Read its operation and control-reconciler logs. Management readiness still requires a control record. |
| Environment preparation fails | Read `job/prepare-<pair>` in management for the failed command's stderr. Preserve its Lease, attempt record, credentials and resources. No tenant operation is created by setup. |
| Redis reports `InsufficientCapacity` | Azure could not allocate the selected `AZURE_REDIS_SKU` in this region. Published offers do not prove live capacity. Inspect the retained cache and failed Job. A new deployment can use a different explicit size or region; changing `.env` does not resize or recover the failed environment. Do not reset or replay the Job blindly. |
| No isolated capacity | Add a named isolated environment with `make bootstrap ... ARGS='--isolated NAME'`. Do not submit tenant requests to create clusters. |
| Control is ready but data is stale | Read the control data report, data-reconciler logs and tenant ConfigMap. Check for a paused reconciler or active fault. |
| SQL observation fails | Preserve credentials and the database. Missing/drifted schema metadata does not authorize reinitialization. |
| Cleanup refuses an owner or journal | Inspect the named resource and restore its fault first. Do not remove guards or force-delete children. |

For any slot, inspect without dumping runtime Secrets:

```bash
make kube ARGS='shared-data get pods'
make kube ARGS='shared-data logs deployment/data-reconciler --tail=40'
make kube ARGS='shared-data get configmap tenant-shared-a -o json'
```

## Automated checks

Use these instead of the manual scenarios on a prepared deployment with no demo
tenants. The harness must use the source revision that built the inspected images.

```bash
make test-e2e CONFIRM_AZURE=yes
make test-outages CONFIRM_AZURE=yes
```

By default, the first command creates the two shared tenants and checks reuse,
isolated-capacity rejection, configuration, counters and access. Use it before
adding an isolated pair. The second checks parent outages and recovery. A single
`all` run is another option:

```bash
uv run --no-sync python scripts/harness/test-e2e.py --environment azure --mode all --execute
```

To include an already prepared isolated pair, pass it explicitly:

```bash
uv run --no-sync python scripts/harness/test-e2e.py --environment azure --mode all \
  --isolated-environment isolated-1 --execute
```

After a manual run, `--mode verify-existing` observes existing tenants and runs
the remaining checks without claiming fresh onboarding proof. It still performs
live mutations. Reports go to stdout and exclude API keys, passwords and DSNs.
The [harness source](scripts/harness/test-e2e.py) contains the individual checks
used by these modes.

If an interrupted first admission provides a continuation handle, only that
bounded continuation is supported. Keep the same source and `.env` selection
and use the exact handle from the run:

```bash
uv run --no-sync python scripts/harness/test-e2e.py --environment azure --mode all \
  --continue-first-from NAME@UID@RUN_ID --execute
```

This is not general replay of failed provisioning. For an interrupted fault,
use the [journal restoration procedure](#interrupted-fault).

## Limits to keep in mind

Use synthetic data. Per-plane demo keys do not provide production tenant
authentication. Administrative setup has no HA scheduler or automatic
replay of interrupted infrastructure work; tenant migration and deletion APIs
are outside the demo.

PostgreSQL and Redis use private Azure connectivity and verified TLS. Certificate
issuance is implemented, but automatic certificate renewal is not. Parent
outages demonstrate configuration independence, not disaster recovery or
reconstruction of lost clusters/databases.
