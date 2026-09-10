# Implementation findings and verification

This file records implementation reviews and observed results. Planned checks
are not passing checks. Runtime artifacts containing credentials stay out of Git.

## Status

Implementation started 2026-09-09. Phase 0's scoped preflight is complete.
Azure integration and application implementation are in progress.

## Phase 0 - Foundations

Baseline: commit `79f4cc4`; clean worktree before implementation.
The current Azure login targets the subscription recorded in `.azure/plan.md`.
Radius, Azure CLI, kubectl, Helm, kind, Docker CLI, uv, Terraform, ShellCheck,
and jq are installed. Docker Desktop's daemon was not responsive at the first
probe; it must be running before image or local-datastore verification.

Rubber-duck review: one correctness finding (F001).
Security review: no vulnerabilities found in the phase-zero scope.

Verification: four script regression tests passed with the project Python 3.13.
The real Azure preflight passed after the correction: scoped operator lookup,
Docker Desktop readiness, Radius 0.60.2/Bicep 0.42.1, and regional capacity
(0/100 regional and DSv5 cores used at the time). No Azure resources were
created by this phase. Both final F001 fix reviews reported no findings.

## Findings ledger

| ID | Phase | Severity | File | Lines | Finding | Status |
|---|---|---|---|---|---|---|
| F001 | 0 | Medium correctness | `scripts/project.py` | 84-90 (initial) | Operator Graph lookup used the global tenant instead of the project subscription | Resolved; live preflight, 4 tests, rubber-duck and security fix reviews passed |
| F002 | 1 | Deployment blocker | Azure PostgreSQL regional capability | n/a | Subscription cannot provision PostgreSQL in East US 2 or West US 2 | Resolved: Central US managed PostgreSQL created through Radius and verified TLS connection succeeded from AKS |
| F003 | 1 | High correctness | `infra/bootstrap/azure.bicep` | 144-156, 245-247 (initial) | Compiled module scopes include invalid loop expressions in non-loop references | Resolved: compiled-contract tests, fix walkthrough, real ARM what-if/validate pass |
| F004 | 1 | High correctness | `infra/bootstrap/federation.bicep` | 11 (initial) | Concurrent writes to one identity's federated credentials can return 409 | Resolved: serial loops, fix walkthrough, and live bootstrap/child federation succeeded |
| F005 | 1 | High security | `infra/bootstrap/platform-access.bicep` | 150-167 (initial) | Tenant-reachable gateway/issuer identities can read or replace other planes' certificates in the shared vault | Resolved: exact object grants, both fix reviews, own-account access and live cross-plane HTTP 403 proved |
| F006 | 1 | High correctness | `scripts/install-radius.py` | 38-59 (initial) | Existing workload-identity label means a patch does not restart pods after identity annotations | Resolved: explicit rollout restart, regression, both fix reviews, four actual identity projections verified in management and gate child |
| F007 | 2 | Medium correctness | `src/plane_demo/control_api.py` | 35-52 (initial) | A delayed older-version success hides failure of the current desired version | Resolved: reproduced before fix, 37 related real-PostgreSQL integration tests passed after, both fix reviews clean |
| F008 | 3 | High correctness | `infra/radius/apps/challenge.bicep`, `workload.bicep` | initial workload integration | Challenge responder correctly returns 404 at /livez, so an inherited HTTP probe would restart it | Resolved: corrected/fix-reviewed TCP probe and Recreate strategy observed in the live challenge Deployment |
| F009 | 3 | High correctness | `scripts/publish-artifacts.py` | 67-107 (initial) | Existing tag accepted without verifying its trusted source/digest record | Resolved: regression tests and both fix reviews pass; real publication succeeded |
| F010 | 3 | Medium correctness | `scripts/issue-certificate.py` | 131-144 (initial) | Staging issuance could replace the live versionless certificate | Resolved in source: validation-only staging, regression and both fix reviews pass |
| F011 | 3 | Medium correctness | `scripts/issue-certificate.py` | 100-109 (initial) | Failed first issuance discarded a newly registered ACME account | Resolved in source: finally persistence, regression and both fix reviews pass |
| F012 | 3 | High security | `containers/provisioner/Dockerfile` | 41 (initial) | Bundled Bicep executable downloaded without digest verification | Resolved: published per-architecture SHA-256 verification, both fix reviews, actual ACR build/tool startup passed |
| F013 | 3 | Medium security | `.dockerignore` | 1-16 (initial) | Build context omitted several credential-file exclusions | Resolved: credential patterns, explicit runtime-script copies, both fix reviews pass |
| F014 | 1 | Connectivity blocker | Management AKS administrative endpoint | live | Operator connection times out despite healthy AKS | Direct laptop access remains unreliable despite /32 refresh; authenticated AKS Run Command and in-cluster operator/harness execution work without broadening CIDRs |
| F015 | 1 | High correctness | `scripts/install-radius.py` / Radius workspace loader | live | Radius workspace enumeration ignores KUBECONFIG and reads HOME/.kube/config | Resolved: per-cluster HOME, actual workspace/credential registration and identity verification, both fix reviews pass |
| F016 | 1 | High correctness | `infra/radius/environments/project-azure.bicep` | live | Azure provider credentials do not automatically authenticate private Recipe downloads | Resolved: documented WI registry SecretStore, real download/deploy and both fix reviews pass |
| F017 | 1 | Deployment blocker | `infra/radius/recipes/azure/cluster.bicep` | live | Cross-resource-group nested Azure modules fail in Radius deployment-engine evaluation | Resolved: flat Recipe/per-slot scope, both fix reviews, actual AKS creation by Radius identity, child Radius installation and HTTP workload proof passed |
| F018 | 3 | High correctness | `src/plane_demo/providers/azure.py` | 374-380 (initial) | Environment registration omitted required private-registry authentication inputs | Corrected and fix-reviewed; included in current 117 passing coordinator tests; full onboarding pending |
| F019 | 3 | High correctness | `src/plane_demo/providers/azure.py` | 932-936 (initial) | Management deployment omitted provisioner managed-identity client ID | Corrected and fix-reviewed; current coordinator tests pass |
| F020 | 3 | Medium correctness | `src/plane_demo/providers/azure.py` | 678-687 (initial) | Incomplete PostgreSQL Recipe state could be replayed before intent persisted | Pre-submission intent/refusal implemented and fix-reviewed; current coordinator tests pass |
| F021 | 3 | Medium correctness | `src/plane_demo/provisioning.py` | 95-127 (initial) | Malformed operator network fields were passed to infrastructure | Parsed IPv4/CIDR validation implemented and fix-reviewed; current coordinator tests pass |
| F022 | 4 | Medium security | `scripts/fault-parent-link.py` | 76-79, 127-140 (initial) | Caller working directory and arbitrary project identity defined the mutation boundary | Fixed and independently reviewed; offline harness tests pass, live acceptance pending |
| F023 | 4 | Medium correctness | `scripts/test-e2e.py` | 623-646 (initial) | Ignored message updates or reset counters could pass acceptance | Exact values/counters asserted; fix-reviewed and offline-tested |
| F024 | 4 | Medium correctness | `scripts/test-e2e.py` | 471-478 (initial) | Stale applied version or missing report time could pass | Exact current child report checked; fix-reviewed and offline-tested |
| F025 | 4 | Medium correctness | `scripts/test-e2e.py` | 666-680 (initial) | Empty control timelines could pass | Required events checked while allowing skipped intermediate versions; fix-reviewed |
| F026 | 4 | Medium correctness | `scripts/test-e2e.py`, `scripts/fault-parent-link.py` | recovery deadline (initial) | Cleanup time was excluded from catch-up timing | Deadline includes final cleanup/recording; late standalone run fails; final fix reviews and 88 offline tests pass |
| F027 | 4 | Medium correctness | `scripts/test-e2e.py` | provenance checks (initial) | Coordinator code and replacement data pod omitted from source verification | Provenance extended to coordinator and replacement pod; fix-reviewed |
| F028 | 3 | High correctness | `containers/provisioner/Dockerfile` | tool runtime check | Bicep binary cannot start without ICU on the slim base image | Resolved: libicu72, real Bicep startup/build and both fix reviews pass |
| F029 | 3 | High correctness | `containers/provisioner/*requirements*`, `azure-cli.txt` | dependency resolution | Certificate overlay upgraded urllib3 beyond Kubernetes support, and macOS resolution omitted Linux distro dependency | Resolved: Linux/API-constrained lockfiles, actual tool checks and compatible package checks in ACR build, both fix reviews pass |
| F030 | 1 | Deployment blocker | Application Gateway / private Key Vault integration | live HTTPS update | App Gateway cannot access the issued certificate reference despite scoped identity grants | Resolved: documented trusted-service network exception, both fix reviews, App Gateway Succeeded and normally verified HTTPS request passed |
| F031 | 3 | High correctness | Radius workspace creation under restricted runtime identity | live | Helm installation check needs Secret-list permission that the runtime identity intentionally lacks | Known workspace + actual Radius API validation implemented and fix-reviewed; latest full coordinator suite passes |
| F033 | 3 | High correctness | `src/plane_demo/providers/commands.py` | failed-command diagnostics | Radius reports deployment errors on stdout; stderr-only logging hid the cause | Bounded redacted stdout and regression implemented; fix and layout reviews passed |
| F034 | 4 | Medium security/correctness | `scripts/export-state.py` | watch ownership cache | Cached trust decisions could miss changed cluster/gateway ownership | Fresh ownership checks every pass; final fix reviews and offline regressions pass |
| F035 | 3 | High correctness | `infra/radius/apps/workload.bicep` | worker/base serialization | Workers emitted forbidden livenessProbe:null; paired base Deployment omitted its ServiceAccount name | Optional probe omitted and base ServiceAccount matched; live worker proof, compiled regression, fix and layout reviews passed |
| F036 | 3 | High correctness | workload and operator Pod security contexts | real PVC remount | Default fsGroup recursion changed private credentials from 0600 to 0660, preventing restart | OnRootMismatch preserves 0600 in a real second mount; strict loader retained; fix and layout reviews passed |
| F038 | 7 | Medium correctness | `operations/clean-azure.py` | role_state | Cleanup absence proof relies on a broad role listing rather than checking every manifest GUID | Exact manifest-GUID queries added and fix-reviewed; regression prevents false clean result |
| F039 | 7 | High integration | `harness/export-state.py`, `operations/clean-azure.py` | cleanup handoff | Exporter does not yet generate cleanup-targets.json and its Radius workspace metadata | Automatic protected export implemented; validated bootstrap-file adoption and nested paths; final fix reviews pass |
| F040 | 7 | Medium correctness | `operations/clean-azure.py` | interrupt handling | KeyboardInterrupt bypasses the explicit incomplete-cleanup result | Explicit 130/incomplete outcome implemented and reviewed; no false rollback claims |
| F041 | 3 | High correctness | `src/plane_demo/management/providers/azure.py` | management_permissions | Worker had namespace pod/port-forward access but lacked cluster-scoped access to Radius's aggregated API | Narrow planes/local, resourceName radius grant added; live 403 became 200 and worker stayed Running without restarts; fix review/unit checks recorded below |
| F042 | 4 | Deployment blocker | `harness/run-azure.py`, `infra/bootstrap/azure.bicep` | identity selection | Harness reused coordinator identity, which cannot read gateway/subnet metadata | Resolved: dedicated harness identity, both fix reviews, 21 exact live grants verified; exporter ready and first tenant provisioning started |
| F043 | 4 | High correctness | `harness/export-state.py` | optional AKS lookup | AKS returns NotFound during creation as well as ResourceNotFound; exporter treated the former as fatal | Exact optional-lookup alternative added; run-path regression, rubber-duck pass, and security fix review passed |
| F044 | 3 | Deployment blocker | `src/plane_demo/management/providers/azure.py` | data prerequisites | Bootstrap ConfigMap Roles collide with Radius's same-name generated container Roles | Resolved: distinct names, source/fix reviews, actual Role/Binding inspection and successful fresh tenant provisioning |
| F045 | 3 | Deployment blocker | `infra/radius/recipes/azure/redis.bicep` | existing endpointNic | Radius reads the declared existing NIC before the private endpoint creates it | Resolved: existing-NIC dependency, compiled regression/fix reviews, locked Recipe publication and successful fresh execution |
| F046 | reset | Correctness/access blocker | `operations/clean-azure.py` | Radius-only group inventory | Partial reset requests Reader permission on node groups whose AKS was never created | Inspect every live AKS node group before deletion; report other node groups as uninspected and untouched; full cleanup unchanged; both fix reviews and regressions pass |
| F047 | reset | Operational blocker | Failed Redis Recipe outputs | live | Failed Recipe left created cache/endpoint/NIC outside Radius deletion tracking | Normal reset stopped safely; exact ownership and link graph verified, explicit operator removal completed, app group empty; not claimed as normal Radius cleanup proof |
| F048 | reset | Medium security | `.state/azure/remove-orphaned-redis.py` | 14-68 (initial) | Python optimization removes assert-based deletion safety checks | Unconditional require checks and guarded mutation path replace assertions; normal/optimized run-path cases and final fix reviews pass |
| F049 | reset | Medium security | `.state/azure/radius-reset-execute.json` | 81 (initial) | Generated executor still references old immutable bootstrap after source guard repair | Original manifest archived; new preview/execute references repaired immutable payload, compared byte-for-byte and checked for unconditional guards; final review clean |
| F050 | 4 | High correctness | `harness/test-e2e.py` | IDENTITY_PROBE | Attribute-based Redis TLS detection rejects a real verified TLS socket | Resolved: negotiated-socket detection, optimization-safe PING, actual live probe pass, bounded first-verification continuation and both fix reviews pass |
| F051 | 4 | High correctness | `harness/test-e2e.py` | paused_reconciler | A 30-second drain deadline leaves no room for the Pod's own 30-second shutdown grace and controller latency | Resolved: bounded grace-aware drain, regressions/review, and live shared-b readiness while data paused followed by restored application proof |
| F052 | 4 | External interruption | Azure governance automation | 2026-09-10T00:06Z | All five project AKS clusters and three PostgreSQL servers were stopped during acceptance despite requested tags | Normal scoped restoration, preserved interrupted state, and reviewed owner-ordered reset completed; all three fresh operations then succeeded; governance controls unchanged |
| F053 | reset | External metadata change | Two management state disks | 2026-09-10T00:55Z | State disks lost required tags after preview, so strict cleanup stopped before mutations | Exact PV/PVC/CSI ownership verified; only required tags merged, properties preserved; reviewed reset and exact disk absence subsequently verified |
| F054 | reset | High correctness | `infra/radius/recipes/azure/redis.bicep` | result.resources | Radius cannot resolve a deletion API version for tracked Microsoft.Resources/tags metadata | Resolved for fresh resources: real create/tag/idempotence/tracking and Radius deletion passed; final native-owner cleanup and unchanged-resource postconditions verified; old deployed records still require explicit cleanup |
| F055 | 4 | High correctness | `harness/test-e2e.py` | initial endpoint discovery | A 30-second endpoint-export wait is shorter than the five-cluster exporter's actual 90-second scan | Resolved: bounded discovery, 90-second-delay regression, unchanged convergence deadlines, 185 tests/211 subtests, clean fix reviews, and live existing-tenant verification passed |
| F056 | lifecycle gate | Medium observability | One-off F054 gate | command logging | Setting the command logger to CRITICAL hid the first gate's already-redacted failure diagnostics | Resolved: standard ERROR diagnostics, 165 focused tests, clean direct walkthrough/security review; next live attempt exposed the actual F057 error |
| F057 | lifecycle gate | Medium correctness | One-off F054 gate | group-show preflight | The wrapper supplied `--group` while the caller also supplied a positional group name, which Radius rejects | Resolved: duplicate removed, explicit scope retained, run-path regression/reviews passed; live group-show now succeeds |
| F058 | lifecycle gate | Medium correctness | One-off F054 gate | group-ID comparison | Radius returns lowercase `resourcegroups`, which failed a case-sensitive comparison with `resourceGroups` | Resolved: actual ID verified, existing comparison helper reused, foreign-group regression/reviews passed; create5 passed Radius preflight |
| F059 | lifecycle gate | High correctness | `redis_nic_tags.py` | location validation | Managed Redis returns `Central US`, not `centralus`, so the new helper rejects the correctly owned cache before tagging | Resolved: actual response/alias verified, guards/regressions/reviews passed, rebuilt image inspected, real baseline and fresh NIC tagging/idempotence passed |
| F060 | lifecycle gate | Medium compatibility | Radius 0.60.2 CLI | unbound SecretStore deletion | `resource delete` tries to parse the generated empty application string before calling the deletion API | Reviewed native Radius owner-API cleanup passed; Radius auth/backing Secret absent and all lifecycle postconditions verified; reusable gate correction being finalized |

Cleanup review F037 (claimed unsupported `resource delete --application`) was
not reproduced. The installed CLI declares the flag, and an offline invocation
with a valid workspace reaches Kubernetes configuration rather than rejecting
it. Pinned command/scope source also does not reject it. Do not treat this as a
proven deployment blocker; ownership must remain established by cleanup's
resource/group checks, not by an assumed application filter.

A suspected F032 in-cluster context override was withdrawn after tracing the
actual CLI call path. The generic helper prefers in-cluster credentials, but
the CLI uses `NewCLIClientConfig -> NewClientConfigFromLocal`. No change is
needed for that suspicion; the independently verified HOME issue is F015.

F001 first correction (`az rest --subscription`) failed the fix walkthrough:
Azure CLI's Graph request path can still acquire the default tenant's token.
The final correction explicitly acquires a subscription-scoped Graph token with
`az account get-access-token` and sends it over verified HTTPS directly to
Graph `/me`, without redirects or token logging. Regression tests inspect every
Azure command from the actual preflight path, the token passed to Graph, and
redirect rejection. No global `az account set` is used.

F002 verification: the restricted-region regression rejects an empty edition
list before writing deployment context. The real Central US preflight passed;
the fix-specific rubber-duck and security reviews found no remaining issue.
This does not claim that PostgreSQL has already been provisioned.

## Phase 1 - Azure integration source review

The initial review evaluated compiled source before deployment. Rubber-duck review
identified F003/F004/F006; security review identified F005 with confidence 9/10.
The reviewed bootstrap has now deployed successfully. Both management AKS nodes
are Ready, Radius is installed, and all four Radius workload identities were
verified in real pods. Four Recipe artifacts were published and tag-locked in
the project ACR. The actual resource-tag audit found no missing requested tag.
Child-cluster and certificate execution remain live gates; PostgreSQL and HTTP
gateway results are recorded below.

PostgreSQL gate now passed: Radius created a private PostgreSQL 16 server and
its setup Secret in the management namespace. A Job using the Azure-built API
image connected with `sslmode=verify-full` and checked `pg_stat_ssl.ssl=true`.
The connection-test Job was removed. Setup credentials were never printed.
The isolated API image was pulled from ACR and all 16 included Python/SQL file
hashes matched the source; it runs as UID 10001 and excludes provisioner code.

Gateway HTTP gate passed: Radius created a real Application Gateway plus two
private load-balancer Services (`10.64.0.240` and `10.64.0.241`). A request to
the Azure-provided DNS name returned the expected public challenge token;
`/api/container-info` returned 404. The actual challenge Deployment has a TCP
liveness probe, not an HTTP probe to an intentionally absent endpoint.
This proves routing only, not final HTTPS or tenant application readiness.

Cluster gate passed: the flat Recipe created `aks-radplanes-shared-control`.
Azure Activity Log caller/client IDs match management Radius's managed identity,
not the operator. A Job inside management used the separate coordinator identity
to fetch child credentials, install Radius there, verify four identity projections,
and deploy a real child container through child Radius. Its HTTP token check
passed. The gate cluster must be removed before the clean onboarding scenario.

Gate reset verified: the child smoke application, child AKS, and empty
management PostgreSQL were deleted through their owning Radius installations.
Azure queries returned no child AKS, no managed node resource group, and no
management PostgreSQL server. The PostgreSQL pre-delete check found zero
application tables. Named temporary Jobs/ConfigMaps were removed; the management
foundation and gateway remain for the real onboarding run.

Certificate issuance and HTTPS gates passed: staging validation completed
without importing a staging certificate. Production Let's Encrypt issuance/import
succeeded through the scoped issuer Job. After the F030 network correction,
Application Gateway reached Succeeded and a normal certificate-verified HTTPS
request returned the expected token without a custom CA or disabled verification.
An actual issuer Job could read its own ACME account but received HTTP 403 for
another plane's account secret. This is gateway infrastructure proof; the full
tenant onboarding and outage scenario remains pending.

| # | Severity | File | Lines | Vulnerability | Confidence |
|---|----------|------|-------|---------------|------------|
| 1 | 🟠 HIGH | `infra/bootstrap/platform-access.bicep` | 150-167 (initial) | Vault-wide gateway/issuer access permits cross-plane certificate access | 9/10 |

## Phase 2 - Application source review

The API/reconciler layer passed 77 tests using real PostgreSQL and Redis; one
Kubernetes test awaits the live cluster. The final-image smoke exercised
bootstrap and HTTP 200/401/422/202/409/503 behavior, nonroot execution, and
source-content hashes. The phase security review found no vulnerabilities.
The rubber-duck review found F007; it is fixed and both fix reviews found no
remaining issue. The Azure API image was rebuilt after the fix and its actual
source contents verified. These tests do not replace Azure end-to-end acceptance.

PostgreSQL 16 setup observation: CREATEROLE grants a role creator ADMIN but not
SET/INHERIT by default. The initializer temporarily enables and restores those
exact membership flags, tested under a non-superuser setup login. Runtime
roles remain non-owner/non-superuser and cannot perform schema initialization.

## Accepted scope limitations

The design is a synthetic-data, trusted-operator demonstration. A compromised
management demo key can authorize costly provisioning; the user explicitly
declined additional product limits for that scenario. This is not a fixed
security claim. Local Docker daemon access is powerful and project labels do
not confine it. High availability, automatic renewal, and interrupted
provisioning recovery are out of scope.

## Phase 4 - Acceptance tooling source review

The acceptance/fault tooling now passes 88 offline tests. It has not yet
run live faults. Source reviews and subsequent fix reviews covered F022-F027
and F034; final walkthrough/security reviews found no remaining issue in those
corrections.
The walkthrough additionally suggested restarting the data API during the
management outage. That is not an unmet requirement: the approved plan requires
that restart during the control-source outage only, which the runner already
does. No extra restart scenario is added merely to satisfy a broader review.

## Phase 3 - Coordinator correction review

The coordinator's F018-F021/F031 production fixes passed independent walkthrough
and security review. The walkthrough caught a stale mocked response in the
F031 test while that fixture was being updated. A fresh parent run of the
complete current coordinator suite passed 117 tests. The deployed public
onboarding path remains the next gate; mocked command tests are not that proof.

Live authorization checks passed using verified workload-identity principals:
the coordinator received Azure `403 AuthorizationFailed` for an AKS write,
and both coordinator and management Radius received the same denial for role
assignment writes. Probe bodies deliberately omitted required resource fields;
no probe resource or assignment was created. The actual project resource-tag
audit also found no resource missing `SecurityControl=Ignore`.

## Layout checkpoint

The user resumed the approved implementation on 2026-09-09 and deferred deeper
logic/SQL simplification until end-to-end proof. A layout-only pass is separating
runtime plane code, platform operations, image packaging, and the demo harness.
Historical finding paths above refer to the files at discovery time; they are
not rewritten to suggest those reviews ran against a later layout.

### Structure-only source verification, 2026-09-09

The working tree now groups runtime code under management/control/data,
shared helpers, and setup support. `operations/` contains platform commands;
`harness/` contains demo-driving/export/fault tooling; `images/` contains image
packaging. Radius `apps/` contains only the three planes and `modules/` contains
their helpers. The current Azure environment replaces the legacy map; no local
three-plane deployment is claimed. Obsolete tracked todo files were removed.

`make check` passed: Ruff across runtime/operations/harness/tests, three generated
Radius extensions, all 22 Bicep source files, 320 tests plus 47 subtests, and
ShellCheck. Fifty live-dependency integration tests were explicitly skipped.
The existing Starlette HTTPX deprecation warning remains; dependency versions
were not changed. All four old Redis CI guards now run against the current
Recipe/data application through the layout tests.

Regression checks exercise deployment path routing, new entrypoint imports from
the API image's exact COPY subset, absence of privileged code in that subset,
matching image/provenance source lists, and harness invocation from another
directory. SQL and unchanged shared helpers match their original contents
byte-for-byte. Documentation links and current source references were checked.
F033/F035/F036 source edits and their regressions remain intact.

Prior untracked cleanup/operator helpers, documentation, and tests were
preserved in their corresponding groups. No runtime state, credentials, or
cloud resources were moved/deleted. No image build, push, deployment, or commit
was performed. Existing image/integration evidence applies to its original
source; rebuilt-image inspection and live onboarding/outage proof are still
required. This source check does not replace the parent's layout reviews.

Parent layout verification also passed `make check` with the same 320 tests,
47 subtests, 50 explicit dependency-test skips, 22 Bicep files, and three type
extensions. Independent walkthrough and security reviews found no layout
regression. The inspected obsolete `.pytest-work` scratch directory was removed
after tests finished; live deployment state and credentials were untouched.

### Live management worker authorization correction

The reorganized management API and operator deployment completed, but the worker
initially crashed on Radius API authorization. A Job using the worker's actual
service account reproduced HTTP 403 for `api.ucp.dev` resource `planes/local`,
name `radius`.
The added ClusterRole grants only that Radius API plane to that service account;
it grants no core Secrets access, Kubernetes cluster-admin, or Azure role writes.
The same request returned HTTP 200 after the change, and the worker remained
Running with zero restarts. Independent fix reviews found the production grant
correct; the obsolete test prohibiting every ClusterRoleBinding was narrowed
to assert this exact grant and subject.

The corrected worker image was rebuilt and 51 actual source/artifact hashes
were verified in AKS. During its controlled update, management API admission was
stopped, zero pending/running operations were confirmed, then the idle worker
and immutable configuration were replaced without reinitializing PostgreSQL.
The operator Job completed successfully, all three management Deployments
became ready, and the worker emitted `provisioner_ready`. A real HTTPS probe
confirmed health 200, missing-key 401, and authenticated unknown-tenant 404.
No tenant was created by that probe; the clean three-tenant scenario is next.

### Cleanup handoff correction verification

The exporter now generates cleanup target/workspace metadata automatically,
without copying full provisioning credentials or overwriting active Radius
configuration. Existing bootstrap kubeconfigs require fresh owned-cluster,
context, CA and allowed-exec validation before reuse. Nested export paths
round-trip through the cleanup reader. Exact role-GUID and interruption tests
also pass. The final focused suite passed 64 tests and 15 subtests, and both
final fix reviews were clean. Live end-of-demo teardown remains unproven.

### In-cluster harness source verification

The optional Azure harness launcher uses the existing inspected provisioner
image, an actual clean-HEAD Git bundle with a checksum, and a separate
`harness-state` volume. It runs the existing exporter and scenario/fault runner;
it does not introduce another application plane or copy operator database
credentials. The exporter now accepts the launcher's bounded three-hour window.
Child diagnostics persist in private files rather than being discarded.
The complete offline harness suite passed 134 tests and 53 subtests, and both
source reviews were clean. First live scenario execution is still pending.

The source-bundle ConfigMap uses server-side apply: a 344 KiB Git bundle would
exceed Kubernetes' annotation limit if client-side apply copied the full object
into `last-applied-configuration`. The 900 KiB object bound remains, and conflicts
are not forced. Its targeted 28 tests and both fix reviews passed.

The first live harness Job stopped before login or tenant mutation because the
token guard compared a resolved file path with an unresolved `/var/run` root.
On the Linux image, `/var/run` aliases `/run`. Both paths are now canonicalized;
the mount boundary, identity checks, and symlink-escape refusal remain. Thirty
runner tests and both fix reviews passed. No tenant was created by that failed
startup.

### Dedicated harness identity correction

The second live harness Job authenticated, then failed before tenant creation:
the coordinator could read AKS but received `AuthorizationFailed` on Application
Gateway and delegated PostgreSQL subnet reads. These are exporter requirements,
not runtime provisioning requirements. Bootstrap now assigns a separate harness
identity project-scoped Reader and AKS cluster access, federated only to its own
management-cluster service account. The launcher selects and validates that
identity; missing metadata or reuse of the coordinator fails before execution.
Coordinator grants and runtime images are unchanged.

The focused harness and compiled-infrastructure suite passed 149 tests and
72 subtests, including emitted role assignments, federation, actual launcher
resource selection, and login/identity guards. Rubber-duck and security fix
reviews found no actionable issues. Real bootstrap what-if and ARM validation
passed at 2026-09-09T17:46:04Z. Deployment and renewed live acceptance are pending;
neither failed harness startup created tenants.

Bootstrap then succeeded. The new identity has the requested tags, the exact
harness federation subject, and 21 verified direct assignments across the 11
owned groups, with no extra direct grants. Runtime state was not changed.
`demo-acceptance-third` runs committed source `788cbda`, passed workload login
and management state export, and became ready for onboarding. The first real
tenant request started operation `036be342-eb25-459c-97a9-1b6af2229a15`; the worker
reported `stage=control-cluster` at 2026-09-09T17:53:06Z. This is the start of
actual API-driven provisioning, not a completed tenant or acceptance result.

### AKS creation absence-code correction

The third acceptance Job stopped at 2026-09-09T17:58:14Z when the exporter
received `ERROR: (NotFound) Could not find managed cluster resource` from
`az aks show` for shared-data. The persisted CLI log confirms exit 3 and this
exact service error; it was not an authorization failure. The exporter accepted
only `ResourceNotFound`. Both codes now mean pending for an explicitly optional
child lookup, never for required resources or an authorization error containing
those words elsewhere.

The direct rubber-duck pass checked that boundary and the actual exporter run
path. The independent security fix review found no vulnerabilities. The focused
suite passed 72 tests and 35 subtests. Both shared AKS clusters subsequently
passed real harness credential/Kubernetes access probes. Their original tenant
operation continued into child Radius installation without restart or replay.
The failed acceptance run remains failed; it is not relabeled as a pass.

### First-admission observation continuation

The opt-in harness flag `--continue-first-from` accepts only a protected failed
record with the exact first-admission event sequence. It verifies the original
commit timestamp, tenant/operation identity, message, and version; it never
POSTs the first tenant again. Both later tenant absence checks and all remaining
scenario/outage assertions still run. New evidence links the untouched failed
record by path, hash, source commit, and admission rather than rewriting history.

The retained live record is
`acceptance-c680a8557f3b4722b01f7815ad1b644b.json`, SHA-256
`36884b08e34c32413b449e319d8f44d9808d568d2120b6730d036bbe4448068b`.
Its actual first admission passed HTTP 202, duplicate, and busy checks before
the exporter stopped. The new offline harness suite passed 154 tests and 116
subtests; independent rubber-duck and security reviews found no actionable
issues. The walkthrough verified that the actual Git bundle includes the
predecessor commit. Live continuation remains to be run.

The continuation ran successfully up to observing the original provisioning
failure; it neither replayed admission nor bypassed that failure. At
2026-09-09T18:37:53Z the operation failed in `data-application`. Management and
control applications, private PostgreSQL, and control HTTPS had deployed, but
the data application hit F044/F045. Both the original operation and continued
acceptance remain failed.

### Data-plane ownership and existing-resource ordering corrections

Radius generates a Role named after each container. Bootstrap's same-name
ConfigMap Roles conflicted with Radius server-side apply on `.rules`. Bootstrap
now owns separate `data-api-configmaps` and `data-reconciler-configmaps`
Roles/Bindings, still bound to the original service accounts with exactly
`get`, and `get/create/patch`, respectively.

The Redis private endpoint and its NIC were eventually created and tagged
correctly. The actual failure was Radius's earlier lookup of the declared
`existing` NIC, not the final tagging operation. Adding `dependsOn: [endpoint]`
to that existing resource emits the dependency in the language-version-2
template. The tag extension already had its own dependency and retains it.

The direct rubber-duck pass checked both resource-ownership boundaries and the
compiled dependency graph. The independent security fix review was clean.
The coordinator/compiled-infrastructure suite passed 145 tests and 19 subtests.
Changed worker image and Recipe publication, content inspection, and a fresh
demo deployment remain required. Do not reset the failed operation to pending
or reinterpret it as a successful onboarding.

The corrected Redis Recipe is now published under
`redis:src-79ecb019e75cc095adde`, digest
`sha256:08ade83642b0482917c58068a41570e3f53217ce7bb93abe18459523bea2ff48`,
with its tag locked. Both images built from `ad031e2` were inspected in AKS:
21 API and 51 provisioner source/artifact hashes matched, grouped imports and
UID 10001 passed, and the API image excludes privileged provider code.

### Radius-only fresh-demo reset preparation

`clean-azure.py --radius-only` reuses normal owner-ordered application/child-AKS
deletion, then explicitly reports retained foundation rather than full cleanup.
It preserves live resource and managed-node ownership checks, rejects provider
fallback and local credential removal, and never directly mutates Azure groups
or roles. Custom role scans remain on the full cleanup path only, since the
partial path cannot delete those roles.

The direct rubber-duck pass and independent security review found no remaining
issue. The focused cleanup/export suite passed 63 tests and 40 subtests.
Operator access to the AKS endpoint still timed out on a fresh scoped check.
The operator verified exact tags and resource IDs in all three existing managed
node groups, then recorded temporary harness Reader assignments for those
groups in protected state. These read-only assignments must be removed after
the reset; no Azure deletion or role-delegation permission was granted.
Live reset, state/evidence preservation, and fresh onboarding remain pending.

The failed admission and continuation records are now archived on the host,
with their original SHA-256 values, alongside token-free exported cleanup
metadata. The reset executor runs in independent `radplanes-system`, not an
application namespace being deleted, and mounts no state PVC or human token.
Its source bundle and six allowed seed files are checked before use. A
temporary exact-subject federation is recorded with the Reader leases for
later removal; the executor security review was clean.

The first live read-only reset preview stopped on Azure `Forbidden` before any
deletion. Cleanup diagnostics now identify the Azure operation and owned
target rather than only the executable; permission failures remain fatal.
The direct walkthrough and independent security fix review were clean, and
48 cleanup tests plus 15 subtests passed. The read-only preview must pass
before executing the reset; no access failure is treated as absence.

The scoped failure was a HEAD request for the never-created isolated-control
node group. The operator separately confirmed both isolated node groups absent.
F046 now discovers live AKS first in Radius-only mode, checks every live cluster's
managed node-group ownership, and explicitly reports node groups without a live
AKS as uninspected/untouched. It does not interpret `Forbidden` as absence.
Full cleanup still checks every allocated node group and role. Both fix reviews
were clean, and 65 cleanup/export tests plus 40 subtests passed. A renewed live
preview is required before deletion.

The third read-only reset preview passed using committed source `2b3b7ff`.
It verified the three live clusters and listed the two never-created isolated
node groups as uninspected, then produced the expected child-app, child-cluster,
management-app deletion sequence. `radius-reset-execute` is running that exact
immutable source and metadata; reset completion is not yet claimed.

Fresh-deployment validation passed at 2026-09-09T19:30:26Z. `make check` passed
404 tests and 150 subtests, all 22 Bicep files, three extensions, Ruff, and
ShellCheck; 50 dependency tests were explicitly skipped. The foundation's
actual ARM what-if/validate passed with unchanged input hashes.

Generated Radius `.tgz` archives change packing metadata on regeneration.
After the full checks, actual-image inspection verified identical nested
`index.json`/`types.json` bytes for all three extensions rather than claiming
their changed archive digests implied changed code. The other inspected image
inputs still match. No image rebuild was used to conceal that distinction.

### Failed-Recipe orphan recovery and guard corrections

The first reset quiesced management, deleted the data Radius application, and
correctly stopped on its remaining Azure app-group resources. The failed Recipe
had not recorded the created Redis/endpoint/NIC outputs for owner deletion.
The operator checked the exact three resource IDs, all ownership tags, the
original Radius tags, and the one-to-one cache/endpoint/NIC relationship.
No group or cluster was selected for direct deletion.

The recovery-script security review found F048 (medium, 10/10): `assert` was
being used for operational safety checks. These now use unconditional
`cleanup.require`, construct an authorized `Cleanup`, and invoke its
`mutation=True` command path. The reset bootstrap's equivalent guards were
also corrected. The follow-up review found F049: changing source alone had not
rewired the generated executor. Its historical manifest is preserved; the
new `radius-reset-guarded-source` payload matches the repaired bootstrap and
both upcoming manifests reference it. The final security review was clean.
The direct walkthrough checked exact deletion order and generated-code wiring.
Twelve optimized/unoptimized script run-path cases passed with every external
process blocked. A permanent optimized-Python cleanup authorization regression
was added; the cleanup suite passed 50 tests and 15 subtests.

The guarded explicit operator recovery then deleted the private endpoint
(and its generated NIC), followed by the exact cache. Real Azure queries
verified all three absent and the data app group empty. The app group, both
child clusters, and the foundation were retained. The protected
`orphan-redis-recovery.json` records the completed action. This exceptional
recovery is not a normal Radius-cleanup success and does not retry the failed
tenant operation. The renewed guarded Radius reset still needs to complete.

### Reset completion and fresh management deployment

The guarded reset completed with `radius_resources_removed`, not a full-clean
claim. Independent operator queries verified all five app groups empty, every
child AKS absent, and management AKS plus the foundation retained. The three
temporary Reader assignments and the reset federation are now absent; the
original 21 harness grants and `demo-harness` federation remain.

Only the old, owned management application namespace was then cleared. It had
no remaining workload controllers, all Jobs/Pods were terminal, and its three
PVC/PV bindings matched the recorded claims, Delete reclaim policy, and exact
owned Azure disk IDs. The two failed acceptance records were checked against
their archived hashes before deletion. A wrong-UID dry-run returned the
expected Kubernetes 409, proving the deletion precondition was honored.
The exact-UID deletion removed the namespace without forcing finalizers.
At 2026-09-09T21:06:53Z, its three PVs and Azure disks were verified absent.
All five management Radius Deployments remained available. The state-clearance
security review found no vulnerabilities; the direct walkthrough checked scope,
archive preservation, and UID preconditions.

Fresh configuration now uses the inspected `ad031e2` images and locked Recipes;
the prior configuration is archived. `az quota list` and `az quota usage list`
reported 8/100 regional and DSv5 vCPUs used, leaving capacity for the required
32 additional vCPUs. `deploy-management-fresh` was submitted with new state and
is running. No old provisioning operation or failed evidence was replayed.
Fresh onboarding and outage acceptance remain pending.

Fresh management deployment completed. Management API, provisioner, and
challenge responder each have one available replica; the worker emitted
`provisioner_ready` at 2026-09-09T21:32:23Z. Real certificate-verified HTTPS
returned health 200, missing-key 401, and authenticated unknown-tenant 404.
A read-only query confirmed zero tenants and zero operations before acceptance.
`demo-acceptance-fresh` now runs the full fresh scenario from committed source
`058d878`, without continuation flags. Its first real operation,
`43bbaeb1-8250-49db-8d70-15ab73a9da73`, entered `control-cluster` at
2026-09-09T21:45:54Z. Tenant completion and outage results are not yet claimed.

### Local execution contract research (not a deployment)

Read-only research against Radius 0.60.2 confirmed that custom Terraform
Recipes execute inside
[`dynamic-rp`](https://github.com/radius-project/radius/blob/v0.60.2/pkg/dynamicrp/options.go),
with no separate executor Job. The
[chart](https://github.com/radius-project/radius/blob/v0.60.2/deploy/Chart/templates/dynamic-rp/deployment.yaml)
has image overrides but no arbitrary volume/security-context override; the
approved local overlay must patch that specific management Deployment.

The kind provider 0.11.0
[schema](https://github.com/tehcyx/terraform-provider-kind/blob/v0.11.0/kind/schema_kind_config.go)
supports explicit API ports and node mounts. Its
[implementation](https://github.com/tehcyx/terraform-provider-kind/blob/v0.11.0/kind/resource_cluster.go)
returns host-facing kubeconfigs and ignores accepted `timeouts` overrides:
legacy CRUD calls do not consume the configured timeout, and readiness uses a
hard-coded wait when enabled. Do not mistake that for an overall deadline.
The initial research claim that timeout blocks were unavailable was corrected.
Use supported HTTP module delivery, not an assumed `file://` loophole, and
verify sibling-node TLS with the real CA rather than copying upstream CI's
TLS bypass. These are source findings only; no local resources were created.
The direct walkthrough and independent security review of this plan correction
were clean. Docker socket access, generated child credentials, and the actual
local Recipe run path remain live verification gates, not passing results.

The fresh data deployment now contains both corrected ConfigMap Roles/Bindings
with their exact intended verbs and subjects, alongside Radius's generated
container Roles; the original naming collision is absent. Live inspection also
showed that Radius-generated roles grant namespace Secret `get/list`, so the
contracts now distinguish minimum ConfigMap needs from effective permissions.
The security review found no exploitable vulnerability in the reviewed HTTP
surface; it did not establish container-compromise credential isolation.
The read-only RBAC snapshot is retained in protected evidence.

### First successful tenant and Redis TLS measurement failure

Fresh operation `43bbaeb1-8250-49db-8d70-15ab73a9da73` actually succeeded and
reached `available`. Management observed the control record at
2026-09-09T22:31:55Z, and the data ConfigMap/application read passed at
22:32:05Z. The first tenant's complete infrastructure and configuration path
therefore ran successfully, including the two repaired deployment mechanisms.

The acceptance harness then failed in its read-only identity check:
`hasattr(connection, "ssl_cert_reqs")` is false on the installed Redis client
even though the connection is TLS. An independent live probe returned PONG,
`SSLConnection`, a negotiated `SSLSocket` using TLS 1.3, `CERT_REQUIRED`, and
hostname verification enabled. The false probe did not cause a provisioning
failure; no second tenant, pause, update, increment, or fault had run.

Failed evidence `acceptance-997de287a5134b7fb6b3adb95e850eb6.json` is retained,
SHA-256 `72837f4078a2cf6a9d8dc1c57dec7b90450c48ad6d5fd7006fb11c2e8d7c9a1c`.
The correction will inspect the actual negotiated socket and rerun the first
tenant's read-only checks before continuing the remaining scenario. It must
not replay admission, reset the successful operation, or relabel old evidence.

F050 is corrected without changing the runtime Redis client. The exact new
probe ran in the live data API container and returned the unchanged
`host/port/peer_address/tls` shape with `tls: true`. PING remains active under
optimized Python. Continuation accepts only the original first-admission
boundary or the exact ten-event first read-only verification boundary; all
first-tenant checks rerun before any later admission, mutation, or fault.
The failed record remains unchanged and is linked from fresh evidence.
Independent rubber-duck and security fix reviews were clean. The full harness
suite passed 165 tests and 155 subtests. Remaining acceptance is not yet passed.

The corrected continuation passed TLS and repeated first-tenant verification,
then stopped before the second admission while draining the data reconciler.
Its Pod had a 30-second termination grace, equal to the harness's entire drain
budget. Kubernetes recorded shutdown at 23:09:52Z; the restored replacement
was created at 23:10:25Z. The `finally` path restored one Running replica.
No second tenant was admitted.

F051 now uses the Deployment's configured grace (default 30 seconds, validated
as an integer from 0 to 300) plus a 30-second controller margin. A drain timeout
has its own error code and still restores the original replica. The required
30-second outage catch-up limits are unchanged, and no Pod is force-deleted.
The direct walkthrough and security review were clean; 168 harness tests and
162 subtests passed, including full-grace shutdown, custom grace, timeout
restoration, and invalid metadata before mutation. Live continuation remains
to verify the corrected pause before the remaining scenario.

### External AKS stop during acceptance

The acceptance monitor lost Run Command access because management AKS was
stopping, not because it read a failed harness result. Azure Activity Log
attributes the stop at 2026-09-10T00:06:02Z to the application
`MCAPSGovernance-AutomationApp`, not the operator, coordinator, harness, or
Radius identities. All five project clusters, including both newly created
isolated clusters, were Stopped/Succeeded and retained `SecurityControl=Ignore`.
Those tags did not prevent this separate power action.

Exact cluster IDs and ownership tags were rechecked before ordinary start
operations were requested for the five project clusters. No governance
automation, Azure Policy, permissions, or tags were changed. After restart,
inspect actual operations, harness evidence, and any fault restoration state;
do not automatically replay or relabel an interrupted provisioning operation.
The available metadata does not yet establish whether acceptance completed.

All five clusters subsequently returned Running/Succeeded. The three project
PostgreSQL servers were also found Stopped and were restored to Ready using
normal start operations after ownership checks; gateways remained Running.
The persisted operation table shows shared-a and shared-b succeeded, while
isolated-c correctly became `interrupted` at `control-certificate` with
`provisioner_restarted`. That state was not rewritten or replayed.

Recovered evidence `acceptance-32518dd5a31445bca734523eb2862fe3.json` (SHA-256
`779ad71d470d1e314e5a8a62770c31799c7e25435c38441b4c78e15074e206d2`)
proves the corrected pause, shared-b readiness while data was paused,
subsequent application, and unchanged shared infrastructure before isolated
admission. It then records `operator_interrupted`; no fault files exist.
All five token-free cleanup targets and three failed acceptance records are
archived under protected `governance-interruption-archive`.

A reviewed five-cluster owner-ordered reset preview passed. Its execution
stopped before deletion when two state disks no longer had the required tags.
Live PV/PVC/CSI bindings prove they are the known operator-state and
harness-state volumes. Disk activity shows a write by the same governance
application at 00:55:46-50Z; both disks now use Standard_LRS rather than the
StorageClass's StandardSSD_LRS setting. No intent is inferred from that change.
Only the original three ownership tags were merged back after independent
ownership checks; actual disk unique IDs, sizes, and current SKU were verified
unchanged. No policy, automation, or ownership guard was weakened. The direct
walkthrough and tag-repair security review were clean. The renewed strict
cleanup preview must pass before retrying the reset.
That renewed preview passed with every managed node group inspected and no
ownership bypass. `governance-reset-execute2` is running the same reviewed
immutable source. Completion, temporary access removal, and fresh state are
still required before the next full acceptance attempt.

That execution removed the shared data Azure resources but timed out deleting
its Radius Redis record after 1,800 seconds. The actual Radius logs identify
the persistent cause: no deletion API version could be found for
`Microsoft.Resources/tags`. Azure deletion activity and a fresh inventory
confirmed the data app group empty. Concurrent parent/child deletion also
produced transient canceled/superseded operations; this was not another tag
ownership failure or an automatic provider fallback.

F054 now lists only the independent Redis cache and private endpoint as
Recipe lifecycle roots. The database, NIC, DNS zone group, and NIC-tag operation
are still created; Azure removes those with their owning parents. TLS, private
access, password encoding, secure output, and existing-NIC creation ordering
are unchanged. Fourteen compiled-infrastructure tests and 19 subtests passed.
The direct walkthrough and independent security fix review were clean.
Existing canceled metadata still references the old output and requires an
explicit, separately verified cleanup action; changing source does not repair
that deployed record.

The corrected Recipe is published and tag-locked as
`redis:src-7e3cabeb0c5bdd2b0e26`, digest
`sha256:e3cc6f3fb59d8bbf331f060d2d9f52175aa1565ed0e16e365f97463c41966cea`.
An explicit recovery preview verified the old data app has exactly one
canceled Redis record, its Azure app group is actually empty, and its child
cluster has the expected management Radius owner. A reviewed one-off helper
is now removing only that empty child through the management Radius cluster
resource. It does not edit Radius storage, force resource state, or directly
delete AKS. Its immutable bootstrap and recovery code were checked against the
reviewed bytes before execution; completion remains to be verified.
That recovery completed through management Radius. Independent Azure queries
confirmed the shared data AKS, its node group, and its app-group resources
absent. The normal remaining-cleanup preview then passed and
`governance-reset-execute3` is running the unchanged owner-ordered path for
the remaining control applications, three children, and management application.
The empty-child recovery remains recorded separately from normal app deletion.

The remaining normal cleanup completed. Independent queries verified every
child AKS/node group and all five app-group resource sets absent, while
management and the foundation remained. All five temporary Reader leases and
the reset federation were removed without changing original harness access.
The exact old management application namespace was then removed with a UID
precondition, after its terminal workloads, three PVC/PV bindings, and archived
evidence were verified. Its three Azure state disks are also confirmed absent.

The next release checkpoint passed 420 tests and 196 subtests, 22 Bicep files,
three generated extensions, Ruff, and ShellCheck; 50 dependency tests were
explicitly skipped. Actual ARM validation passed at 2026-09-10T03:46:34Z with
unchanged foundation input hashes. The candidate's image/Recipe references,
actual image source hashes, and regenerated extension payload bytes were
rechecked. Fresh management and full acceptance remain the next live gates.
The verified candidate is now active and `deploy-management-lifecycle` is
running with a new operator-state volume. No old tenant operation, runtime
marker, credential file, or harness evidence was replayed into the fresh run.

`deploy-management-lifecycle` completed with three available workloads and
`provisioner_ready` at 2026-09-10T04:03:28Z. Real verified HTTPS returned
200/401/404 for health, missing key, and authenticated unknown tenant; a
read-only query confirmed zero tenants and operations. The new full
`demo-acceptance-lifecycle` run uses source `147c739` with no continuation flag.
Its first operation `60ed95e4-ac7e-4b45-9ff7-dac0f0c43574` entered cluster
provisioning at 04:19:34Z. Full tenant/outage success remains unproven.

All three fresh provisioning operations subsequently succeeded. The isolated
control record was ready at 05:53:57Z; its ConfigMap was created at 05:53:58Z
and control received `config_applied` at 05:53:58.851Z. The harness nevertheless
timed out at 05:54:27Z because the isolated endpoint had not yet been exported.
The recorded exporter scans were about 90 seconds apart, with the last scan
still observing the gateway's preceding state. This is F055, not slow runtime
configuration convergence. Keep the working tenants and fix initial discovery;
do not rebuild their infrastructure or weaken the 30-second outage requirement.

Live inspection also corrected the earlier F054 assumption. Radius 0.60
[appends implicitly created template resources](https://github.com/radius-project/radius/blob/v0.60.2/pkg/recipes/driver/bicep/bicep.go#L402-L458)
to the explicit output list. The fresh isolated Redis record therefore still
has `radiusManaged: true` for the tag extension and child resources, despite
the parent-only explicit list. The source/compile change alone was insufficient
and is not a completed lifecycle fix. This must be corrected before final
cleanup proof; no current working resource is being reprovisioned for that
investigation.

F055 now separates a pair's initial endpoint discovery (300 seconds total) from
ordinary client, application-convergence, and outage-recovery checks (30
seconds). Missing export state may wait; authentication, ownership, and schema
errors remain fatal. Regression coverage includes a 90-second export delay,
the shared discovery budget, and the unchanged convergence deadline.

The new `verify-existing` mode in both harness entrypoints performs no tenant
creation or admission pause. Its evidence explicitly declares
`scope: existing-tenants-only` and `admission_checks_performed: false`. It
validates the three successful operations and current topology, then runs
configuration/counter/authentication, timeline/idempotency, and both outage
checks. It rejects continuation input and cannot overwrite existing evidence.
The failed fresh run remains `acceptance-f4de91a0617c4cafa41b9b78536416a6`;
its genuine admission/reuse/immediate-child results are not relabeled as a
complete pass.

All 185 harness tests and 211 subtests passed, with Ruff and whitespace checks.
Independent F055 rubber-duck and security reviews found no issues. The
walkthrough checked actual API contracts and run-path dispatch, not just helper
tests. The security review retained the documented trusted-operator and
namespace Secret-reader limitations. No image or runtime deployment changes
are needed for this harness-only verification; its live result remains open.

### Azure functional proof, 2026-09-10

`demo-acceptance-existing` completed successfully. Its committed-source run
`80a3a427f3874169b6604800dd05580e` ran from 06:56:55Z to 07:03:57Z,
after source `3c66555` was committed at 06:54:36Z. This is explicitly
**existing-tenants-only**, not a fresh all-mode pass. The earlier failed
`f4de91a0617c4cafa41b9b78536416a6` retains genuine admission, shared reuse,
paused-data immediate-child readiness, and all three successful operation
observations. Its bytes and failed outcome are unchanged.

The new run verified four distinct child AKS resource IDs and five cluster
UIDs; shared versus isolated PostgreSQL/Redis instances; actual Redis TLS;
configuration, onboarding-ID and counter isolation; negative authentication;
complete paginated timelines; and idempotency across polling intervals.
Both parent faults blocked fresh and already-open database connections while
the child's local database path stayed available. Data continued serving and
incrementing counters for ten calls over 66 seconds in each fault.

| Measured outcome | Result |
|---|---|
| Management-link reporting recovery, including restoration | 16.225 seconds |
| Control-link latest-only catch-up, including restoration | 9.361 seconds |
| Data API replacement while control PostgreSQL was blocked | 6.684 seconds; configuration/counter retained |
| Fault policy restoration | 9.961 / 9.250 seconds; original policy sets preserved |
| Latest-only control recovery | Version 4 to 6; no intermediate version-5 application |

Both fault records say `verified_and_restored`, with successful restored
connection probes and exact original/restored policy equality. Independent
live AKS queries at 07:08:24Z confirmed no remaining fault policies and all six
shared control/data Deployments available. The protected
`functional-evidence-archive/` contains the three new records and unchanged
failed predecessor. Acceptance SHA-256:
`b75695073d4fb7d328d4705642be45ce40fb199ed4b4ea136f954844db46231b`.
Management/control fault SHA-256:
`65e690342e8949a266ec51c8e6e2c5f95e7d6b467bf478ee10675ef4af334952` /
`2b2537b59afaebfdac306fe41b7483f29740e443474dd7ec178bd27b7412775b`.
Predecessor SHA-256:
`997109b3e0f3c8f14028a1bdd51948729aed3491720aa83973cb73834adb091d`.

Independent functional-phase rubber-duck and security reviews found no issues
within these evidence scopes. They did not claim a fresh all-mode pass, HA,
production authentication isolation, Azure teardown, or local acceptance.
F054 and whole-environment cleanup remain open before the local phase.

F054's revised source removes the NIC tag extension and existing-NIC lookup
from the Recipe. The actual data deployment path now invokes a metadata Job
after both challenge and HTTPS deployments. It uses only the existing
per-plane Radius identity and app-group grant. The helper checks exact Radius
ownership, resource-group/cache/private-endpoint/subnet/NIC relationships,
merges required tags, verifies the readback and unchanged network properties,
and returns success only through the completed Job. No new role grant,
human token, Recipe-state edit, or old-resource migration is included.

`make check` passed 514 tests and 245 subtests, all 22 Bicep files, three
generated extensions, Ruff, and ShellCheck; 50 opt-in dependency tests were
explicitly skipped. Independent F054 security and rubber-duck reviews found no
issues. The walkthrough checked the pinned Radius/ARM contracts, actual
deploy-to-Job call path, image copy/dependency paths, completion, cleanup, and
failure behavior. The new image's actual contents/SDK execution and fresh
Redis creation, tagging, stored tracking, and deletion still require live
proof. Do not redeploy the working Redis records to attempt metadata migration.

The corrected Recipe is published and locked as
`redis:src-9e30d7ddd853894146c0`, digest
`sha256:6600de781e06b4045addd066de24c5656b1326b5504a4db225c96a3bddb5ab2b`.
Both images were built from `1b26a76`, then inspected in tokenless AKS Jobs:
23 API and 54 provisioner source/artifact hashes matched, including the new
helper, dependency manifests, and generated extension payloads. Both ran as
UID 10001, and the API image excluded privileged provider/operations code.
The exact candidate references and checks are in protected `images.json`;
the active configuration and running applications still use the prior images.

A separate read-only Job at 08:09:29Z executed the inspected image's actual
workload-identity SDK under isolated-data's existing `applications-rp` account.
Federated token exchange and an authenticated ARM GET verified the allocated
resource group and required tags. It performed no ARM mutation. This proves
the real SDK/identity path before the fresh lifecycle gate; it does not yet
prove NIC tag merging or Redis deletion.

The first submitted fresh lifecycle Job, `f054-redis-lifecycle-create2`, failed
at 09:10:33Z before the `baseline_verified` phase and before the Recipe-create
call. No Redis lifecycle result is claimed. The one-off gate had raised the
standard command logger threshold to CRITICAL, hiding its redacted diagnostic
and leaving only `command_failed` (F056). Restore ERROR logging rather than
guessing at the cause or changing Azure permissions. Preserve the failed Job
and its immutable payload; regenerate a new named payload after review.

F056 passed 165 focused tests, the direct rubber-duck walkthrough, and an
independent security review. The regenerated `create3` attempt exposed the
actual preflight error: Radius rejects a group name supplied both positionally
and through `--group`. This is F057; no Azure permission change is needed.
The wrapper already supplies the exact group, so remove only the duplicate
positional argument. The expected Radius group-ID assertion remains mandatory.
Add a regression through the actual execute/read/command wrapper path, not
only a mock of the high-level read. Both failed attempts and their immutable
payloads remain evidence, not successful lifecycle runs.

The initial lifecycle helper's independent preparation reviews were clean and
its 18 offline tests passed, but those mocks did not catch the duplicate
group argument. F057 adds the missing real-wrapper assertion; all 19 gate
tests now pass. The direct fix walkthrough and independent security review
found no issues. `f054-redis-lifecycle-create4` was regenerated from the
reviewed sources and submitted at 09:18:26Z. Its UID is
`2e828bbb-9539-46db-9cbb-73afd8eda01f`; the immutable candidate configuration
hash remains `f3b4efc1b40038b9a19c9aaefe6ef774f1bce3182085c3f226d2b1693ba9d8b0`.
The API/runtime images and active tenant environment configuration have not
changed during these gate-only corrections. Actual lifecycle success remains
pending the Job and absence checks, not its submission.

`create4` then stopped on the group-ID comparison. A scoped read-only Job
verified the expected cluster UID and returned the actual Radius group ID:
`/planes/radius/local/resourcegroups/radplanes`. Only path-segment casing
differs from the expected ID (F058). Use the gate's existing case-insensitive
ID helper, already used for its other Radius/ARM IDs; do not relax the group
name, workspace, cluster UID, or subscription checks. Add both actual-casing
acceptance and foreign-group rejection coverage before another fresh payload.

All 21 gate tests passed after F058, including actual execute/read/command
construction and refusal of a different group before mutations. The direct
rubber-duck and independent security fix reviews were clean. The regenerated
`f054-redis-lifecycle-create5` Job started at 09:24:03Z with UID
`33eea4ff-fdc1-4d2d-a581-121910fd2ac5`. Its candidate remains isolated from the
active tenant configuration. No failed gate record has been relabeled.

`create5` passed Radius preflight but its read-only baseline probe failed before
Recipe creation. Actual ARM reads identify F059: Managed Redis returns
`location: "Central US"` while its endpoint/NIC return `"centralus"`. The
subscription's `/locations` API confirms those are the display and canonical
names of the same region. The helper now accepts those exact two names; its
target configuration remains restricted to `centralus`, and other or malformed
regions still fail before any PATCH. The default test cache now uses the actual
display-name response rather than the previous unrealistic fixture.

The one-off probe also now prints only allowlisted provider error codes instead
of discarding them into the generic exception class. No exception text, tokens,
or response bodies are exposed. Because F059 changes privileged runtime source,
another committed image build and actual-content inspection are required; no
source overlay into the previous image is allowed.

F059 passed 237 focused provider/gate tests, Ruff, and whitespace checks.
The direct rubber-duck review used the real cache response and subscription
location metadata; the independent security fix review found no issues with
the exact alias or allowlisted probe diagnostics. Existing resource ownership
and foreign-region refusals remain enforced. No foundation or Recipe inputs
changed. Rebuild the committed privileged source before the next gate attempt.

The `31e131a` builds completed and tokenless AKS inspection again matched
23 API and 54 provisioner files/artifacts, including the exact region fix.
UID, API exclusion, extension members, and SDK imports passed. The new
provisioner reference is
`plane-provisioner@sha256:236b03faeba1ca2a735930618a59a1ed006fcabd7cfa50cb89bd487f169a2afd`.
All 21 gate tests passed against the newly verified manifest. The regenerated
`f054-redis-lifecycle-create6` Job started at 09:39:06Z with UID
`3049f59c-98d7-42a0-8595-32e14c19f863`. Its isolated candidate configuration
hash is `fe3198c4e5173d4b569451152c2ea9dabf799218b5dd8c2720ea453a9aef6732`;
no previous image's runtime code was overlaid. Live lifecycle proof remains
pending, and the working tenant configuration is unchanged.

`create6` proved the F054/F059 core behavior: a fresh Radius-created cache
`amr-shexztagitn52`, successful NIC tagging through the real helper twice,
idempotence, unchanged existing-resource fingerprints, and four unique tracked
resource IDs with no `Microsoft.Resources/tags`. Radius then deleted the fresh
Redis resource, application, and environment. Independent Azure inventory
confirmed the new cache/private endpoint/NIC absent; Radius lists confirmed
the Redis/application/environment absent.

The Job failed only at the last registry-auth SecretStore cleanup (F060).
Its actual resource has `application: ""`; the pinned CLI's
[`extractEnvironmentAndApplicationIDs`](https://github.com/radius-project/radius/blob/v0.60.2/pkg/cli/cmd/resource/delete/delete.go)
parses any non-null value as an ID before invoking deletion. The empty string
therefore fails in the client, not in Radius's resource lifecycle. The only
remaining gate record is `redis-lifecycle-registry-auth`, whose verified
output is exactly its same-named Kubernetes Secret in `radius-system`.

The reviewed `f054-finish` Job uses the authenticated native Radius API
documented by the pinned SDK's Kubernetes transport. It validates the exact
owner/type/output, the original cluster and existing-application fingerprints,
and absence of the former gate owners before deleting that one auth resource.
It does not edit Radius storage, change application fields, delete Azure
resources directly, or delete the backing Kubernetes Secret itself. It waits
for Radius and backing-Secret absence, then runs the original Azure
absence/fingerprint probes. Its direct walkthrough and independent security
review were clean. The original `create6` result remains failed; final cleanup
and postconditions will be a separate evidence record.

`f054-finish` completed at 10:13:38Z. Its result is
`lifecycle_postconditions_verified`: the exact Radius auth record and backing
Secret are gone, all three fresh Azure resource IDs are absent, and the
original cluster UID, Radius projection, and existing cache/endpoint/NIC
fingerprints are unchanged. This completes the F054 fresh-resource lifecycle
proof without rewriting `create6` as a passing all-in-one run. The old deployed
Redis records still contain their original unsupported tag metadata; removing
those during final teardown is a separate explicit owner-cleanup operation.

Final teardown now has five temporary Reader assignments on the exact live
AKS managed-node groups and one `radius-reset` federation for the existing
harness identity. Reader's actual permission is `*/read`, with no data actions;
no Contributor/Owner or subscription-wide grant was added. Seven no-cloud
guard tests, the direct walkthrough, and independent security review passed.
Independent readback verified the exact five new assignments and federation,
with the original 21 grants and original federation unchanged. The protected
`final-cleanup-access.json` journal records every intended/created ID and the
baseline before mutations. Remove these exact temporary entries after the
Radius-only phase and verify their absence before bootstrap teardown.

The final Radius-only cleanup package uses source `146ca9c`, the inspected
tool image, and eight allowlisted, token-free seed files. Its five cluster
UIDs match the successful functional run. The unchanged independent reset
bootstrap runs in `radplanes-system`, so deleting application namespaces
cannot kill its executor. The bundle, bootstrap, and seed bytes were checked
against the actual immutable ConfigMap in AKS. Fifty cleanup/optimization
tests and 15 subtests passed; the direct package walkthrough and independent
security review were clean. `final-radius-preview` is performing the strict
read-only ownership/terminal-state inventory. Execution requires that preview
to pass; no provider-delete fallback has been added.

The preview passed at 10:56:23Z with all managed-node groups inspected.
`final-radius-execute` then quiesced management API/provisioner and removed
the isolated data app's Azure resources. It stopped at 11:22:45Z because the
Radius app remained, despite the CLI returning zero. The actual-absence guard
therefore prevented a false cleanup success. Independent Azure inventory
confirmed that app group empty; a renewed strict preview found the remaining
legacy Redis record in terminal `Failed` state.

A separate reviewed recovery is checking the exact empty isolated data child,
its sole failed Redis record, the precise old NIC-tag reference, and its
management Radius cluster owner. It will not edit metadata or directly delete
AKS. Any removal must go through that verified management Radius owner after
the recovery preview passes. The immutable live bundle/bootstrap/helper/seed
bytes match the reviewed package. All other child resources and the foundation
remain pending normal owner-ordered cleanup; temporary access remains tracked.
