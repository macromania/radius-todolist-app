# Run local scenarios

Run each block from a Bash terminal at the repository root. This walkthrough
uses Docker Desktop and kind to run the three-plane demo without Azure.
Stop at each checkpoint before continuing.

A fresh live end-to-end verification run of the current implementation remains
outstanding. The checkpoints describe expected results to verify.

**Resource scope:** the full walkthrough creates five kind clusters in Docker
Desktop. Only one local deployment can use the reserved ports at a time.
No Azure account is required. Read [cleanup](#5-clean-up-local) before starting;
it destroys the selected demo's databases and local volume data.

The order is:

1. [Prepare the workspace](#1-prepare-the-workspace).
2. [Deploy local management](#2-deploy-local-management).
3. [Run the manual scenarios](#3-run-the-manual-scenarios).
4. [Check datastore persistence](#4-check-datastore-persistence).
5. [Clean up local](#5-clean-up-local).

This guide includes its own setup and shell controls. Do not run the
[automated alternative](#automated-checks) alongside these manual requests;
it uses the same tenant names.

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

Each plane runs in its own kind cluster, backed by Docker containers.
Management's provisioner asks management Radius to create children using the
kind Recipe, its provider-specific template. Each child gets its own Radius
installation to deploy its applications and dependencies.

Shared tenants reuse a control/data pair; an isolated tenant gets another pair.
Reconciler processes poll their parent databases and report local progress.
The application declarations are the same ones used for Azure.

| Check | Meaning |
|---|---|
| Management `onboarding_status: ready` | Control created the tenant record |
| Management `provisioning_status: succeeded` | The provisioning operation finished |
| Control `data_config.status: applied` | Data applied the requested ConfigMap version |
| Data returns message, version, and counter | The request used local configuration and Redis |
| `/healthz` returns 200 | The API process is alive; this is not dependency readiness |

## 1. Prepare the workspace

Use a clean checkout of committed source. Keep that revision unchanged through
build, deployment and the scenarios.

Use Bash for the commands below. Have Git, `uv`, `jq`, `curl`, `tar`, `helm`,
Docker Desktop, ShellCheck, and the following tools installed:

| Tool | Version |
|---|---|
| Project Python | 3.13, through `uv` |
| Radius | 0.60.2 |
| Radius Bicep | 0.42.1, at `$HOME/.rad/bin/bicep` |
| kubectl | 1.35.7 |
| kind | 0.31.0 |
| Terraform for offline checks | 1.14-1.15; CI/runtime use 1.15.8 |

```bash
uv sync --locked
git status --short
make check-bicep
docker --context desktop-linux version
```

`git status` should be clean and Docker Desktop should respond.
`make check-bicep` compiles infrastructure and generates type extensions without
creating resources. `make check` runs the full source checks if you want that
checkpoint before deploying.

Bootstrap shows three phases: verify tools/images, prepare management, and
install/verify Radius. Phase numbers indicate order, not time. Progress goes
to stderr; JSON/API stdout and complete native build/push logs remain intact.
Redirected output is plain. Use `COLOR=never` or `NO_COLOR=1` to disable styling
and animation; `COLOR=always` forces styling unless `NO_COLOR` is set. Spinners
stop for prompts and native output.

### Select operator configuration

Choose the local deployment identity:

```bash
make init ENV=local
make show-config
```

Initialization writes or replaces the private, git-ignored `.env`; it creates
no resources. The defaults are project `radplanes` and deployment `learning`.
For another selection, use
`make init ENV=local ARGS='--project demo --deployment team'`.
`make show-config` treats the file as data and redacts keys.

Initialization replaces rather than appends or merges settings. Repeating the
same inputs writes each key once. Switching from Azure removes its old settings
and keys. Invalid inputs or duplicate credential slots preserve the previous file.

To supply a demo key, add `--demo-key-from-env SLOT=VARIABLE` to initialization,
with the value already set privately in that environment variable. Never put
the key itself in `ARGS` or shell history.

Later commands read `.env`; a different `ENV` argument does not retarget them.
Credentials belong in Kubernetes Secrets, business/operation records in
PostgreSQL, and infrastructure/fault progress with their resource owners.
Temporary CLI access files are discarded. No Azure login, subscription,
Key Vault or cloud registry is needed.

Local commands resolve Docker Desktop's `desktop-linux` context and pin its
local Unix socket. They do not change the global Docker context or use another
container runtime. Management Radius needs Docker daemon access to create kind
children; this grants it whole-daemon authority. Use a trusted operator.
API and provisioner containers do not receive that socket.

Never run two local deployments concurrently: deployment-specific names still
use the same reserved loopback host ports:

| Slot | Gateway | Kubernetes API |
|---|---:|---:|
| management | 35490 | 35495 |
| shared-control | 35491 | 35496 |
| shared-data | 35492 | 35497 |
| isolated-1-control | 35493 | 35498 |
| isolated-1-data | 35494 | 35499 |

## 2. Deploy local management

The order is build, bootstrap, then management deployment.

### Build and inspect the images

```bash
make build CONFIRM_LOCAL=yes
make inspect-build
```

Build prepares the API, provisioner, Radius executor/operator, charts, Terraform
and provider dependencies before cluster creation. Public downloads belong to
this stage. Inspection checks actual bytes and runtime permissions rather than
trusting a tag. Existing immutable image tags are not overwritten.

### Create management and install Radius

```bash
make bootstrap CONFIRM_LOCAL=yes
```

Checkpoint: only management exists, with Radius installed and management Secret
encryption verified. The encryption key belongs to the kind node, not a mounted
checkout file. Child creation belongs to management Radius.

### Deploy the management application

Start PostgreSQL, the API and the provisioner:

```bash
make deploy-management CONFIRM_LOCAL=yes
make kube ARGS='management get pods,pvc'
make kube ARGS='management logs deployment/provisioner --tail=20'
```

Deployment registers the prepared Recipes, initializes management PostgreSQL,
and starts the API/provisioner. It waits for actual completion and creates no
child cluster. To inspect Recipe registration as a separate manual checkpoint,
run `make local-setup CONFIRM_LOCAL=yes` before deployment. No saved setup record
or host credential bundle is required.

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

Initially require only management, no tenants, a bound `postgres-data` PVC,
`provisioner_ready`, and HTTP 200. `make fault-status ARGS='SLOT COMPONENT'`
reads a fault's Kubernetes journal; the running fault helper performs the
network checks.

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
The local fault helper uses the reconciler Pod's network namespace because
kind's default CNI does not enforce NetworkPolicy.

In the second terminal:

```bash
make fault CONFIRM_LOCAL=yes \
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
make fault CONFIRM_LOCAL=yes \
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
Do not replace the data reconciler whose network namespace owns the local fault.

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
make fault CONFIRM_LOCAL=yes \
  ARGS="--slot $FAULT_SLOT --component $FAULT_COMPONENT --restore"
make fault-status ARGS="'$FAULT_SLOT' '$FAULT_COMPONENT'" | jq '{outcome, restored, physical_restored}'
```

Do not use `kill -9`, replace the faulted reconciler, or delete its cluster to
clear a fault. Do not proceed until the original link is confirmed restored.

### H. Check what a plane key can reach

API keys belong to a plane instance, not to a tenant. Any caller holding the
shared data key can read and change every tenant on the shared pair.
This section shows that boundary rather than testing tenant authentication.

Read the shared control and data URLs and keys into shell variables without
printing them:

```bash
set -o pipefail
CONTROL_URL=$(make endpoints ARGS=shared-control | jq -er '.url') || exit 1
CONTROL_KEY=$(make kube ARGS='shared-control get secret control-api-runtime -o json' | \
  jq -er '.data.DEMO_KEY | @base64d') || exit 1
DATA_URL=$(make endpoints ARGS=shared-data | jq -er '.url') || exit 1
DATA_KEY=$(make kube ARGS='shared-data get secret data-api-runtime -o json' | \
  jq -er '.data.DEMO_KEY | @base64d') || exit 1
```

Use one key for both shared tenants:

```bash
for tenant in shared-a shared-b; do
  curl -q -sS -o /dev/null -w "control $tenant: HTTP %{http_code}\n" \
    -H "X-Demo-Key: $CONTROL_KEY" "$CONTROL_URL/tenants/$tenant"
  curl -q -sS -o /dev/null -w "data $tenant: HTTP %{http_code}\n" \
    -H "X-Demo-Key: $DATA_KEY" "$DATA_URL/tenants/$tenant"
done
make api ARGS='data:shared GET /tenants/shared-b' | jq '{counter}'
curl -q -sS --fail-with-body -X POST -H "X-Demo-Key: $DATA_KEY" \
  "$DATA_URL/tenants/shared-b/counter" | jq '{tenant_id, counter}'
```

Expect four 200 responses. The POST increments `shared-b` by one: the same key
that serves `shared-a` changed another tenant. Nothing in the request identifies
the caller as a tenant.

Keys do not cross planes or pairs:

```bash
MANAGEMENT_URL=$(make endpoints ARGS=management | jq -er '.url') || exit 1
curl -q -sS -o /dev/null -w 'data key on control: HTTP %{http_code}\n' \
  -H "X-Demo-Key: $DATA_KEY" "$CONTROL_URL/tenants/shared-a"
curl -q -sS -o /dev/null -w 'control key on data: HTTP %{http_code}\n' \
  -H "X-Demo-Key: $CONTROL_KEY" "$DATA_URL/tenants/shared-a"
curl -q -sS -o /dev/null -w 'data key on management: HTTP %{http_code}\n' \
  -H "X-Demo-Key: $DATA_KEY" "$MANAGEMENT_URL/tenants/shared-a"
curl -q -sS -o /dev/null -w 'shared data key on isolated-1: HTTP %{http_code}\n' \
  -H "X-Demo-Key: $DATA_KEY" \
  "$(make endpoints ARGS=isolated-1-data | jq -er '.url')/tenants/isolated-c"
```

Expect 401 for each. Shared and isolated placement separate infrastructure and
data; authorization is per plane instance, not per tenant. This demo does not
provide hostile-tenant isolation.

### I. Observe a noisy neighbor

Generate sustained counter traffic for `shared-b` and watch `shared-a` on the
same data API, Redis and reconciler. The demo has no rate limits or per-tenant
quotas, so this records behavior rather than asserting a threshold.

Reuse the variables from section H and record a baseline:

```bash
probe() {
  for _ in $(seq 1 30); do
    curl -q -sS -o /dev/null -w '%{http_code} %{time_total}\n' \
      -H "X-Demo-Key: $DATA_KEY" "$DATA_URL/tenants/shared-a"
    sleep 0.5
  done | sort -k2 -n | awk '{t[NR]=$2; if ($1 != 200) e++}
    END {printf "requests=%d errors=%d p50=%.3fs p95=%.3fs max=%.3fs\n",
      NR, e, t[int(NR/2)+1], t[int(NR*0.95)], t[NR]}'
}
make api ARGS='data:shared GET /tenants/shared-a' | jq '{message, applied_version, counter}'
make api ARGS='data:shared GET /tenants/shared-b' | jq '{counter}'
probe
```

Start four workers for 60 seconds, then measure `shared-a` and one
configuration change while they run:

```bash
for worker in 1 2 3 4; do
  (end=$((SECONDS + 60))
   while (( SECONDS < end )); do
     curl -q -sS -o /dev/null -w '%{http_code}\n' -X POST \
       -H "X-Demo-Key: $DATA_KEY" "$DATA_URL/tenants/shared-b/counter"
   done > "$NOTES/noise-$worker.txt") &
done
sleep 5
probe
START=$SECONDS
VERSION=$(printf '%s\n' '{"message":"alpha-under-load"}' | \
  make api ARGS='control:shared PUT /tenants/shared-a/configuration' | jq -er '.desired.version') || exit 1
until make api ARGS='data:shared GET /tenants/shared-a' 2>/dev/null | \
  jq -e --argjson v "$VERSION" '.applied_version == $v' >/dev/null; do sleep 1; done
echo "applied version $VERSION after about $((SECONDS - START)) s"
wait
```

Then count the noise and compare counters:

```bash
cat "$NOTES"/noise-*.txt | sort | uniq -c
make api ARGS='data:shared GET /tenants/shared-a' | jq '{message, applied_version, counter}'
make api ARGS='data:shared GET /tenants/shared-b' | jq '{counter}'
```

Require zero `shared-a` errors, `alpha-under-load` applied within a few poll
intervals, an unchanged `shared-a` counter, and a `shared-b` counter increased by
exactly the number of 200 responses. Record the latency percentiles and any
non-200 codes. All kind clusters share Docker Desktop's CPU and memory, so this
is not a capacity test of Redis, PostgreSQL connections or API workers.

### J. Stop Redis and recover

Data should keep its configuration but refuse requests while Redis is down,
then return the same counters afterwards. Redis is shared by every tenant on the
data pair.

Save the current responses, then stop only the shared data Redis:

```bash
make api ARGS='data:shared GET /tenants/shared-a' > "$NOTES/redis-a-before.json"
make api ARGS='data:shared GET /tenants/shared-b' > "$NOTES/redis-b-before.json"
make kube ARGS='shared-data scale statefulset/redis --current-replicas=1 --replicas=0'
make kube ARGS='shared-data get pods'
```

Wait until `redis-0` is gone, including a terminating Pod. Then:

```bash
make api ARGS='data:shared GET /tenants/shared-a'
make api ARGS='data:shared POST /tenants/shared-b/counter'
make kube ARGS='shared-data get configmap tenant-shared-a -o json' | jq .data
make api ARGS='data:isolated-1 GET /tenants/isolated-c'
```

Expect 503 `local_counter_unavailable` for both shared tenants, including the
GET, because every response reads the counter. The ConfigMap is still present.
The isolated tenant keeps returning 200 from its own Redis.

Restore Redis before leaving this section, even after an error:

```bash
make kube ARGS='shared-data scale statefulset/redis --current-replicas=0 --replicas=1'
make kube ARGS='shared-data rollout status statefulset/redis --timeout=180s'
make api ARGS='data:shared GET /tenants/shared-a' > "$NOTES/redis-a-after.json"
make api ARGS='data:shared GET /tenants/shared-b' > "$NOTES/redis-b-after.json"
diff -u "$NOTES/redis-a-before.json" "$NOTES/redis-a-after.json"
diff -u "$NOTES/redis-b-before.json" "$NOTES/redis-b-after.json"
```

Expect no diff: the rejected POST did not count, and the append-only file on the
retained `redis-data` PVC restored every counter. This is a clean shutdown, not
forced-crash durability.

### K. Fail and retry a control record

Management should report a failed control record without calling the tenant
failed, and the reconciler should recover on its own. This takes the shared
control database down while a new tenant is onboarded.

Stop only the shared control PostgreSQL:

```bash
make kube ARGS='shared-control scale statefulset/postgres --current-replicas=1 --replicas=0'
make kube ARGS='shared-control get pods'
```

Wait until `postgres-0` is gone. Then accept a tenant and follow it:

```bash
printf '%s\n' '{"tenant_id":"shared-k","isolation":"shared","initial_message":"kilo"}' | \
  make api ARGS='management POST /tenants'
make api ARGS='management GET /tenants/shared-k' | jq '{provisioning_status, onboarding_status, control_record}'
make kube ARGS='shared-control logs deployment/control-reconciler --tail=20'
make api ARGS='control:shared GET /tenants/shared-a'
make api ARGS='data:shared GET /tenants/shared-a'
```

Wait for `provisioning_status: succeeded`. Management must show
`onboarding_status: pending` with `control_record.status: failed`; it never
reports `onboarding_status: failed`. The reconciler logs `control_record_failed`
on each poll. Control returns 503 `database_unavailable`, while data still serves
`shared-a` from its ConfigMap and Redis.

Restore the database before leaving this section, even after an error:

```bash
make kube ARGS='shared-control scale statefulset/postgres --current-replicas=0 --replicas=1'
make kube ARGS='shared-control rollout status statefulset/postgres --timeout=180s'
make api ARGS='management GET /tenants/shared-k' | \
  jq '{onboarding_status, control_record, events: [.timeline[] | select(.type | startswith("control_record")) | .type]}'
make api ARGS='control:shared GET /tenants/shared-k'
make api ARGS='data:shared GET /tenants/shared-k'
```

Within a few polls, require `ready`, `control_record.status: created`, one
`control_record_failed` event followed by one `control_record_created`, and data
returning `kilo`, version 1. Repeated failures do not add duplicate events.
Existing tenants keep their single `control_record_created` event.

### L. Block a ConfigMap update

Data should keep serving its last applied configuration when the data reconciler
cannot write the tenant's ConfigMap. Control should report the new version as
failed, then applied after the block is removed.

This marks one tenant ConfigMap immutable, so Kubernetes rejects the
reconciler's next change to it. An immutable ConfigMap cannot be made mutable
again; the only restore is deleting it so the reconciler recreates it. Target
only `tenant-shared-a`.

Read the current state:

```bash
make api ARGS='data:shared GET /tenants/shared-a' | jq '{message, applied_version, counter}'
make api ARGS='data:shared GET /tenants/shared-b' | jq '{message, applied_version}'
make kube ARGS="shared-data patch configmap tenant-shared-a --type=merge '--patch={\"immutable\":true}'"
make kube ARGS='shared-data get configmap tenant-shared-a -o json' | jq '{immutable, data}'
```

Require `immutable: true`. Now create a version that data cannot write:

```bash
printf '%s\n' '{"message":"blocked-write"}' | \
  make api ARGS='control:shared PUT /tenants/shared-a/configuration'
make api ARGS='control:shared GET /tenants/shared-a' | jq '.data_config'
make kube ARGS='shared-data logs deployment/data-reconciler --tail=20'
make api ARGS='data:shared GET /tenants/shared-a'
make api ARGS='data:shared POST /tenants/shared-a/counter'
make api ARGS='data:shared GET /tenants/shared-b'
```

Wait for control to report `status: failed` with `last_report.type:
config_apply_failed` and `error_code: config_write_failed`. Data must keep the
message and version from before the PUT while its counter still increments.
`shared-b` stays `applied`: the reconciler does not rewrite ConfigMaps that are
already current.

Restore by deleting only that ConfigMap:

```bash
make kube ARGS='shared-data delete configmap tenant-shared-a'
make api ARGS='data:shared GET /tenants/shared-a'
make api ARGS='control:shared GET /tenants/shared-a' | jq '.data_config'
make kube ARGS='shared-data get configmap tenant-shared-a -o json' | jq '{immutable, data}'
```

Data may return 404 `tenant_config_not_applied` until the next data poll.
Then require `blocked-write` at the new version, control `applied`, no
`immutable` field, and the counter value observed before the restore.
The timeline keeps one `config_apply_failed` followed by `config_applied` for
that version.

### M. Interrupt the provisioner

A provisioner restart should mark its running operation `interrupted`, keep the
evidence, and never replay it. Run this after the other scenarios in this
section: the interrupted tenant cannot be retried or deleted, so only cleanup
removes it.

Accept a shared tenant and read its operation straight away:

```bash
printf '%s\n' '{"tenant_id":"shared-m","isolation":"shared","initial_message":"mike"}' | \
  make api ARGS='management POST /tenants' > "$NOTES/shared-m-request.json"
OP_M=$(jq -er '.operation_id' "$NOTES/shared-m-request.json") || exit 1
make api ARGS="management GET '/operations/$OP_M'" | jq '{status, stage}'
```

As soon as the status is `running`, restart the provisioner:

```bash
make kube ARGS='management rollout restart deployment/provisioner'
make kube ARGS='management rollout status deployment/provisioner --timeout=180s'
make kube ARGS='management logs deployment/provisioner --tail=40'
```

Reusing the existing shared pair takes only seconds. If the operation already
reports `succeeded`, the interruption was not exercised; repeat with another
tenant name rather than counting it.

```bash
make api ARGS="management GET '/operations/$OP_M'" | jq '{status, stage, error_code}'
make api ARGS='management GET /tenants/shared-m' | \
  jq '{provisioning_status, provisioning_stage, error_code, onboarding_status, events: [.timeline[].type]}'
printf '%s\n' '{"tenant_id":"shared-m","isolation":"shared","initial_message":"mike"}' | \
  make api ARGS='management POST /tenants'
```

Require status `interrupted`, `error_code: provisioner_restarted`, the stage it
reached, a `provisioning_interrupted` event and a `provisioner_ready` log line.
The operation must stay `interrupted` on later reads. The repeated POST returns
409: there is no retry or tenant-deletion API. Because the shared pair was
already available, control can still create the record, so `onboarding_status`
may become `ready` while provisioning stays `interrupted`.

Do not edit database status values or replay the operation. The supported
recovery is to inspect the evidence above and remove the whole demo with
[local cleanup](#5-clean-up-local).

## 4. Check datastore persistence

Replace datastore Pods while retaining their PVCs to separate process lifetime
from stored data.

After all faults are restored and operations completed, replace one datastore
at a time. Start with the shared pair:

```bash
make api ARGS='management GET /tenants/shared-a' > "$NOTES/m-persist.json"
make api ARGS='control:shared GET /tenants/shared-a' > "$NOTES/c-persist.json"
make api ARGS='data:shared GET /tenants/shared-a' > "$NOTES/d-persist.json"
make kube ARGS='shared-control get pod postgres-0 -o custom-columns=NAME:.metadata.name,UID:.metadata.uid'
make kube ARGS='shared-data get pod redis-0 -o custom-columns=NAME:.metadata.name,UID:.metadata.uid'
make kube ARGS='shared-control get pvc postgres-data -o custom-columns=NAME:.metadata.name,UID:.metadata.uid,VOLUME:.spec.volumeName'
make kube ARGS='shared-data get pvc redis-data -o custom-columns=NAME:.metadata.name,UID:.metadata.uid,VOLUME:.spec.volumeName'

make kube ARGS='shared-control rollout restart statefulset/postgres'
make kube ARGS='shared-control rollout status statefulset/postgres --timeout=180s'
make kube ARGS='shared-data rollout restart statefulset/redis'
make kube ARGS='shared-data rollout status statefulset/redis --timeout=180s'
```

Repeat the Pod/PVC reads. Require new Pod UIDs but unchanged PVC UIDs and volume
names. Do not delete PVCs. With no intervening state-changing API requests:

```bash
make api ARGS='management GET /tenants/shared-a' > "$NOTES/m-persist-after.json"
make api ARGS='control:shared GET /tenants/shared-a' > "$NOTES/c-persist-after.json"
make api ARGS='data:shared GET /tenants/shared-a' > "$NOTES/d-persist-after.json"
diff -u "$NOTES/m-persist.json" "$NOTES/m-persist-after.json"
diff -u "$NOTES/c-persist.json" "$NOTES/c-persist-after.json"
diff -u "$NOTES/d-persist.json" "$NOTES/d-persist-after.json"
```

Repeat the capture/replace/compare procedure for the remaining datastores:

| Slot | StatefulSet | API records to compare |
|---|---|---|
| management | postgres | Every management tenant record and operation state |
| isolated-1-control | postgres | `control:isolated-1` and `data:isolated-1`, tenant `isolated-c` |
| isolated-1-data | redis | `data:isolated-1`, tenant `isolated-c` |

Use `make kube ARGS='SLOT rollout restart statefulset/NAME'` followed by
`make kube ARGS='SLOT rollout status statefulset/NAME --timeout=180s'`, substituting
that row.
After management returns, also check `provisioner_ready`.
This is Pod-replacement persistence, not backup/restore, forced-crash durability,
or HA.

## 5. Clean up local

Cleanup destroys the selected demo's PostgreSQL, Redis and node-local volume
data. Restore faults and paused workloads, finish or inspect active provisioning,
and check that `.env` still selects local:

```bash
make show-config
make clean-plan
make clean CONFIRM_LOCAL=yes
make verify-clean
```

Review the plan before `make clean`. The cleaner quiesces management, removes
data/control applications through child Radius, deletes children through
management Radius, then removes management. It checks Docker, Kubernetes,
Radius and the actual Terraform state, not just resource names. Partial
topologies can be inspected without a saved export.

Checkpoint: `make verify-clean` returns `status: clean` for
`scope: owned-active-resources`. Images/build cache, the shared kind network,
unrelated containers, local files and global contexts remain untouched.
Verification needs no previous cleanup record.

An active or interrupted `management-bootstrap` Lease blocks cleanup; do not
delete or take it over to force progress. Missing or contradictory owners,
remaining faults and changed Terraform state also stop deletion. Inspect the
reported resources rather than directly deleting child kind clusters or
flushing network rules. Terraform state can contain child administrator
credentials; do not dump it into reports.

## Troubleshooting

| Where the run stops | What to inspect |
|---|---|
| Docker or bootstrap | Check Docker Desktop, the reserved ports and the selected `.env`. Do not switch to another runtime or cloud deployment. |
| Artifact inspection | Check the committed revision and prepared image set. Do not overwrite immutable tags or substitute uninspected dependencies. |
| Management deployment | Read management Pods and provisioner logs. An existing bootstrap Lease needs investigation, not automatic takeover. |
| Tenant stays pending | Read its operation and management provisioner logs. A failed/interrupted operation is not automatically replayed. |
| Control is ready but data is stale | Read the control data report, data-reconciler logs and tenant ConfigMap. Check for a paused reconciler or active fault. |
| Control reports data `failed` | Read `last_report.error_code` and data-reconciler logs. An immutable tenant ConfigMap left from section L blocks writes until that one ConfigMap is deleted. |
| Data returns 503 `local_counter_unavailable` | Redis is unreachable from the data API. Check whether section J left `statefulset/redis` at zero replicas. |
| Control returns 503 `database_unavailable` | Check whether section K left `statefulset/postgres` at zero replicas in that control slot. |
| SQL observation fails | Preserve credentials, the database and its metadata. Do not reset passwords or rerun initialization to bypass drift. |
| Cleanup refuses an owner or journal | Restore the fault and inspect the named owner/state. Do not delete a child directly or alter Terraform state to bypass the check. |

For example:

```bash
make kube ARGS='shared-data get pods'
make kube ARGS='shared-data logs deployment/data-reconciler --tail=40'
make kube ARGS='shared-data get configmap tenant-shared-a -o json'
```

## Automated checks

Use the harness instead of the manual scenarios on a prepared deployment with
no demo tenants. Keep the source revision used to build the inspected images:

```bash
make local-test CONFIRM_LOCAL=yes
```

This covers fresh admissions, reuse, isolation, configuration, counters,
authentication, timelines and both parent outages. Reports go to stdout and
exclude credentials. The [harness source](scripts/harness/test-e2e.py) contains
the individual checks.

After a manual run, existing tenants can be verified without claiming fresh
onboarding proof:

```bash
uv run --no-sync python scripts/harness/test-e2e.py --environment local \
  --mode verify-existing --execute
```

This still performs live mutations. If an interrupted first admission provides
a continuation handle, keep the same source and `.env` and use that exact handle:

```bash
uv run --no-sync python scripts/harness/test-e2e.py --environment local --mode all \
  --continue-first-from NAME@UID@RUN_ID --execute
```

Continuation is limited to that first-admission scope. It is not general replay
of failed provisioning. For an interrupted fault, use the
[journal restoration procedure](#interrupted-fault).

## Limits to keep in mind

Use synthetic data and a trusted operator. Management Radius has Docker daemon
authority; names and labels are not a hostile-tenant isolation boundary.
Demo keys are per plane instance: any holder of a pair's key can read and change
every tenant on that pair, and there are no per-tenant rate limits.
Management Secret encryption is checked, but encryption at rest for child
datastore Secrets, Terraform state and PVCs is not claimed.

Local PostgreSQL and Redis use explicit non-TLS transport on internal paths.
HTTP gateways bind only the reserved loopback ports. The singleton provisioner
has no HA scheduler or automatic replay, and there is no tenant migration or
deletion API. Pod-replacement persistence does not establish backup/restore,
forced-crash durability or disaster recovery.
