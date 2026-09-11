# Opt-in three-plane acceptance

These scripts perform **real HTTP requests and Kubernetes mutations** only when
called with `--execute`. The normal unit/integration suites never invoke them.
Live Azure and local results, their source revisions, and teardown proofs are
recorded in [FINDINGS.md](../../FINDINGS.md); tests alone are not that evidence.
The checkout containing each script is the immutable path authority: project
identity is fixed to `radplanes`, and relative configuration, state, evidence,
Git commands, and source reads resolve from that checkout, never the caller's
working directory. A configuration cannot select another project or external
state directory.

```sh
uv run python -m unittest discover -s tests/harness -p 'test_*.py'

# After parent deployment/integration gates, reviewed commits, and state export:
uv run python harness/test-e2e.py --config .state/azure/acceptance.json --mode all --execute
```

Modes are `scenario`, `outages`, `all` (default), and `verify-existing`.
Evidence includes the mode; `scenario` alone is not outage acceptance, and
`verify-existing` is not fresh onboarding proof. The application/SQL/Radius application
and acceptance-script worktree must be clean, and deployed source hashes must
match it. Results include UTC times, source commit, image IDs, full parent API
timelines, versions/counters, cluster UIDs, and authenticated datastore endpoint
identities. They never include demo keys, DSNs, full environments, credentials
files, or raw subprocess errors. Evidence is written under the configured
environment's `evidence/` directory with mode `0600`.

Provenance covers both management workloads, not just its API. The provisioner
check includes coordinator/provider Python, copied administrative scripts, SQL,
and Radius application/type sources. On Azure, every checked workload must use its
configured digest-pinned image reference and expose a running image digest.
Those references/digests and exact source hashes are recorded separately,
including the replacement data API pod after restart.
Redis identity evidence reports `tls: true` only for the connected
`ssl.SSLSocket` with a negotiated protocol version, not a connection option or
redis-py attribute. PING runs and must succeed even under optimized Python.
The Redis output remains limited to host, port, peer address, and TLS status.

## Local five-cluster acceptance

The local implementation uses the **same** `Runner.scenario`, configuration,
counter/auth, timeline, idempotency, and outage checks as Azure. There is no
local shortcut around the second shared tenant's paused-data-reconciler
admission, four distinct child cluster IDs, five live cluster UIDs, either
outage, or the absolute 30-second recovery deadline. Offline command-path tests
are not a claim that the full local scenario passed. The parent must review,
deploy, export, and run the live checks; see [the local contract](../../docs/local.md).

After committed, inspected native runtime images and management deployment:

```sh
# Read-only: exit 3 means the expected child state is incomplete, not failure.
uv run python harness/local/export-state.py --once

# Keep this foreground process, or a parent-owned attached async process, running
# through admissions. Wait for export-status.json ready_for_onboarding: true.
uv run python harness/local/export-state.py --watch --timeout 10800

# Mutations and API requests require the explicit execution switch.
uv run python harness/test-e2e.py --config .state/local/acceptance.json --mode all --execute

./harness/api.sh local management GET /tenants/shared-a
./harness/api.sh local control:shared GET /tenants/shared-a
./harness/api.sh local data:isolated-1 POST /tenants/isolated-c/counter
```

The only provisioning input is the protected `.state/local/provisioning.json`
written for `LocalConfig`: version 1, provider `local`, project `radplanes`,
five fixed allocations, four reviewed Recipe references, native image references
and `imageId` values, and the management cluster's UID, decoded-PEM CA SHA-256,
internal node address, and Kubernetes Service address. No tenant supplies these
values. The exporter reads `.state/local/runtime-images.json`, requires matching
committed source, image IDs, inspected contents, and timestamps, and compares the
exact reviewed source file list with **running Pod files**. It also maps each
Pod's reported image ID through `crictl inspecti` on its verified node to the
reviewed Docker configuration digest. If CRI reports a manifest digest instead,
the exporter reads the node's containerd content using `ctr --namespace k8s.io`,
hashes the raw manifest and configuration bytes (including an index's unique
reviewed Linux/native-platform manifest when present), and requires that exact manifest
to reference the reviewed configuration digest. Neither a different digest nor
a matching tag can skip the running-source check. Acceptance repeats that check, including
the replacement data API Pod after restart. Local tag/ID support does not relax
Azure's registry digest-pinned reference requirement.

Slots are `management`, `shared-control`, `shared-data`, `isolated-1-control`,
and `isolated-1-data`. Cluster IDs are `kind://radplanes-local-<slot>`; contexts
are `radplanes-local-<slot>`, never `kind-` prefixed. Gateways use only
`http://127.0.0.1:35490` through `:35494` in that order. Host Kubernetes API
ports are 35495–35499, with verified CA transport. Namespaces are
`radplanes-local-<slot>-<role>`. Labels and service accounts retain their
existing `plane-demo/project=radplanes` and component names. The data API alone
uses the dedicated `data-api-runtime` account, not Radius's generated Secret
reader. Acceptance checks the mounted identity's actual parent-Secret GET and
Secret LIST return authenticated 403 responses. Broad and resource-name-scoped
authorization reviews permit only ConfigMap get and reject token minting,
ConfigMap writes/watch, and Secret access. The checks repeat after API restart.

Every pass verifies the recorded management Docker ID and encryption proof,
each exact kind node name/label/ID, its unique private address on the `kind`
network, and the live Kubernetes/namespace UIDs. Child credentials come only
from each named, Radius-owned access Secret in `radplanes-local-access`, after
checking the cluster resource's `bootstrapAccessRef`, Secret slot label, and
resource-ID annotation. Only the `kubeconfig` field and ownership metadata are
read, never a Secret list or whole Secret. Its internal address, explicit
certificate name, CA, and certificate/key profile are checked before changing
only the endpoint to the reserved host loopback port. An existing protected
export must match exactly before it is used; it is never rewritten to adopt a
changed cluster or credential. The bootstrap kubeconfig is not modified.

Only `DEMO_KEY` from each named `ROLE-api-runtime` Secret is copied, into a
mode-0600 key file. Keys, kubeconfigs, and endpoint generations are immutable.
Each progressively published `acceptance.json` points to its own immutable
endpoint generation; `endpoints.json` remains the API client's atomic projection.
A single-writer lock protects publication. Missing children and unfinished
rollouts may remain pending; authentication, changed ownership, transport,
image/source, and key failures do not become an empty inventory or fake readiness.
The exporter never edits live resources or global CLI/HOME configurations.

Local PostgreSQL uses its owning cluster's verified **node private IPv4 address
on port 31543**, with no host port mapping. A control API uses its own control
node; its reconciler's management connection uses the management node. Neither
connection uses a PostgreSQL DNS/5432 alias. This local transport intentionally
uses `sslmode=disable`; Azure PostgreSQL verification is unchanged. A data API
connects to its own namespaced Redis DNS endpoint on port 6379 with `tls: false`.
The acceptance probe performs a real authenticated PING, rejects a wrong
password, and records the local non-TLS result explicitly. Azure still requires
a negotiated TLS socket.

### Local fault mechanism and recovery

kind's default CNI does not enforce NetworkPolicy. Local fault proof therefore
uses `docker exec` with the pinned kind node's existing `crictl`, `stat`, `bash`,
`nsenter`, and `iptables` tools. No live package installation or additional Pod
image is used. The operator tool has Docker daemon access; **runtime API and
provisioner containers receive none**.

Before each mutation, the harness verifies the exact node, cluster/namespace
UIDs, deployment/ReplicaSet/Pod ownership, component service account, CRI
container and sandbox Pod UID, PID, and network namespace inode. It opens a
descriptor for that namespace and refuses the node's network namespace. The
only rule inserted is an `OUTPUT` TCP `DROP` to the exact parent IPv4 `/32` and
port 31543, with a unique run comment. It changes no host/node/global networking,
policy defaults, unrelated rules, or other Pods. The protected journal is written
**before** insertion. There is no ephemeral container or cleanup Pod.

The existing interactive probe opens the real parent DSN from the reconciler
before insertion and must prove both that connection and a fresh connection
fail, while the reconciler's local database/Kubernetes prerequisite still works.
The normal outage checks then send ten exact counter increments over at least
60 seconds. During the control-parent fault, two control API updates remain
pending, the data API restarts without its parent, and only the newest version
may apply after recovery.

`finally` removes only the exact recorded rule, checks the original `OUTPUT`
rule hash, and proves fresh parent connectivity. The same absolute 30-second
budget includes removal, probe cleanup, and reporting/latest-version convergence.
A replaced Pod, changed namespace, or failed removal is an explicit failed
restoration, not permission to touch a replacement. Preserve the evidence and
investigate; do not flush rules or reset clusters. After an interruption, only
the recorded, still-owned rule may be restored:

```sh
uv run python harness/fault-parent-link.py --config .state/local/acceptance.json \
  --restore .state/local/evidence/RECORDED-FAULT.json --execute
```

Standalone local faults route through the same command (or
`harness/local/fault-parent-link.py`) using `--slot shared-control
--component control-reconciler --duration 60 --execute`. Restore-only success
is not acceptance. The Azure-only first-admission continuation remains
Azure-only; local execution must not reinterpret historical Azure evidence.

Cleanup tooling can call `assert_restored_for_cleanup(commands.run, configuration)`
from `harness/local/fault-parent-link.py`, using that module's
`base.Configuration`. This read-only helper requires a complete five-target
export, validates protected fault journals, and reuses the exact node/Pod/CRI
network-namespace guards. Attempted faults must have successful restoration
markers and matching recorded/current Pod, sandbox, and original/restored/live
`OUTPUT` hashes. Every current reconciler must be free of owned fault markers.
Historical failure fields may remain after a successful explicit restoration;
they do not override current restoration proof. Changed journals invalidate the
check. The helper returns non-secret journal hashes and live identity/rule
observations for the cleanup owner's journal; it writes nothing and never
inserts or removes a rule.

## Operator state contract

For in-cluster execution when the workstation cannot reach the AKS API:

```sh
CONFIRM_AZURE=yes uv run python harness/run-azure.py --images-inspected --mode all --execute
```

This submits, but does not await, a `demo-acceptance-*` Job in management through
AKS Run Command. It requires a clean committed checkout and matching
`images.json` references with `content_verified: true`. The verified provisioner
image supplies tools; a checked Git bundle supplies harness source. No harness
code is added to runtime images. The dedicated `harness` service account
authenticates as `foundation.harnessIdentity` through workload federation.
Bootstrap grants it project-scoped metadata reads and AKS cluster access for
Secret export, workload inspection, and Cilium fault policies. It is trusted
operator tooling, not runtime or tenant authorization. Coordinator permissions
are unchanged; the launcher refuses a missing or reused coordinator identity.
Only the separate `harness-state`
PVC retains exported keys, kubeconfigs, evidence, and `azure/harness/termination.json`.
The launcher never mounts operator/provisioner state. `--name` accepts a bounded
`demo-acceptance` suffix; `--config` defaults to `.state/azure/provisioning.json`.
All four modes are available through this launcher. The exporter must support the
requested 10,800-second watch timeout before this Job can pass.

### Initial export discovery versus convergence

After a newly provisioned pair's onboarding operation succeeds, `scenario`/`all`
wait up to **300 seconds total per pair** for its control and data API exports.
The same discovery wait runs for the first pair in a first-tenant continuation
and at `verify-existing` startup, after current operations succeed. This covers
slow exporter scans; it neither provisions resources nor retries failed API
requests. Only unpublished endpoints/missing exported state are awaited.
Authentication, ownership, configuration, and other errors fail immediately.
Discovery exhaustion is `initial_export_discovery_timeout`.

Only after discovery do the existing **30-second applied-configuration checks**
start. Export discovery does not count as data convergence time. General client
waits and `outages` mode are unchanged, and scoped-outage recovery still uses its
original absolute 30-second deadline without a new discovery grace period.
No configurable timeout or general retry mechanism is added.

### Verify already-existing tenants

```sh
uv run python harness/test-e2e.py --config .state/azure/acceptance.json \
  --mode verify-existing --execute

# Through the existing in-cluster launcher, with the usual deployment guards:
CONFIRM_AZURE=yes uv run python harness/run-azure.py \
  --images-inspected --mode verify-existing --execute
```

This explicit mode writes a new record with `mode: verify-existing`,
`scope: existing-tenants-only`, and `admission_checks_performed: false`.
It cannot be combined with `--continue-first-from` and imports no prior proof.
Existing failed records remain failed and unchanged.

All three configured tenants must exist, become management-ready, and have
successful current operations. Their exports and current applied configurations
are checked before mutations. The first two must share pair `shared` and control/
data URLs; the isolated tenant must use another pair. The normal provenance and
isolation checks still require four distinct child ARM cluster IDs, five distinct
live cluster UIDs, and different PostgreSQL/Redis endpoints across the pairs.
The same configuration updates, exact counter increments, auth checks, timelines,
poll idempotency, both scoped outages, and final timelines then run.

The current configuration versions and counter values are the starting baseline;
they are not reset. This mode makes no `POST /tenants` requests and does not pause
the data reconciler for second-tenant admission. It proves neither historical
HTTP 202/409/503 behavior nor historical resource reuse/no-resource-creation.
Those proofs remain in their original evidence scope. The launcher still requires
fresh timestamps, clean current source, and the explicit existing-only scope in
the final evidence before reporting this mode passed.

### Continue only a previously admitted first tenant

If the observer/exporter failed after the first tenant's admission, or initial
read-only verification failed before the second tenant, either harness script
accepts the explicit option:

```sh
--continue-first-from .state/azure/evidence/acceptance-<32-lowercase-hex-run-id>.json
```

This is **not workflow resume or provisioning recovery**. Only `scenario` and
`all` accept it, and the prior mode must match. The prior protected file must
belong to this project's Azure state, describe a failed version-1 run with a
clean, timestamp-verified source commit, and contain this exact ordered prefix:
`acceptance_started`, management API `workload_image`, provisioner
`workload_image`, and one `tenant_accepted` for the configured first tenant.
That admission must retain its original operation UUID, HTTP 202, and
`busy_verified: true`. The prefix may stand alone or have exactly six further
read-only events: `management_ready` for that tenant/shared pair/same operation,
`data_applied` for that tenant at version 1, then `workload_image` for shared
control API, control reconciler, data API, and data reconciler in that order.
Partial tails, other progress (including any reconciler pause, later admission,
update, or fault), chained continuations, foreign IDs, altered admission shapes,
oversized/nonprivate files, and symlinked evidence are refused.

The harness requires both remaining tenants to return 404. It reads the first
tenant, checks the same shared-pair operation is not failed/interrupted, and
waits for the existing readiness and operation completion checks. It sends
**no first-tenant onboarding POST**, including duplicate checks or retries.
The original run ID determines the initial message; version 1 and that exact
message must still be applied. Prior read-only events skip nothing: readiness,
operation completion, applied configuration, and full first-workload source and
datastore identity checks all run again under the current code. Changed
configuration fails before admitting the second tenant.
All later admissions, paused-reconciler/inventory checks,
configuration/counter checks, provenance, timelines, and selected outages run
normally. There are no resets or skips of later scenario steps.

The failed file remains byte-for-byte unchanged. A new run has its own fresh
timestamps and current clean source metadata; `continued_first_from` separately
records the predecessor's relative path, SHA-256, run/source metadata, timestamps,
and actual earlier admission. The new event list records `first_tenant_continued`,
not a fabricated first HTTP 202. Protected evidence is trusted operator input,
not a cryptographically signed attestation.

For `run-azure.py`, the path refers to the retained **harness-state PVC**, not a
required local file. Only that bounded relative argument is forwarded through
bootstrap to the existing in-cluster harness; old evidence is not bundled in a
ConfigMap, and operator/provisioner volumes remain unmounted. Current source,
image inspection, and final fresh-evidence verification requirements are unchanged.
Offline tests cover this path; they do not establish live acceptance success.

Generate state from the existing provisioning configuration; no manual endpoint,
key, namespace UID, or component-name editing is required:

```sh
# One read-only pass. Exit 3 means expected child state is still incomplete.
uv run python harness/export-state.py --once

# Run in the foreground, or as a parent-owned attached async command, during onboarding.
uv run python harness/export-state.py --watch --timeout 7200
```

The default input is `.state/azure/provisioning.json`; `--config` accepts another
provisioning file beneath `.state/azure`, including nested directories.
Watch mode sleeps five seconds between passes,
stops when all three configured showcase tenants are ready and their planes are
exported, and fails on timeout. `export-status.json` is the atomic heartbeat:
wait for `ready_for_onboarding: true` before starting the scenario. Progress
lists ready/published/pending slots, the operator PID, and UTC observation time.
`--once` returns 0 only for a complete export, 3 while incomplete, and 1 on error;
none of these outcomes means that API acceptance itself passed.

The exporter obtains operator **Entra user** kubeconfigs with explicit Azure
subscription, allocated cluster name/resource group, context, and local file.
It never uses `--admin`, changes global CLI defaults, creates cloud resources,
or modifies Kubernetes resources. It verifies resource IDs/tags, discovers the
actual HTTPS Application Gateway public-IP DNS output, and performs trusted
public health GETs. It deliberately uses Azure gateway/PIP reads rather than
Radius, avoiding Radius's global-HOME/kubeconfig discovery behavior.

An existing bootstrap `management.kubeconfig` is reused at its canonical path,
without rewriting it, after checking private permissions, current context, and
the selected server/CA/exec profile against fresh credentials for the owned AKS.
Only then is it used for the live cluster-UID read. Foreign or altered files are
refused before use, not overwritten to force the export through.

Only `.data.DEMO_KEY` from each named `ROLE-api-runtime` Secret is exported into
its private relative key file. No provisioner credentials file, whole Kubernetes
Secret, setup password, or parent DSN is copied. Management inventory uses a
read-only database transaction inside the management API pod; parent connection
metadata is parsed inside the respective reconciler and returns only host/port.
PostgreSQL subnet CIDRs are derived from the **matching parent allocation's**
role IP and verified against the actual Azure delegated subnet. Dictionary key
order is never used as a role index.

Each complete plane is published progressively. Every `acceptance.json` points
at a matching immutable `exported-state/GENERATION/endpoints.json`, so the
unchanged runner cannot mix generations. `endpoints.json` remains an atomic
compatibility projection for `harness/api.py`. Generations and keys remain
protected in project state until environment teardown. A single-writer lock
prevents concurrent exporters from dropping newer targets. Missing children
preserve the previous snapshot; authorization errors, ownership/identity changes,
key changes, invalid TLS, and inconsistent allocation fail instead of becoming
fake readiness.
Every pass refreshes AKS ownership/Entra configuration and live cluster identity,
gateway/public-IP ownership and DNS, and the parent delegated subnet. Cached
kubeconfig files may be reused; previous ownership decisions are never reused.
The exporter also supplies normalized `images.api`/`images.provisioner`
references and management's `provisioner` component. Rerunning it upgrades an
older owned snapshot without manual edits or exporting provisioner credentials.

The same run automatically supplies the normal cleanup handoff:
`cleanup-targets.json` (version 1) maps known, verified clusters to exact ARM IDs,
cluster UIDs, contexts, private `<slot>.kubeconfig` files, and `cleanup-radius.yaml`.
The latter contains only cleanup workspace connections, using context names as
workspace names and Radius group `radplanes`. It does not alter active
`radius.yaml` or read the provisioner PVC. Management inventory's existing
clusters receive cleanup access even without a showcase tenant assignment.
For nested export configurations, acceptance paths remain relative to the
export directory, but cleanup paths are relative to `.state/azure`. Select the
nested metadata with cleanup's `--targets operator-export/cleanup-targets.json`
when exporting under `operator-export/`. Earlier generated basename-only
cleanup references are repaired on rerun without changing the acceptance
generation.

Cleanup metadata is published after live AKS/Entra/FQDN/cluster-UID verification,
before application/gateway readiness. Thus a partial export can support cleanup
without claiming a completed demo export or onboarding readiness. If management's
app is unavailable, only its own newly verified access can be discovered on
that pass; previous child exports remain intact. Cleanup independently checks
every existing cluster and fails on missing targets or changed ownership.

Generated cleanup files have ownership markers and project/subscription checks;
manual, foreign, symlinked, or scope-changed files are refused. Unowned
kubeconfigs are not overwritten. Files are atomically replaced with mode `0600`
under the required `0700` environment state directory. Missing cleanup metadata
is repaired on rerun even when `acceptance.json` does not change. No tokens, DSNs,
full Secrets, or provisioner credential files are added to this handoff.
See [cleanup checks and execution](../../docs/cleanup.md); the exporter itself
never runs deletion or scaling commands.

The existing `endpoints.json` and relative `key_file` format is unchanged; see
`docs/provisioning.md`. Key files and kubeconfigs must be private (`0600`) and
remain within the environment state directory. Azure API URLs must be actual
`*.cloudapp.azure.com` HTTPS endpoints with normal certificate verification;
redirects are not followed. Local HTTP is restricted to loopback ports
35490–35499.

The generated `.state/azure/acceptance.json` has this shape (illustrative only):

```json
{
  "version": 1,
  "environment": "azure",
  "project": "radplanes",
  "synthetic_data": true,
  "endpoints_file": "exported-state/GENERATION/endpoints.json",
  "onboarding_timeout_seconds": 3600,
  "images": {
    "api": "PROJECT.azurecr.io/api@sha256:DIGEST",
    "provisioner": "PROJECT.azurecr.io/provisioner@sha256:DIGEST"
  },
  "tenants": {
    "shared_a": "shared-a",
    "shared_b": "shared-b",
    "isolated": "isolated-c"
  },
  "targets": {
    "management": {
      "context": "radplanes-management",
      "kubeconfig": "management.kubeconfig",
      "namespace": "radplanes-management-management",
      "cluster_uid": "ACTUAL-KUBE-SYSTEM-NAMESPACE-UID",
      "namespace_uid": "ACTUAL-APPLICATION-NAMESPACE-UID",
      "components": {
        "management-api": {"deployment": "ACTUAL-NAME", "container": "ACTUAL-NAME"},
        "provisioner": {"deployment": "ACTUAL-NAME", "container": "ACTUAL-NAME"}
      }
    },
    "shared-control": {
      "context": "radplanes-shared-control",
      "kubeconfig": "shared-control.kubeconfig",
      "namespace": "radplanes-shared-control-control",
      "cluster_uid": "ACTUAL-KUBE-SYSTEM-NAMESPACE-UID",
      "namespace_uid": "ACTUAL-APPLICATION-NAMESPACE-UID",
      "components": {
        "control-api": {"deployment": "ACTUAL-NAME", "container": "ACTUAL-NAME"},
        "control-reconciler": {"deployment": "ACTUAL-NAME", "container": "ACTUAL-NAME"}
      },
      "parent": {
        "host": "ACTUAL-MANAGEMENT-PG.postgres.database.azure.com",
        "port": 5432,
        "allowed_cidrs": ["10.64.48.0/27"]
      }
    }
  }
}
```

The placeholders are intentionally not executable defaults. Also supply targets
`shared-data`, `isolated-1-control`, and `isolated-1-data` (or the actually assigned
isolated slot). Data targets need `data-api` and `data-reconciler` components.
`shared-data.parent` describes **shared control PostgreSQL**, not management.
The parent host is verified against the selected reconciler's own DSN. The
allowed subnet is the operator-owned allocation, not an inferred whole VNet.

Every tested Deployment's pod template needs both:

```text
plane-demo/project=radplanes
plane-demo/component=management-api|control-api|control-reconciler|data-api|data-reconciler
```

Configured contexts and namespace/cluster UIDs are checked before mutations.
The helper verifies the pod → ReplicaSet → configured Deployment UID chain,
requires exactly one replica, and refuses host-networked pods. Context names,
name prefixes, or project labels alone are not sufficient authority.

**Export prerequisite:** keep the operator exporter running to publish actual child
endpoints/key files, host-usable scoped kubeconfigs, namespace UIDs, and component
names after each child bootstrap. The runner rereads state before accessing a
new child and waits up to 30 seconds for endpoint export. It does not copy the
provisioner's whole credentials file or guess DNS names/cluster UIDs. Missing
exports fail explicitly. This is the same operator exporter prerequisite
already described in `docs/provisioning.md`, now including target identity
metadata. The runner can start with management exported while the parent export
process updates child entries during onboarding.

## Scenario

A fresh scenario requires the three configured tenant IDs to be absent.
The guarded continuation above permits only its verified first tenant. It performs:

1. Prompt management `202`, persisted operation lookup, duplicate `409` with the
   original status URL, and another request's `503`/`Retry-After` while the first
   operation is pending/running. An unobserved busy window is a failure.
2. Two shared tenants and one isolated tenant. Management inventory is read
   through the management API pod's read-only database login; four distinct
   child cluster IDs and five distinct actual cluster UIDs are required.
   Authenticated control-PostgreSQL and data-Redis endpoint identities are
   compared before/after shared reuse and against the isolated pair.
3. Pausing only the shared data reconciler for the second shared onboarding:
   management must become ready while control is pending and data returns 404.
   The drain budget is the Deployment's termination grace plus 30 seconds of
   controller margin; grace must be an integer from 0 to 300 seconds. This does
   not change the 30-second outage catch-up limit. The original replica count
   is restored in `finally`, including on a drain timeout; Pods are not force-deleted.
4. Consecutive control versions, applied message/version checks, atomic counter
   changes without crossing tenant keys, missing/wrong-key rejection, minimal
   health, and absence of the old environment-disclosing route.
5. Complete two-event-page timeline reconstruction, immediate-child-only event
   types, report deduplication, and another three poll intervals with no changes.

Update assertions compare the exact requested messages, not merely control/data
agreement. Every tenant receives a nonzero counter before updates; counters and
messages must survive both configuration changes and observed successful
control-reconciler polls of management. An `applied` summary must match the
onboarding UUID, desired version, last-applied version, and a valid report
timestamp. Timelines must contain creation, every desired update, and the latest
successful application report; skipped intermediate **applied** versions remain
valid.

The authenticated runtime endpoints are datastore identity evidence, **not**
Azure ARM resource IDs. Radius ownership, Azure Activity Log attribution,
network/identity authorization, certificates, and full teardown remain the
parent integration/deployment gates; this runner does not claim to replace them.

## Scoped Cilium outages and restoration

Both fault targets require an established, serving `ciliumnetworkpolicies.cilium.io`
CRD and the approved Azure CNI/Cilium dataplane. No cluster installation or CNI
change is attempted. Plain kind has no verified fault implementation here:
`--mode outages` or `all` with `environment=local` fails before tenant mutation.

The helper creates one uniquely named **namespaced CiliumNetworkPolicy** with:

* project/component endpoint labels selecting only the intended reconciler;
* `egressDeny` to every private parent address resolved **inside that pod**, each
  narrowed to `/32` (IPv4) or `/128` (IPv6), only on the configured PostgreSQL TCP
  port;
* `enableDefaultDeny: {ingress: false, egress: false}`, so adding this fault does
  not unintentionally block DNS, control's local PostgreSQL, or Kubernetes.

Cilium deny rules override existing allow rules. This is not an additive
Kubernetes “deny-all” policy pretending to override an existing allow. Before
and after the fault, the helper snapshots and compares the UIDs and canonical
specification hashes of all namespaced NetworkPolicy/CiliumNetworkPolicy objects;
it never rewrites them. Hashes avoid copying potentially sensitive pre-existing
L7 header rules into evidence. The helper's own non-secret deny policy is stored
in full for precise recovery.

Normal deployment policy remains parent-owned. A least-privilege normal
control-reconciler Cilium egress policy should allow:

```yaml
spec:
  endpointSelector:
    matchLabels:
      plane-demo/project: radplanes
      plane-demo/component: control-reconciler
  egress:
    - toEndpoints:
        - matchLabels:
            k8s:io.kubernetes.pod.namespace: kube-system
            k8s:k8s-app: kube-dns
      toPorts:
        - ports: [{port: "53", protocol: UDP}, {port: "53", protocol: TCP}]
    - toCIDR: ["ACTUAL-MANAGEMENT-PG-IP/32", "ACTUAL-LOCAL-CONTROL-PG-IP/32"]
      toPorts:
        - ports: [{port: "5432", protocol: TCP}]
```

For data reconciliation, allow DNS plus its control PostgreSQL IP/port and
`toEntities: [kube-apiserver]` on TCP 443. If using node-local DNS, add the actual
node-local DNS destination with an appropriate Cilium entity/endpoint rule;
do not assume kube-dns-only policy permits that path. The baseline must already
work before the fault: both parent and local prerequisites are queried first.
This helper neither installs those normal policies nor introduces an allow-all
rule to make tests pass.

A persistent `kubectl exec` Python probe first opens a real parent Psycopg
connection, using the reconciler's own environment without printing it. After
policy creation, both a **new connection** and that **already-open connection**
must fail with network/timeout errors. The latter uses bounded TCP user timeout,
not an indefinitely stalled query. The local control database (management-link
fault) or local Kubernetes API (control-link fault) must still work. DNS changes,
pod replacement, permission failures, or non-enforcement fail the test.

Each outage then lasts at least 60 seconds with ten successful counter calls.
The management-link test changes control configuration and proves data catches
up despite failed control→management queries. After restoration, a successful
control-reconciler poll is observed in timestamped logs without recording raw
log text or adding duplicate parent events.

The control-link test issues two new desired versions while data cannot read
control. It serves the old local version throughout, deletes only the
UID-checked data API pod, ensures its Deployment still has one replica, and
checks the replacement preserves configuration and Redis count. Restart recovery
time is recorded; this single-replica demo does not claim uninterrupted
availability during pod replacement. After restoration the latest version must
converge within 30 seconds, without an intermediate applied-version event.
Policy deletion, connectivity verification, cleanup, and application catch-up
share one monotonic recovery deadline. Cleanup does not grant a fresh 30-second
polling window. Report observations after that deadline fail even if eventually
successful.
Physical connectivity restoration is recorded separately as `physical_restored`.
`restored=true` requires final probe cleanup and completion recording within the
same deadline. A 29-second restoration followed by a two-second probe close is
a failed run, including when using the standalone fault CLI.

A data API restart during the management-link outage is **not applicable** to
the approved acceptance plan. Restart is required only during the control-source
outage; no second restart scenario has been added.

The fault is removed in `finally`, including after failed probes or an ambiguous
create response. Deletion has a Kubernetes UID precondition. The helper refuses
to delete an object whose ownership/specification changed. A fresh parent query
and exact original-policy comparison are required before `restored=true`.
Restoration failure is a failure, never masked by successful API requests.

Standalone fault and recovery commands:

```sh
uv run python harness/fault-parent-link.py --config .state/azure/acceptance.json \
  --slot shared-control --component control-reconciler --duration 60 --execute

# After an interrupted operator process, use its exact recorded evidence file:
uv run python harness/fault-parent-link.py --config .state/azure/acceptance.json \
  --restore .state/azure/evidence/fault-EXACT-RUN.json --execute
```

SIGINT/SIGTERM take the `finally` path. SIGKILL, host loss, or Kubernetes API
unavailability can still prevent restoration; the protected evidence file
contains the exact owned policy and identities for the recovery command.
Recovery reports `restored_only_not_acceptance`, never a passed acceptance run.

Policy references:

* https://docs.cilium.io/en/stable/security/policy/deny/
* https://docs.cilium.io/en/stable/security/policy/intro/
* https://learn.microsoft.com/en-us/azure/aks/azure-cni-powered-by-cilium
