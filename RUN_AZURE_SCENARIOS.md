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

```bash
bash
set -o pipefail
umask 077
```

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
Azure user account, not a service principal. The operator needs permission to
create the project's resource groups, resources, managed identities, custom
roles, and scoped role assignments. The workstation must reach the AKS APIs.
Docker Desktop is used to inspect built images.

```bash
uv sync --locked
git status --short
make check-bicep
```

`git status` should be clean. `make check-bicep` compiles infrastructure and
generates type extensions; it does not deploy resources. `make check` runs the
full source checks if you want that checkpoint before deploying.

### Select operator configuration

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

Checkpoint: bootstrap completed and management AKS and Radius exist. The
management application and tenant clusters have not been deployed yet.

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

### Load the shell controls

These helpers keep requests short while discovering access for each command.
Run them in your main terminal:

```bash
set -o pipefail
umask 077
NOTES=$(mktemp -d "${TMPDIR:-/tmp}/plane-manual.XXXXXX")

api() { bash scripts/operations/api.sh "$@"; }
k() { bash scripts/operations/kube.sh "$@"; }
endpoint() { bash scripts/operations/endpoints.sh "$1" | jq -er '.url'; }

report() {
  uv run --no-sync python scripts/harness/export-state.py --environment azure --once
}

journal() {
  k "$1" get configmap "plane-demo-fault-$2" -o json | jq -er '.data["record.json"] | fromjson'
}

report
k management get pods,pvc
k management logs deployment/provisioner --tail=20
api management GET /healthz
```

These shortcuts keep the commands below short:

| Command | What it does |
|---|---|
| `api management GET ...` | Discovers the endpoint/key and sends one HTTP request |
| `k shared-data get ...` | Uses fresh scoped access, then discards its temporary kubeconfig |
| `report` | Prints current topology and tenant status; creates no tenants or inventory file |
| `journal SLOT COMPONENT` | Reads the fault record from its owning Kubernetes ConfigMap |

Initially the report contains only the management endpoint and no tenants.
Require `provisioner_ready` and HTTP 200 before onboarding. Reports fail nonzero
on observation errors. `$NOTES` holds only your optional response comparisons.
No command discovers infrastructure or credentials from those notes. The worker
has no credential-seed file or working-state PVC.

Successful reads return HTTP 200; tenant acceptance returns 202. The helper
prints status to stderr and JSON to stdout. Expected 401/404/409/422/503 checks
return nonzero, so run the blocks individually rather than as one unattended
script. Two matching error responses do not prove unchanged application state.

For a new terminal/session, return to this checkout and reload these controls.
Resume with checkpoint reads, not deployment commands or tenant POSTs.
Restore any active fault or paused workload before taking a break.

### A. Provision the first shared tenant

You follow one request from acceptance to infrastructure creation and a working
data response.

```bash
api management GET /healthz
api management GET /tenants/shared-a
```

Expect 200, then 404. Send the request once:

```bash
api management POST /tenants \
  '{"tenant_id":"shared-a","isolation":"shared","initial_message":"alpha"}' \
  > "$NOTES/shared-a-request.json"
OP_A=$(jq -er '.operation_id' "$NOTES/shared-a-request.json") || exit 1
api management GET "/operations/$OP_A"
```

Expect HTTP 202 and an operation ID.

#### Optional admission checks

Duplicate requests must return 409 without creating another operation:

```bash
api management POST /tenants \
  '{"tenant_id":"shared-a","isolation":"shared","initial_message":"alpha"}'
```

While the first operation is still `pending` or `running`, you can also test
the single-active-operation rule. Skip these two calls if it already finished:

```bash
api management POST /tenants \
  '{"tenant_id":"busy-check","isolation":"shared","initial_message":"not accepted"}'
api management GET /tenants/busy-check
```

Expect 503 `provisioner_busy`, then 404. If you get 202, you accepted another
tenant: wait for that operation too and do not count this as a successful busy
check.

#### Follow provisioning

Repeat these reads while the first operation is pending or running:

```bash
api management GET /tenants/shared-a \
  | jq '{pair_id, provisioning_status, provisioning_stage, onboarding_status, control_record}'
k management logs deployment/provisioner --tail=20
```

Management Radius should create the shared control/data clusters. The provisioner
installs child Radius and deploys their applications. Require both
`provisioning_status: succeeded` and `onboarding_status: ready`.
Stop on `failed` or `interrupted`; do not reset state to force a retry.

```bash
report
api control:shared GET /tenants/shared-a
api data:shared GET /tenants/shared-a
k shared-data get configmap tenant-shared-a -o json | jq .data
```

Expect three discovered endpoints. Control should become `applied`; data should
return `alpha`, version 1, and counter 0. Match the `onboarding_id` across
the three APIs.

Save the logical placement for the reuse check. Management does not store or
return endpoint URLs; endpoint and cluster discovery use provider APIs.

```bash
api management GET /tenants/shared-a \
  | jq '{pair_id}' > "$NOTES/shared-pair.json"
for slot in shared-control shared-data; do
  k "$slot" get namespace kube-system -o jsonpath='{.metadata.uid}{"\n"}'
done > "$NOTES/shared-clusters-before.txt"
```

### B. Reuse the pair while data reconciliation is paused

This separates management readiness from data configuration application while
checking that the same cluster pair is reused.

Pause only the shared data reconciler:

```bash
k shared-data scale deployment/data-reconciler --current-replicas=1 --replicas=0
k shared-data get pods -l plane-demo/component=data-reconciler
```

Wait until no matching Pods remain, including terminating Pods. Then:

```bash
api management POST /tenants \
  '{"tenant_id":"shared-b","isolation":"shared","initial_message":"bravo"}'
api management GET /tenants/shared-b
api control:shared GET /tenants/shared-b
api data:shared GET /tenants/shared-b
```

Wait for management readiness and provisioning success. Control should have
`bravo` but report data as `pending`, with no applied version. Data should return
404. This shows that management readiness does not wait for data application.

Restore the reconciler before leaving this section, even after an error:

```bash
k shared-data scale deployment/data-reconciler --current-replicas=0 --replicas=1
k shared-data rollout status deployment/data-reconciler --timeout=60s
api control:shared GET /tenants/shared-b
api data:shared GET /tenants/shared-b
```

Wait for `applied` and the `bravo` response, then compare placement:

```bash
api management GET /tenants/shared-b \
  | jq '{pair_id}' > "$NOTES/shared-b-pair.json"
diff -u "$NOTES/shared-pair.json" "$NOTES/shared-b-pair.json"
report | jq '.endpoints | keys'
for slot in shared-control shared-data; do
  k "$slot" get namespace kube-system -o jsonpath='{.metadata.uid}{"\n"}'
done > "$NOTES/shared-clusters-after.txt"
diff -u "$NOTES/shared-clusters-before.txt" "$NOTES/shared-clusters-after.txt"
```

Expect no diff and still three slots. The new shared tenant added records,
not another cluster pair.

### C. Provision an isolated tenant

This tenant should get its own control/data pair. The two pairs must not serve
each other's tenant records.

```bash
api management POST /tenants \
  '{"tenant_id":"isolated-c","isolation":"isolated","initial_message":"charlie"}'
api management GET /tenants/isolated-c
k management logs deployment/provisioner --tail=20
```

Wait for provisioning success and management readiness:

```bash
report
api control:isolated-1 GET /tenants/isolated-c
api data:isolated-1 GET /tenants/isolated-c
for slot in management shared-control shared-data isolated-1-control isolated-1-data; do
  printf '%s ' "$slot"
  k "$slot" get namespace kube-system -o jsonpath='{.metadata.uid}{"\n"}'
done
```

Expect five slots with five distinct cluster UIDs. The isolated tenant has
different control/data endpoints and its own PostgreSQL/Redis instances.
Wait for `charlie`, version 1, on data.

Check that the wrong pair does not host the records:

```bash
api control:shared GET /tenants/isolated-c
api data:shared GET /tenants/isolated-c
api control:isolated-1 GET /tenants/shared-a
api data:isolated-1 GET /tenants/shared-a
```

Expect 404 for each. These use valid plane keys and demonstrate placement,
not production tenant authentication.

### D. Update configuration and compare counters

Change tenant configuration through control, then check that data applies it
without resetting counters.

```bash
api data:shared GET /tenants/shared-a
api data:shared GET /tenants/shared-b
api data:isolated-1 GET /tenants/isolated-c
api data:shared POST /tenants/shared-a/counter
api data:shared POST /tenants/shared-a/counter
```

Each POST must increment only `shared-a` by one. GET must not increment.
Read the other two tenants again; their counters should be unchanged.
Use the values you observed, rather than assuming zero after repeated commands.

Update each tenant through control:

```bash
api control:shared PUT /tenants/shared-a/configuration '{"message":"alpha-v2"}'
api control:shared PUT /tenants/shared-b/configuration '{"message":"bravo-v2"}'
api control:isolated-1 PUT /tenants/isolated-c/configuration '{"message":"charlie-v2"}'
api control:shared GET /tenants/shared-a
api data:shared GET /tenants/shared-a
api data:shared GET /tenants/shared-b
api data:isolated-1 GET /tenants/isolated-c
k shared-data get configmap tenant-shared-a -o json | jq .data
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
api management GET /tenants/shared-a > "$NOTES/m-before.json"
api control:shared GET /tenants/shared-a > "$NOTES/c-before.json"
api data:shared GET /tenants/shared-a > "$NOTES/d-before.json"
sleep 15
k shared-control logs deployment/control-reconciler \
  --since-time="$POLL_FROM" --timestamps=true --tail=20
api management GET /tenants/shared-a > "$NOTES/m-after.json"
api control:shared GET /tenants/shared-a > "$NOTES/c-after.json"
api data:shared GET /tenants/shared-a > "$NOTES/d-after.json"
diff -u "$NOTES/m-before.json" "$NOTES/m-after.json"
diff -u "$NOTES/c-before.json" "$NOTES/c-after.json"
diff -u "$NOTES/d-before.json" "$NOTES/d-after.json"
```

Require a successful `control_poll` after `$POLL_FROM`, with `succeeded` greater
than zero and `failed=0`, plus unchanged snapshots. That shows control retained
its updates while polling management. No successful poll means no proof yet.

Read a timeline in small pages:

```bash
api control:shared GET '/tenants/shared-a?limit=2' > "$NOTES/page.json"
jq '{timeline, next_after_event_id}' "$NOTES/page.json"
CURSOR=$(jq -r '.next_after_event_id' "$NOTES/page.json")
if [ "$CURSOR" != null ]; then
  api control:shared GET "/tenants/shared-a?limit=2&after_event_id=$CURSOR"
fi
```

Continue with each returned cursor until `null`. IDs increase but need not be
consecutive. Management reports control-record creation; control reports
configuration changes and data application.

Check rejected requests:

```bash
curl -q -sS -o /dev/null -w 'HTTP %{http_code}\n' "$(endpoint management)/tenants/shared-a"
curl -q -sS -o /dev/null -w 'HTTP %{http_code}\n' \
  -H 'X-Demo-Key: wrong' "$(endpoint management)/tenants/shared-a"
api management POST /tenants \
  '{"tenant_id":"Bad ID","isolation":"shared","initial_message":"invalid"}'
api management POST /tenants \
  '{"tenant_id":"shared-a","isolation":"shared","initial_message":"must not replace alpha"}'
```

Expect 401, 401, 422, and 409. The duplicate must not reset control's configuration
or the counter. Repeat the curl checks for the child API URLs listed by:

```bash
make endpoints ARGS=all
```

Inspect the data API identity without displaying Secrets:

```bash
k shared-data get deployment data-api \
  -o jsonpath='{.spec.template.spec.serviceAccountName}{"\n"}'
DATA_NS=$(k shared-data get deployment data-api -o jsonpath='{.metadata.namespace}')
API_ID="system:serviceaccount:$DATA_NS:data-api-runtime"
k shared-data auth can-i get configmaps --as="$API_ID"
k shared-data auth can-i get secret/data-reconciler-runtime --as="$API_ID"
k shared-data auth can-i list secrets --as="$API_ID"
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
journal "$FAULT_SLOT" "$FAULT_COMPONENT" | jq '{slot, component, outcome, blocked_at, restored}'
```

Require `shared-control`, `control-reconciler`, and `blocked_verified` before
continuing. While the helper holds the fault:

```bash
api management GET /tenants/shared-a > "$NOTES/m-blocked-before.json"
api control:shared PUT /tenants/shared-a/configuration '{"message":"without-management"}'
api data:shared GET /tenants/shared-a
api data:shared POST /tenants/shared-a/counter
```

The new message should reach data through control's own database. Keep reading
and incrementing for at least 60 seconds, then:

```bash
api management GET /tenants/shared-a > "$NOTES/m-blocked-after.json"
diff -u "$NOTES/m-blocked-before.json" "$NOTES/m-blocked-after.json"
```

Expect no new management reports during blockage. After the fault terminal
finishes:

```bash
journal "$FAULT_SLOT" "$FAULT_COMPONENT" \
  | jq '{outcome, restored, physical_restored, restoration_started_at, restored_at}'
RESTORE_FROM=$(journal "$FAULT_SLOT" "$FAULT_COMPONENT" | jq -er '.restoration_started_at')
k shared-control logs deployment/control-reconciler \
  --since-time="$RESTORE_FROM" --timestamps=true --tail=20
```

Require `verified_and_restored`, both restoration flags true, and a successful
control poll after restoration. Recovery must not add a duplicate
`control_record_created` event.

### G. Block control, queue updates, and restart data

Data should keep serving its applied configuration through the outage and an
API restart, then apply the newest control version after reconnection.

Start only after the preceding fault is restored. Save the current data response:

```bash
api data:shared GET /tenants/shared-a > "$NOTES/outage-baseline.json"
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
journal "$FAULT_SLOT" "$FAULT_COMPONENT" | jq '{slot, component, outcome, blocked_at, restored}'
```

Require `shared-data`, `data-reconciler`, and `blocked_verified`. Now data cannot
read control PostgreSQL.

```bash
api control:shared PUT /tenants/shared-a/configuration '{"message":"queued-first"}'
api control:shared PUT /tenants/shared-a/configuration '{"message":"queued-latest"}'
api control:shared GET /tenants/shared-a
api data:shared GET /tenants/shared-a
api data:shared POST /tenants/shared-a/counter
```

Control should report the latest version as `pending`. Data must keep returning
the baseline message/version while its counter still works.

Replace only the data API while the link remains blocked:

```bash
k shared-data get pods -l plane-demo/component=data-api \
  -o custom-columns=NAME:.metadata.name,UID:.metadata.uid
k shared-data rollout restart deployment/data-api
k shared-data rollout status deployment/data-api --timeout=30s
k shared-data get pods -l plane-demo/component=data-api \
  -o custom-columns=NAME:.metadata.name,UID:.metadata.uid
api data:shared GET /tenants/shared-a
```

Require a new Pod UID and the old applied message/version with the current
counter. Keep requests going for at least 60 seconds of verified blockage.
Do not replace the data reconciler during the fault; the helper checks its Pod identity.

After restoration:

```bash
journal "$FAULT_SLOT" "$FAULT_COMPONENT" | jq '{outcome, restored, physical_restored}'
api control:shared GET /tenants/shared-a
api data:shared GET /tenants/shared-a
k shared-data get configmap tenant-shared-a -o json | jq .data
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
journal "$FAULT_SLOT" "$FAULT_COMPONENT" | jq '{outcome, restored, physical_restored}'
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

### Optional: retain the foundation

To remove applications and child clusters but keep management AKS, registry,
networking and identities, use the normal cleaner's narrower mode:

```bash
uv run --no-sync python scripts/operations/clean-azure.py --radius-only
CONFIRM_AZURE=yes uv run --no-sync python scripts/operations/clean-azure.py --radius-only --execute
```

Its success is `radius_resources_removed`, not a clean whole environment.
Full `make verify-clean` is only appropriate after full cleanup.

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
k shared-data get pods
k shared-data logs deployment/data-reconciler --tail=40
k shared-data get configmap tenant-shared-a -o json
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
