# Run local scenarios

Run the three-plane demo on Docker Desktop one step at a time. You will create
tenants, inspect each plane, change configuration, disconnect parent databases,
and restore the system. Each step explains what it demonstrates.

Run this guide from the repository root. The order is:

1. [Prepare the workspace](#1-prepare-the-workspace).
2. [Deploy local management](#2-deploy-local-management).
3. [Run the manual scenarios](#3-run-the-manual-scenarios).
4. [Check datastore persistence](#4-check-datastore-persistence).
5. [Clean up local](#5-clean-up-local).

This guide includes its own setup and shell controls; it does not require an
Azure deployment or another walkthrough. Do not run `make local-test` or
`test-e2e.py --mode all` alongside these requests: they create the same tenants
and change their state.

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

Management's separate provisioner asks Radius to create control/data clusters.
Each child has its own Radius installation. Shared tenants reuse a pair;
isolated tenants get a separate pair.

| Check | Meaning |
|---|---|
| Management `onboarding_status: ready` | Control created the tenant record |
| Management `provisioning_status: succeeded` | The provisioning operation finished |
| Control `data_config.status: applied` | Data applied the requested ConfigMap version |
| Data returns message, version, and counter | The request used local configuration and Redis |
| `/healthz` returns 200 | The API process is alive; this is not dependency readiness |

## 1. Prepare the workspace

You prepare the tools and isolate this run's state before creating resources.

Use one clean checkout per demo run. State contains credentials and cluster
identities, so do not clear `.state` to bypass an earlier attempt. If you need
a fresh checkout:

```bash
git worktree add --detach ../plane-demo-local HEAD &&
  cd ../plane-demo-local || exit 1
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

Expect a clean worktree. `make check-bicep` compiles the infrastructure and
generates the Radius extensions needed by the images. To explore the unit and
infrastructure tests separately, run `make check`; it creates no clusters.

### Select operator configuration

The root Makefile is the public command entrypoint. Operator and demo utilities
live under `scripts/operations/` and `scripts/harness/`.

```bash
make init ENV=local
make show-config
```

Initialization creates or replaces the checkout's private `.env`. It does not
create resources. `make show-config` reads that file without executing its
contents and redacts demo keys. Optional nonsecret arguments are forwarded with
`ARGS`, for example `make init ENV=local ARGS='--project demo --deployment team'`.
For demo keys, forward `--prompt-demo-key SLOT` or
`--demo-key-from-env SLOT=VARIABLE`, never the key value itself.

The deployment targets below still use their existing explicit configuration
and `.state` records. Initializing `.env` does not yet retarget those commands.

Local commands resolve Docker Desktop's `desktop-linux` context and pin its
local Unix socket. They do not change the global Docker context or use another
container runtime. Keep source unchanged during the demo; image and export
checks bind the deployment to the committed source.

Never run two local deployments concurrently: they use the same cluster names
and these loopback host ports:

| Slot | Gateway | Kubernetes API |
|---|---:|---:|
| management | 35490 | 35495 |
| shared-control | 35491 | 35496 |
| shared-data | 35492 | 35497 |
| isolated-1-control | 35493 | 35498 |
| isolated-1-data | 35494 | 35499 |

## 2. Deploy local management

You create the platform that accepts tenant requests and provisions the child
planes through local Recipes.

### Build and inspect the images

```bash
make local-prepare
make local-executor-build CONFIRM_LOCAL=yes
make local-executor-inspect CONFIRM_LOCAL=yes
make local-runtime-build CONFIRM_LOCAL=yes
make local-runtime-inspect CONFIRM_LOCAL=yes
```

The first image pair contains Radius execution tools; the second contains the
API and provisioner. Inspection checks contents rather than trusting a tag.
Existing immutable application image tags are not overwritten.

### Create management and install Radius

```bash
make local-bootstrap CONFIRM_LOCAL=yes
make local-install-radius CONFIRM_LOCAL=yes
make local-runtime-load CONFIRM_LOCAL=yes
```

Checkpoint: only management exists. Bootstrap verifies management Secret
encryption. Child clusters will be created by management Radius, not by
host-side `kind create`.

```bash
docker --context desktop-linux ps \
  --filter name=radplanes-local- --format 'table {{.Names}}\t{{.Status}}'
make local-setup CONFIRM_LOCAL=yes
make local-deploy-management CONFIRM_LOCAL=yes
```

Setup registers Recipes; deployment initializes management PostgreSQL and
starts the API/provisioner. Neither creates a child cluster. Use the shell
controls below to inspect management before submitting a tenant.

## 3. Run the manual scenarios

Run each scenario in order against the local management deployment.

### Load the shell controls

You obtain verified access and give each command an explicit plane and cluster.

In your main terminal, from the demo checkout:

```bash
export DEMO_ENV=local
export STATE=".state/$DEMO_ENV"
set -o pipefail
umask 077
mkdir -p "$STATE/manual"

api() { ./scripts/harness/api.sh "$DEMO_ENV" "$@"; }

export_state() {
  uv run --no-sync python scripts/harness/local/export-state.py --once
}

k() {
  local slot="$1"
  shift
  local file context namespace
  file=$(jq -er --arg s "$slot" '.targets[$s].kubeconfig' "$STATE/acceptance.json") || return
  context=$(jq -er --arg s "$slot" '.targets[$s].context' "$STATE/acceptance.json") || return
  namespace=$(jq -er --arg s "$slot" '.targets[$s].namespace' "$STATE/acceptance.json") || return
  kubectl --kubeconfig "$STATE/$file" --context "$context" \
    --namespace "$namespace" --request-timeout=30s "$@"
}

export_state
jq '{ready_for_onboarding, published_slots, pending_slots}' "$STATE/export-status.json"
k management get pods,pvc
k management logs deployment/provisioner --tail=20
api management GET /healthz
```

These shortcuts keep the commands below short:

| Command | What it does |
|---|---|
| `api management GET ...` | Sends one HTTP request using the exported URL/key |
| `k shared-data get ...` | Selects that slot's exported kubeconfig, context, and namespace |
| `export_state` | Refreshes verified access files; creates no tenants |

Export exit **3** means expected child endpoints are incomplete. At this point,
expect only management exported, `ready_for_onboarding: true`, bound
`postgres-data` and `provisioner-state` PVCs, `provisioner_ready`, and HTTP 200.
Other errors are blockers. Run export again after a new pair finishes provisioning.

Every successful observation must show HTTP 200. The API helper prints HTTP
status to stderr and JSON to stdout; non-2xx requests return nonzero.
Negative examples below deliberately return 401, 404, 409, or 503.
Do not mistake two matching error responses for unchanged application state.

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
  > "$STATE/manual/shared-a-request.json"
OP_A=$(jq -er '.operation_id' "$STATE/manual/shared-a-request.json")
api management GET "/operations/$OP_A"
```

Expect HTTP 202 with an operation ID. While the operation is `pending` or
`running`, inspect it and repeat these reads:

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
export_state
api control:shared GET /tenants/shared-a
api data:shared GET /tenants/shared-a
k shared-data get configmap tenant-shared-a -o json | jq .data
```

Expect three exported slots. Control should become `applied`; data should
return `alpha`, version 1, and counter 0. Match the `onboarding_id` across
the three APIs.

Save the logical placement for the reuse check. Management does not store or
return endpoint URLs; endpoint and cluster discovery use provider APIs.

```bash
api management GET /tenants/shared-a \
  | jq '{pair_id}' > "$STATE/manual/shared-pair.json"
```

#### Optional admission checks

You distinguish duplicate requests from temporary provisioning capacity limits.

Repeat the `shared-a` POST: expect 409 `duplicate_tenant` pointing to the
original status URL, without overwriting configuration or creating an operation.

While the first operation is still pending/running, a different tenant request
should receive 503 `provisioner_busy`:

```bash
api management POST /tenants \
  '{"tenant_id":"busy-check","isolation":"shared","initial_message":"not accepted"}'
api management GET /tenants/busy-check
```

Expect 503 and 404. Skip this check after the first operation finishes.
If you get 202, you submitted another real tenant; wait for it too and do not
claim that the busy check passed.

### B. Reuse the pair while data reconciliation is paused

You prove that shared tenants reuse infrastructure and that management readiness
does not depend on the data reconciler finishing its work.

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
  | jq '{pair_id}' > "$STATE/manual/shared-b-pair.json"
diff -u "$STATE/manual/shared-pair.json" "$STATE/manual/shared-b-pair.json"
export_state
jq '.targets | keys' "$STATE/acceptance.json"
```

Expect no diff and still three slots. The new shared tenant added records,
not another cluster pair.

### C. Provision an isolated tenant

You compare shared placement with a dedicated cluster pair and verify that each
pair serves only its assigned records.

```bash
api management POST /tenants \
  '{"tenant_id":"isolated-c","isolation":"isolated","initial_message":"charlie"}'
api management GET /tenants/isolated-c
k management logs deployment/provisioner --tail=20
```

Wait for provisioning success and management readiness:

```bash
export_state
api control:isolated-1 GET /tenants/isolated-c
api data:isolated-1 GET /tenants/isolated-c
jq -r '.targets | to_entries[] |
  [.key, .value.cluster_id, .value.cluster_uid] | @tsv' "$STATE/acceptance.json"
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

You change application behavior through control without redeploying data, while
checking that tenant counters stay independent.

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

You verify that unchanged polling preserves control-owned updates, that reports
can be followed in order, and that the API has limited access.

Capture a baseline after updates have applied:

```bash
POLL_FROM=$(uv run --no-sync python -c \
  'from datetime import UTC, datetime; print(datetime.now(UTC).isoformat())')
api management GET /tenants/shared-a > "$STATE/manual/m-before.json"
api control:shared GET /tenants/shared-a > "$STATE/manual/c-before.json"
api data:shared GET /tenants/shared-a > "$STATE/manual/d-before.json"
sleep 15
k shared-control logs deployment/control-reconciler \
  --since-time="$POLL_FROM" --timestamps=true --tail=20
api management GET /tenants/shared-a > "$STATE/manual/m-after.json"
api control:shared GET /tenants/shared-a > "$STATE/manual/c-after.json"
api data:shared GET /tenants/shared-a > "$STATE/manual/d-after.json"
diff -u "$STATE/manual/m-before.json" "$STATE/manual/m-after.json"
diff -u "$STATE/manual/c-before.json" "$STATE/manual/c-after.json"
diff -u "$STATE/manual/d-before.json" "$STATE/manual/d-after.json"
```

Require a successful `control_poll` after `$POLL_FROM`, with `succeeded` greater
than zero and `failed=0`, plus unchanged snapshots. That shows control retained
its updates while polling management. No successful poll means no proof yet.

Read a timeline in small pages:

```bash
api control:shared GET '/tenants/shared-a?limit=2' > "$STATE/manual/page.json"
jq '{timeline, next_after_event_id}' "$STATE/manual/page.json"
CURSOR=$(jq -r '.next_after_event_id' "$STATE/manual/page.json")
if [ "$CURSOR" != null ]; then
  api control:shared GET "/tenants/shared-a?limit=2&after_event_id=$CURSOR"
fi
```

Continue with each returned cursor until `null`. IDs increase but need not be
consecutive. Management reports control-record creation; control reports
configuration changes and data application.

Check rejected requests:

```bash
API_URL=$(jq -er '.management.url' "$STATE/endpoints.json")
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' "$API_URL/tenants/shared-a"
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' \
  -H 'X-Demo-Key: wrong' "$API_URL/tenants/shared-a"
api management POST /tenants \
  '{"tenant_id":"Bad ID","isolation":"shared","initial_message":"invalid"}'
api management POST /tenants \
  '{"tenant_id":"shared-a","isolation":"shared","initial_message":"must not replace alpha"}'
```

Expect 401, 401, 422, and 409. The duplicate must not reset control's configuration
or the counter. Repeat the curl checks for the child API URLs listed by:

```bash
jq -r '.management.url, (.pairs[] | .control.url, .data.url)' "$STATE/endpoints.json"
```

Inspect the data API identity without displaying Secrets:

```bash
k shared-data get deployment data-api \
  -o jsonpath='{.spec.template.spec.serviceAccountName}{"\n"}'
DATA_NS=$(jq -er '.targets["shared-data"].namespace' "$STATE/acceptance.json")
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

You show that an existing control/data pair can keep changing configuration and
serving requests without reaching management's database.

Finish onboarding and export all five slots before faults. Open a second
terminal in the same checkout and set the same `DEMO_ENV` and `STATE`.
Do not stop an API to simulate a database outage.
The local fault helper uses the reconciler Pod's network namespace because
kind's default CNI does not enforce NetworkPolicy.

In the second terminal:

```bash
uv run --no-sync python scripts/harness/fault-parent-link.py \
  --config "$STATE/acceptance.json" \
  --slot shared-control --component control-reconciler --duration 300 --execute
```

This blocks shared control's connection to management PostgreSQL, not the
management API or the isolated pair. The helper verifies the target and both
fresh/existing database connections, holds the fault, and restores it.

In the main terminal:

```bash
ls -t "$STATE"/evidence/fault-*.json
```

Use the exact new filename:

```bash
FAULT_FILE="$STATE/evidence/fault-REPLACE_WITH_NEW_ID.json"
jq '{slot, component, outcome, blocked_at, restored}' "$FAULT_FILE"
```

Require `shared-control`, `control-reconciler`, and `blocked_verified` before
continuing. While the helper holds the fault:

```bash
api management GET /tenants/shared-a > "$STATE/manual/m-blocked-before.json"
api control:shared PUT /tenants/shared-a/configuration '{"message":"without-management"}'
api data:shared GET /tenants/shared-a
api data:shared POST /tenants/shared-a/counter
```

The new message should reach data through control's own database. Keep reading
and incrementing for at least 60 seconds, then:

```bash
api management GET /tenants/shared-a > "$STATE/manual/m-blocked-after.json"
diff -u "$STATE/manual/m-blocked-before.json" "$STATE/manual/m-blocked-after.json"
```

Expect no new management reports during blockage. After the fault terminal
finishes:

```bash
jq '{outcome, restored, physical_restored, restoration_started_at, restored_at}' "$FAULT_FILE"
RESTORE_FROM=$(jq -er '.restoration_started_at' "$FAULT_FILE")
k shared-control logs deployment/control-reconciler \
  --since-time="$RESTORE_FROM" --timestamps=true --tail=20
```

Require `verified_and_restored`, both restoration flags true, and a successful
control poll after restoration. Recovery must not add a duplicate
`control_record_created` event.

### G. Block control, queue updates, and restart data

You show that data can serve its last applied state even after an API restart,
then catch up to the newest configuration when control becomes reachable.

Start only after the preceding fault is restored. Save the current data response:

```bash
api data:shared GET /tenants/shared-a > "$STATE/manual/outage-baseline.json"
jq . "$STATE/manual/outage-baseline.json"
```

In the fault terminal:

```bash
uv run --no-sync python scripts/harness/fault-parent-link.py \
  --config "$STATE/acceptance.json" \
  --slot shared-data --component data-reconciler --duration 300 --execute
```

Select the new journal as `FAULT_FILE`. Require `shared-data`, `data-reconciler`,
and `blocked_verified`. Now data cannot read control PostgreSQL.

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
Do not replace the data reconciler whose network namespace owns the local fault.

After restoration:

```bash
jq '{outcome, restored, physical_restored}' "$FAULT_FILE"
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
uv run --no-sync python scripts/harness/fault-parent-link.py \
  --config "$STATE/acceptance.json" --restore "$FAULT_FILE" --execute
jq '{outcome, restored, physical_restored}' "$FAULT_FILE"
```

Do not use `kill -9`, replace the faulted reconciler, or delete its cluster to
clear a fault. Do not proceed until the original link is confirmed restored.

## 4. Check datastore persistence

You separate Pod lifetime from stored data by replacing database and Redis Pods
while retaining their storage and application state.

After all faults are restored and operations completed, replace one datastore
at a time. Start with the shared pair:

```bash
api management GET /tenants/shared-a > "$STATE/manual/m-persist.json"
api control:shared GET /tenants/shared-a > "$STATE/manual/c-persist.json"
api data:shared GET /tenants/shared-a > "$STATE/manual/d-persist.json"
k shared-control get pod postgres-0 -o custom-columns=NAME:.metadata.name,UID:.metadata.uid
k shared-data get pod redis-0 -o custom-columns=NAME:.metadata.name,UID:.metadata.uid
k shared-control get pvc postgres-data \
  -o custom-columns=NAME:.metadata.name,UID:.metadata.uid,VOLUME:.spec.volumeName
k shared-data get pvc redis-data \
  -o custom-columns=NAME:.metadata.name,UID:.metadata.uid,VOLUME:.spec.volumeName

k shared-control rollout restart statefulset/postgres
k shared-control rollout status statefulset/postgres --timeout=180s
k shared-data rollout restart statefulset/redis
k shared-data rollout status statefulset/redis --timeout=180s
```

Repeat the Pod/PVC reads. Require new Pod UIDs but unchanged PVC UIDs and volume
names. Do not delete PVCs. With no intervening state-changing API requests:

```bash
api management GET /tenants/shared-a > "$STATE/manual/m-persist-after.json"
api control:shared GET /tenants/shared-a > "$STATE/manual/c-persist-after.json"
api data:shared GET /tenants/shared-a > "$STATE/manual/d-persist-after.json"
diff -u "$STATE/manual/m-persist.json" "$STATE/manual/m-persist-after.json"
diff -u "$STATE/manual/c-persist.json" "$STATE/manual/c-persist-after.json"
diff -u "$STATE/manual/d-persist.json" "$STATE/manual/d-persist-after.json"
```

Repeat the capture/replace/compare procedure for the remaining datastores:

| Slot | StatefulSet | API records to compare |
|---|---|---|
| management | postgres | All three management tenant records and operation states |
| isolated-1-control | postgres | `control:isolated-1` and `data:isolated-1`, tenant `isolated-c` |
| isolated-1-data | redis | `data:isolated-1`, tenant `isolated-c` |

Use `k SLOT rollout restart statefulset/NAME` followed by
`k SLOT rollout status statefulset/NAME --timeout=180s`, substituting that row.
After management returns, also check `provisioner_ready`.
This is Pod-replacement persistence, not backup/restore, forced-crash durability,
or HA.

## 5. Clean up local

You remove the local deployment through Radius and independently check that the
owned clusters are gone.

Restore all faults and paused workloads, then:

```bash
export_state
jq '.targets | keys' "$STATE/acceptance.json"
make local-clean-plan
make local-clean CONFIRM_LOCAL=yes
```

The normal local cleaner requires all five exported slots. A partial/failed
topology needs explicit recovery review, not direct child-cluster deletion.
Use the exact cleanup record printed by the command:

```bash
make local-verify LOCAL_CLEANUP_RECORD=.state/local/evidence/cleanup-REPLACE_WITH_PRINTED_ID.json
```

Require `resources_removed`. Images/cache, the shared kind network, and protected
state are retained intentionally. See [local cleanup](docs/local-cleanup.md).

## References for the walkthrough

| Topic | Source |
|---|---|
| How the planes connect | [Architecture](docs/architecture.md), [API/database contracts](docs/contracts.md) |
| Runtime | `src/plane_demo/{management,control,data,shared,setup}`, `sql/` |
| Infrastructure | `infra/radius/apps/` declares planes; `types/` defines APIs; `recipes/` implements them; `environments/` selects Recipes |
| Administration | `scripts/operations/`, [local provider](docs/local-provider.md) |
| Harness pieces to inspect | `scripts/harness/api.py`, exporters, [`Runner.scenario`, `management_outage`, `control_outage`](scripts/harness/test-e2e.py), [harness reference](tests/harness/README.md) |
| Results and limits | [Findings](FINDINGS.md), [decisions](DECISIONS.md), [limitations](docs/limitations.md) |

Use synthetic data. Shared demo keys keep the API examples simple; production
tenant authentication is outside this POC. Keep credentials separate from source.
