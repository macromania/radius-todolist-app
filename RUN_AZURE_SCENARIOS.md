# Run Azure scenarios

Run this demo one step at a time. You will create tenants, inspect each plane,
change configuration, disconnect parent databases, and restore the system.
Use the checkpoints as stopping points when learning or presenting to a team.
The focus is Radius provisioning, the boundaries between planes, and their
behavior during configuration changes and outages.

The state-removal refactor still needs a fresh end-to-end run. The checkpoints
below are requirements to verify, not claims that the current revision passed.
See [FINDINGS.md](FINDINGS.md) for revision-specific results.

Run this guide from the repository root. The order is:

1. [Prepare the workspace](#1-prepare-the-workspace).
2. [Deploy Azure management](#2-deploy-azure-management).
3. [Run the manual scenarios](#3-run-the-manual-scenarios).
4. [Clean up Azure](#4-clean-up-azure).

You will use individual operations and a few harness utilities, not the
all-in-one acceptance runner. Do not run `make test-e2e` or `test-e2e.py --mode all`
alongside the manual demo: they create the same tenants and change their state.
For the separate Docker Desktop walkthrough, use
[RUN_LOCAL_SCENARIOS.md](RUN_LOCAL_SCENARIOS.md).

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

You prepare the tools and choose the deployment identity before creating resources.
Use committed source. A fresh checkout does not need a previous `.state` folder:

```bash
git worktree add --detach ../plane-demo-azure HEAD &&
  cd ../plane-demo-azure || exit 1
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

Azure also needs `az`, `helm`, `kubelogin`, and a login with the required access
to the project's configured subscription. The operator must be able to reach
the AKS APIs. Check [Azure prerequisites](docs/azure-infrastructure.md) before
creating resources.

```bash
uv sync --locked
git status --short
make check-bicep
```

Expect a clean worktree. `make check-bicep` compiles the infrastructure and
generates the Radius extensions needed by the images. To explore the unit and
infrastructure tests separately, run `make check`; it creates no clusters.

### Select operator configuration

The root Makefile is the public command entrypoint. Operator and demo utilities
live under `scripts/operations/` and `scripts/harness/`.

```bash
make init ENV=azure
make show-config
```

Initialization creates or replaces the checkout's private `.env`. It may read
the active Azure subscription as a suggestion but does not deploy resources.
Supply nonsecret options through `ARGS` to avoid the account lookup:

```bash
make init ENV=azure \
  ARGS='--subscription <subscription-uuid> --location centralus --project demo --deployment team'
```

`make show-config` reads `.env` without executing its contents and redacts demo
keys. For demo keys, forward `--prompt-demo-key SLOT` or
`--demo-key-from-env SLOT=VARIABLE`, never the key value itself.

The commands below use this `.env`. `ENV` selects the environment only when
initializing it; it does not override an existing selection. Azure credentials
belong in the shared Key Vault. Access, endpoints and deployment outputs are
queried from current APIs. Do not copy an old endpoint or credential inventory.

Keep source unchanged during the demo. Image and export checks bind the
deployment to the committed source.

## 2. Deploy Azure management

You create the platform that accepts tenant requests and provisions child planes.

The stages are foundation, build, then management deployment. Artifact inspection
and deployment-input assembly are part of the normal commands, not manual file
handoffs.

### Create the foundation

You create management AKS, the Azure foundation and management Radius.
Bootstrap checks the selected account and existing resource ownership.

```bash
make bootstrap CONFIRM_AZURE=yes
```

Checkpoint: bootstrap completed and management AKS and Radius exist. There are
no tenant clusters or management application yet. Azure owns the deployment
outputs; no `bootstrap.outputs.json` file is required on the workstation.

### Build and inspect artifacts

You publish the Recipes and build the two runtime images. The normal build
checks actual filesystem contents and trusted build provenance, not only tags.

```bash
make build CONFIRM_AZURE=yes
make inspect-build
```

Require successful artifact inspection. The API image excludes provider code,
administrative tools and deployment credentials. The privileged provisioner is
separate. Recipe publication verifies the registry's repository-permission
boundary. See [provisioning](docs/provisioning.md) for these contracts.

### Deploy management and wait for completion

You start management's API, database, and provisioner, then confirm the Job
finished rather than treating submission as success.

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

Checkpoint: management's API, PostgreSQL, and provisioner are ready.
There are no tenant clusters yet. Deployment already registers management's
Radius resources; a separate `register-radius` command is unnecessary.

## 3. Run the manual scenarios

Run each scenario in order against the Azure management deployment.

### Load the shell controls

You obtain verified access and give each command an explicit plane and cluster.

In your main terminal, from the demo checkout:

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
No command discovers infrastructure or credentials from those notes.

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
  > "$NOTES/shared-a-request.json"
OP_A=$(jq -er '.operation_id' "$NOTES/shared-a-request.json")
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

You show that an existing control/data pair can keep changing configuration and
serving requests without reaching management's database.

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

You show that data can serve its last applied state even after an API restart,
then catch up to the newest configuration when control becomes reachable.

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

You prove the ownership model in reverse: children and their applications are
removed before their management foundation.

Save your observations under `$NOTES`. Restore paused workloads and all
faults; no helper or provisioning operation may still be running.

```bash
make clean-plan
make clean CONFIRM_AZURE=yes
make verify-clean
```

Review the plan before executing it. Cleanup reads current Azure, Radius and
Kubernetes owners. It does not require an export, local cleanup record, or live
management database. It removes applications and child clusters through Radius
before deleting the foundation. Do not substitute direct AKS deletion.
See [Azure cleanup](docs/cleanup.md).

Keep soft-deleted vault retention and unrelated resources distinct from active
deployment removal. An externally selected Key Vault and its retained objects
are reported, not deleted or purged.

## References for the walkthrough

| Topic | Source |
|---|---|
| How the planes connect | [Architecture](docs/architecture.md), [API/database contracts](docs/contracts.md) |
| How Azure child clusters are provisioned | [Provisioning run path](docs/provisioning.md), [Azure infrastructure](docs/azure-infrastructure.md) |
| Runtime | `src/plane_demo/{management,control,data,shared,setup}`, `sql/` |
| Infrastructure | `infra/radius/apps/` declares planes; `types/` defines APIs; `recipes/` implements them; `environments/` selects Recipes |
| Administration | `scripts/operations/`, [Azure operations](docs/azure.md) |
| Harness pieces to inspect | `scripts/harness/api.py`, exporters, [`Runner.scenario`, `management_outage`, `control_outage`](scripts/harness/test-e2e.py), [harness reference](tests/harness/README.md) |
| Results and limits | [Findings](FINDINGS.md), [decisions](DECISIONS.md), [limitations](docs/limitations.md) |

Use synthetic data. Shared demo keys keep the API examples simple; production
tenant authentication is outside this POC. Keep credentials separate from source.
