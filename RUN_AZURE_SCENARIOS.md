# Run Azure scenarios

Run each block from a Bash terminal at the repository root. This walkthrough
creates an Azure deployment, onboards shared and isolated tenants, changes
configuration, and tests what happens when a parent database becomes unreachable.
Stop at each checkpoint before continuing.

A fresh live end-to-end verification run of the current implementation remains
outstanding. The checkpoints describe expected results to verify.

Run this guide from the repository root. The order is:

1. [Prepare the workspace](#1-prepare-the-workspace).
2. [Deploy Azure management](#2-deploy-azure-management).
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

The management API saves requests; a separate provisioner asks management
Radius to create control/data clusters. A Recipe is the provider-specific
template Radius executes. The provisioner installs Radius in each child,
then that child's Radius deploys its applications and dependencies.

Each plane has its own cluster. Shared tenants reuse a control/data pair;
an isolated tenant gets another pair. Reconciler processes poll their parent
databases and report local progress. An API request does not push configuration
through all three planes.

### Azure resource groups

Bootstrap creates six groups with the prefix `rg-<project>-<deployment>-azure-`:

| Suffix | Resources |
|---|---|
| `platform` | Shared networking, registry, private DNS and default Key Vault |
| `management` | Management AKS, managed identities, PostgreSQL and Application Gateway |
| `shared-control`, `isolated-1-control` | Each control instance's AKS, identities, PostgreSQL and Application Gateway |
| `shared-data`, `isolated-1-data` | Each data instance's AKS, identities, Redis, private endpoint and Application Gateway |

AKS adds a separate `*-nodes` group per cluster. The complete demo has eleven
groups, including node groups. The six bootstrap groups reserve permissions
upfront; their existence does not mean all five clusters are running.

Management Radius still owns child clusters. Each child's Radius owns its
applications. Management/control application roles permit PostgreSQL and gateway
operations; data application roles permit Redis, gateway, private-endpoint and
NIC operations. These roles cannot manage AKS, replace identities, grant roles
or delete groups. Identity attachment and federation retain separate,
resource-scoped grants.

| Check | Meaning |
|---|---|
| Management `onboarding_status: ready` | Control created the tenant record |
| Management `provisioning_status: succeeded` | The provisioning operation finished |
| Control `data_config.status: applied` | Data applied the requested ConfigMap version |
| Data returns message, version, and counter | The request used local configuration and Redis |
| `/healthz` returns 200 | The API process is alive; this is not dependency readiness |

## 1. Prepare the workspace

Use a clean checkout of committed source. Keep that source revision unchanged
through build, deployment, and the scenarios.

Use Bash for the commands below. Have Git, `uv`, `jq`, `curl`, Docker Desktop,
ShellCheck, and the following tools installed:

| Tool | Version |
|---|---|
| Project Python | 3.13, through `uv` |
| Radius | 0.60.2 |
| Radius Bicep | 0.42.1, at `$HOME/.rad/bin/bicep`, not `az bicep` |
| kubectl | 1.35.7 |
| Terraform for offline checks | 1.14-1.15; CI/runtime use 1.15.8 |

Azure also needs `az`, `helm`, and `kubelogin`. Sign in with an interactive
Azure user account, not a service principal. The required roles are listed below.
The workstation must reach the AKS APIs. Docker Desktop is used to inspect built
images.

```bash
uv sync --locked
git status --short
make check-bicep
```

`git status` should be clean. `make check-bicep` compiles infrastructure and
generates type extensions; it does not deploy resources. `make check` runs the
full source checks if you want that checkpoint before deploying.

All Make workflows use colored sections and labeled status: blue headings, cyan
progress, green success, yellow warnings, and red errors. Quiet waits print elapsed
time every 15 seconds. `COLOR=always` forces status color; `COLOR=never` or nonempty
`NO_COLOR` disables it. Redirected output is plain by default. Status goes to stderr;
JSON/API stdout and complete native tool diagnostics and build/push logs are preserved.

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

Use a **new deployment name** for this layout. Existing `*-cluster` / `*-app`
deployments are not migrated. Keep their matching checkout and private `.env`
for operation and cleanup. The new command path rejects old or mixed layouts.

Choose the subscription and a short project/deployment name:

```bash
make init ENV=azure
make show-config
```

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

## 2. Deploy Azure management

The order is bootstrap, build, then management deployment. The commands discover
their inputs and inspect artifacts; you do not assemble an inventory by hand.

### Create the foundation

Bootstrap creates management AKS, networking, registry, vault integration and
the preassigned identities, then installs management Radius. It reserves the
child scopes and permissions but does not create tenant clusters or databases.

```bash
make bootstrap CONFIRM_AZURE=yes
```

Before resource creation, bootstrap checks and registers the providers used by
the foundation and later child Recipes: Network, Compute, Storage, ContainerService,
ManagedIdentity, ContainerRegistry, KeyVault, DBforPostgreSQL, and Cache. These are
subscription-wide registrations and remain after cleanup. The operator needs the
providers' `/register/action` permissions; Contributor and Owner include them.
The command never grants itself permissions or changes the default subscription.
After registration starts, ARM validation checks the selected deployment before
creation; a provider need not finish registering in every unrelated region first.

The current templates require no preview feature registrations. In particular,
`Microsoft.Network/AllowBringYourOwnPublicIpAddress` is not an intended dependency:
NAT and Application Gateway use Azure-assigned Standard Static addresses, not
customer-owned IP ranges. If Azure requests that feature, inspect the failed ARM
operation and its request rather than enabling it blindly. Bootstrap prints the
selected deployment name and a scoped inspection command when creation fails.
Denied registration or an unexpected response stops bootstrap without claiming
success. A genuinely required approval-pending feature needs service approval,
not repeated deployment attempts.

Checkpoint: bootstrap completed and management AKS and Radius exist. The
management application and tenant clusters have not been deployed yet.

Bootstrap and subsequent build/deployment commands check the five Radius
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

Build publishes Recipes and the API/provisioner images. Inspection checks image
contents and build provenance, rather than treating a changed tag as proof.

```bash
make build CONFIRM_AZURE=yes
make inspect-build
```

Require successful inspection before deployment. The API image excludes
provider tools and deployment credentials. The separate provisioner image has
those administrative tools. Recipe publication checks registry permissions;
do not overwrite a tag or bypass an inspection failure to continue.

### Deploy management and wait for completion

This starts management PostgreSQL, its API and the provisioner through an
owned deployment Job.

Inspect the discovered inputs and proposed Job without submitting it:

```bash
make deploy-management-preview
```

Then deploy:

```bash
make deploy-management CONFIRM_AZURE=yes
make kube ARGS='management get job/deploy-management'
make kube ARGS='management logs job/deploy-management --all-containers=true'
```

The command waits for the Job and workloads. Require `Complete=True`;
`Failed=True` is a deployment failure, not permission to resubmit blindly.
The Job owns its temporary inputs; Key Vault and PostgreSQL own credentials
and initialization progress.

Checkpoint: management's API, PostgreSQL and provisioner are ready, with no
tenant clusters yet. Radius registration is part of this deployment command.

## 3. Run the manual scenarios

### Inspect management

The Make commands read `.env` and discover current access. They manage their own
shells, temporary kubeconfigs and credential permissions:

```bash
make report
make kube ARGS='management get pods,pvc'
make kube ARGS='management logs deployment/provisioner --tail=20'
make api ARGS='management GET /healthz'
```

Initially the report contains only the management endpoint and no tenants.
Require `provisioner_ready` and HTTP 200 before onboarding. There are no shell
functions to define, detached worktrees to create, or provisioning files to
assemble. `make fault-status ARGS='SLOT COMPONENT'` reads a fault's Kubernetes
journal; the running fault helper performs the network checks.

### Prepare response comparisons

The later examples compare API responses. Create a private temporary directory
for those notes, and enable failure reporting for shell pipelines:

```bash
set -o pipefail
NOTES=$(mktemp -d "${TMPDIR:-/tmp}/plane-manual.XXXXXX")
```

`$NOTES` is only for your comparisons. Deployment, discovery, credentials and
cleanup never read it. You can discard the notes after the demo.

Pass the target, method and path through `ARGS`. Pipe JSON request bodies into
`make api` so message text is not interpreted by Make. Keep the inner quotes
shown around query URLs and variable arguments.

Successful reads return HTTP 200; tenant acceptance returns 202. The helper
prints status to stderr and JSON to stdout. Expected 401/404/409/422/503 checks
return nonzero, so run the blocks individually rather than as one unattended
script. Two matching error responses do not prove unchanged application state.

For a new terminal, return to this checkout and run `make show-config`. Create
a new notes directory if you need comparisons, then capture fresh baselines.
Resume with checkpoint reads, not deployment commands or tenant POSTs.
Restore any active fault or paused workload before taking a break.

### A. Provision the first shared tenant

You follow one request from acceptance to infrastructure creation and a working
data response.

```bash
make api ARGS='management GET /healthz'
make api ARGS='management GET /tenants/shared-a'
```

Expect 200, then 404. Send the request once:

```bash
printf '%s\n' '{"tenant_id":"shared-a","isolation":"shared","initial_message":"alpha"}' | \
  make api ARGS='management POST /tenants' > "$NOTES/shared-a-request.json"
OP_A=$(jq -er '.operation_id' "$NOTES/shared-a-request.json") || exit 1
make api ARGS="management GET '/operations/$OP_A'"
```

Expect HTTP 202 and an operation ID.

#### Optional admission checks

Duplicate requests must return 409 without creating another operation:

```bash
printf '%s\n' '{"tenant_id":"shared-a","isolation":"shared","initial_message":"alpha"}' | \
  make api ARGS='management POST /tenants'
```

While the first operation is still `pending` or `running`, you can also test
the single-active-operation rule. Skip these two calls if it already finished:

```bash
printf '%s\n' '{"tenant_id":"busy-check","isolation":"shared","initial_message":"not accepted"}' | \
  make api ARGS='management POST /tenants'
make api ARGS='management GET /tenants/busy-check'
```

Expect 503 `provisioner_busy`, then 404. If you get 202, you accepted another
tenant: wait for that operation too and do not count this as a successful busy
check.

#### Follow provisioning

Repeat these reads while the first operation is pending or running:

```bash
make api ARGS='management GET /tenants/shared-a' | jq '{pair_id, provisioning_status, provisioning_stage, onboarding_status, control_record}'
make kube ARGS='management logs deployment/provisioner --tail=20'
```

Management Radius should create the shared control/data clusters. The provisioner
installs child Radius and deploys their applications. Require both
`provisioning_status: succeeded` and `onboarding_status: ready`.
Stop on `failed` or `interrupted`; do not reset state to force a retry.

```bash
make report
make api ARGS='control:shared GET /tenants/shared-a'
make api ARGS='data:shared GET /tenants/shared-a'
make kube ARGS='shared-data get configmap tenant-shared-a -o json' | jq .data
```

Expect three discovered endpoints. Control should become `applied`; data should
return `alpha`, version 1, and counter 0. Match the `onboarding_id` across
the three APIs.

Save the logical placement for the reuse check. Management does not store or
return endpoint URLs; endpoint and cluster discovery use provider APIs.

```bash
make api ARGS='management GET /tenants/shared-a' | jq '{pair_id}' > "$NOTES/shared-pair.json"
for slot in shared-control shared-data; do
  make kube ARGS="'$slot' get namespace kube-system -o json" | jq -er .metadata.uid
done > "$NOTES/shared-clusters-before.txt"
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
make api ARGS='management GET /tenants/shared-b' | jq '{pair_id}' > "$NOTES/shared-b-pair.json"
diff -u "$NOTES/shared-pair.json" "$NOTES/shared-b-pair.json"
make report | jq '.endpoints | keys'
for slot in shared-control shared-data; do
  make kube ARGS="'$slot' get namespace kube-system -o json" | jq -er .metadata.uid
done > "$NOTES/shared-clusters-after.txt"
diff -u "$NOTES/shared-clusters-before.txt" "$NOTES/shared-clusters-after.txt"
```

Expect no diff and still three slots. The new shared tenant added records,
not another cluster pair.

### C. Provision an isolated tenant

This tenant should get its own control/data pair. The two pairs must not serve
each other's tenant records.

```bash
printf '%s\n' '{"tenant_id":"isolated-c","isolation":"isolated","initial_message":"charlie"}' | \
  make api ARGS='management POST /tenants'
make api ARGS='management GET /tenants/isolated-c'
make kube ARGS='management logs deployment/provisioner --tail=20'
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

Capture a baseline after updates have applied:

```bash
POLL_FROM=$(uv run --no-sync python -c \
  'from datetime import UTC, datetime; print(datetime.now(UTC).isoformat())')
make api ARGS='management GET /tenants/shared-a' > "$NOTES/m-before.json"
make api ARGS='control:shared GET /tenants/shared-a' > "$NOTES/c-before.json"
make api ARGS='data:shared GET /tenants/shared-a' > "$NOTES/d-before.json"
sleep 15
make kube ARGS="shared-control logs deployment/control-reconciler '--since-time=$POLL_FROM' --timestamps=true --tail=20"
make api ARGS='management GET /tenants/shared-a' > "$NOTES/m-after.json"
make api ARGS='control:shared GET /tenants/shared-a' > "$NOTES/c-after.json"
make api ARGS='data:shared GET /tenants/shared-a' > "$NOTES/d-after.json"
diff -u "$NOTES/m-before.json" "$NOTES/m-after.json"
diff -u "$NOTES/c-before.json" "$NOTES/c-after.json"
diff -u "$NOTES/d-before.json" "$NOTES/d-after.json"
```

Require a successful `control_poll` after `$POLL_FROM`, with `succeeded` greater
than zero and `failed=0`, plus unchanged snapshots. That shows control retained
its updates while polling management. No successful poll means no proof yet.

Read a timeline in small pages:

```bash
make api ARGS="control:shared GET '/tenants/shared-a?limit=2'" > "$NOTES/page.json"
jq '{timeline, next_after_event_id}' "$NOTES/page.json"
CURSOR=$(jq -r '.next_after_event_id' "$NOTES/page.json")
if [ "$CURSOR" != null ]; then
  make api ARGS="control:shared GET '/tenants/shared-a?limit=2&after_event_id=$CURSOR'"
fi
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
make api ARGS='management GET /tenants/shared-a' > "$NOTES/m-blocked-before.json"
printf '%s\n' '{"message":"without-management"}' | \
  make api ARGS='control:shared PUT /tenants/shared-a/configuration'
make api ARGS='data:shared GET /tenants/shared-a'
make api ARGS='data:shared POST /tenants/shared-a/counter'
```

The new message should reach data through control's own database. Keep reading
and incrementing for at least 60 seconds, then:

```bash
make api ARGS='management GET /tenants/shared-a' > "$NOTES/m-blocked-after.json"
diff -u "$NOTES/m-blocked-before.json" "$NOTES/m-blocked-after.json"
```

Expect no new management reports during blockage. After the fault terminal
finishes:

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

Start only after the preceding fault is restored. Save the current data response:

```bash
make api ARGS='data:shared GET /tenants/shared-a' > "$NOTES/outage-baseline.json"
jq . "$NOTES/outage-baseline.json"
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
the baseline message/version while its counter still works.

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

Cleanup destroys the selected demo's application data. Save any response
comparisons you need, restore faults and paused workloads, and finish or inspect
active provisioning before proceeding. Check that `.env` still selects Azure:

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
| Tenant stays pending | Read its operation and management provisioner logs. Management readiness still requires a control record. |
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

The first command creates the tenants and checks reuse, isolation, configuration,
counters and access. The second checks the parent outages and recovery. A single
`all` run is another option:

```bash
uv run --no-sync python scripts/harness/test-e2e.py --environment azure --mode all --execute
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
authentication. The singleton provisioner has no HA scheduler or automatic
replay of interrupted infrastructure work; tenant migration and deletion APIs
are outside the demo.

PostgreSQL and Redis use private Azure connectivity and verified TLS. Certificate
issuance is implemented, but automatic certificate renewal is not. Parent
outages demonstrate configuration independence, not disaster recovery or
reconstruction of lost clusters/databases.
