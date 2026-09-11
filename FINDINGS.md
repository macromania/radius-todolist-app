# Implementation findings and verification

This file records implementation reviews and observed results. Planned checks
are not passing checks. Runtime artifacts containing credentials stay out of Git.

## Status

Implementation started 2026-09-09. Azure admission, separately scoped functional/
outage proof, fresh Redis lifecycle, and teardown are verified. The local
executor gate and fresh five-cluster local acceptance passed, including both
parent outages and separately recorded datastore persistence. Both deployed
environments have been removed through their owners. Final local security review
found Radius's default data-API Secret access; the declarative runtime-identity
correction is implemented and reviewed, but still requires fresh live proof.

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
| F018 | 3 | High correctness | `src/plane_demo/providers/azure.py` | 374-380 (initial) | Environment registration omitted required private-registry authentication inputs | Resolved: corrected/reviewed inputs passed actual three-tenant onboarding and private Recipe pulls in the fresh lifecycle gate |
| F019 | 3 | High correctness | `src/plane_demo/providers/azure.py` | 932-936 (initial) | Management deployment omitted provisioner managed-identity client ID | Corrected and fix-reviewed; current coordinator tests pass |
| F020 | 3 | Medium correctness | `src/plane_demo/providers/azure.py` | 678-687 (initial) | Incomplete PostgreSQL Recipe state could be replayed before intent persisted | Pre-submission intent/refusal implemented and fix-reviewed; current coordinator tests pass |
| F021 | 3 | Medium correctness | `src/plane_demo/provisioning.py` | 95-127 (initial) | Malformed operator network fields were passed to infrastructure | Parsed IPv4/CIDR validation implemented and fix-reviewed; current coordinator tests pass |
| F022 | 4 | Medium security | `scripts/fault-parent-link.py` | 76-79, 127-140 (initial) | Caller working directory and arbitrary project identity defined the mutation boundary | Resolved: fixed/reviewed boundaries passed both real parent-link outages, exact restoration, and independent fault-policy absence checks |
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
| F060 | lifecycle gate | Medium compatibility | Radius 0.60.2 CLI | unbound SecretStore deletion | `resource delete` tries to parse the generated empty application string before calling the deletion API | Resolved: reviewed native owner-API cleanup and actual postconditions passed; gate helper finalized with 46 tests and clean direct walkthrough/security review |
| F061 | final cleanup | Medium correctness | Shared empty-owner recovery helper | managed-node inventory | The copied one-off helper queried a deleted node group after its temporary Reader grant was correctly removed with it | Resolved: existing live-AKS-first pattern retained every live ownership check; no extra grants; direct walkthrough/security review and actual corrected preview passed |
| F062 | 5 | High security | `harness/local/cluster-gate.py` | 645-655 (initial) | Secret-denial checks changed the target namespace but always impersonated `default:default`, missing the protected namespaces' default accounts | Resolved: corrected source/target/get-list-watch matrix, 18 entrypoint refusal cases, clean fix review, and live denial checks passed before child submission |
| F063 | 5 | High security | `operations/local/bootstrap.py`, local cluster Recipe | kubeadm patches | kind 0.31 emits kubeadm v1beta3, so v1beta4 patches were ignored: management Secret encryption was inactive and the child SAN patch would also miss | Management encryption resolved by reviewed fresh bootstrap and actual ciphertext proof before Radius; child SAN schema/loopback names corrected and mock-tested, live child TLS proof pending |
| F064 | 5 | High correctness | `harness/local/cluster-gate.py` | custom-resource submission | Generic `rad resource create` sends the legacy API version, so the custom cluster's 2025 API rejects it before provisioning | Use the exact authenticated Radius PUT with explicit registered version, then poll actual provisioning while observing the executor; 58 local tests and fix review pass; live retry pending |
| F065 | 5 | High correctness | `operations/local/common.py` | native PUT media type | `kubectl replace --raw` did not send the JSON media type required by dynamic-RP | Explicit JSON over project CA/client-certificate authenticated HTTP transport; no child/state created by the rejected request; live retry pending |
| F066 | 5 | Medium security | `operations/local/common.py` | unreleased transport candidate | Kubernetes ApiClient still followed redirects when connection retries were zero, allowing authenticated PUT replay | Candidate replaced before live use with HTTPX `follow_redirects=False`; real-client 301/302/307/308 same/foreign-origin and disconnect tests prove one request only; fix review clean |
| F067 | 6 | High security | `operations/local/runtime-images.py` | initial image inspection | Candidate-executed hash reporting did not bind inspection to the guarded build or verify administrative binaries | Require recorded immutable build IDs and trusted host-side filesystem/tool/Python-runtime hashing before import smoke tests; 17 focused tests and independent fix review pass; actual builds pending |
| F068 | 6 | High correctness | `operations/local/module-server.py` | ConfigMap mount handling | Rejecting all symlinks also rejects Kubernetes' legitimate atomic ConfigMap projection | Fixed: containment-checked AtomicWriter projection and checksum verification; startup/escape regressions and fix review pass |
| F069 | 6 | High correctness | Local provider and exporter | aggregate endpoint publication | Provider-generated `endpoints.json` conflicts with the exporter's strict ownership marker | Fixed: exporter is sole aggregate publisher; provider writes per-slot records only; ownership regression and review pass |
| F070 | 6 | High correctness | `harness/local/export-state.py` | child Radius ownership | Export validation expects different application/environment owners from the provider's `cluster-SLOT`/`provision-SLOT` records | Fixed: exact real owners validated, with producer-shaped positive and wrong-owner refusal tests |
| F071 | 6 | High correctness | `harness/test-e2e.py` | local PostgreSQL identity | Local acceptance expects service DNS/5432 instead of the approved node-private-IP/31543 output | Fixed: exact target node IP and port 31543, foreign node/port refusal, and unchanged Azure checks |
| F072 | 6 | High correctness | `operations/local/cleanup.py` | Terraform ownership inventory | Full-demo state includes `terraform_data.images[0]`, but cleanup accepts only the two gate resources | Fixed: exact third image-import owner and configured inputs checked; arbitrary extra state remains refused |
| F073 | 6 | High correctness | `providers/local.py` | environment registration | Radius 0.60.2 generic create does not support the supplied `--group` flag | Removed unsupported flag while retaining the exact seeded workspace scope; actual command-path tests and fix review pass |
| F074 | 6 | Medium correctness | Runtime image and acceptance source manifests | copied local overlay | Host-side image inspection found the copied `dynamic-rp-overlay.yaml` missing from both expected source lists | Include the actual copied YAML in worker build/acceptance provenance, never the API; original guard and failed build retained |
| F075 | 6 | High correctness | `providers/local.py` | Terraform init permissions | Root with only CHOWN cannot chmod a volume already owned by UID 65532 | Take ownership before mode changes, seed verified binaries, and hand ownership back last; no capabilities added; real restricted-container and management-RP checks pass |
| F076 | 6 | High correctness | Local provider/export/cleanup | Radius resource-ID comparisons | The real API returns lowercase `resourcegroups`, which mismatched newly added case-sensitive comparisons | Reuse exact case-insensitive Radius-ID equality across all local ownership surfaces; foreign IDs remain refused; 187 tests and actual management identity reads pass |
| F077 | 6 | High correctness | Local PostgreSQL/Redis/gateway Recipes | context scope validation | Terraform Recipe validators repeated the case-sensitive Radius group-path assumption | Normalize only Radius resource-ID casing; actual-wire fixtures and foreign-group refusal pass in all three Recipes; fresh immutable modules required |
| F078 | 6 | High correctness | Local data-plane prerequisites | persistent Redis permissions | Stock applications-RP can create StatefulSets but cannot create the Redis PVC | Add only namespace-scoped PVC lifecycle permissions to the existing applications-RP account in data namespaces; real denial, run-path tests, and fix review verified |
| F079 | 6 | High correctness | `harness/test-e2e.py` | PostgreSQL identity probe | Psycopg exposes SSL state on `connection.pgconn`, not `connection.info` | Correct supported property, preserve transport guard, and test the actual probe; real management-PG execution and fix review pass |
| F080 | 6 | High correctness | `operations/local/cleanup.py` | Radius inventory | Bare resource listing is environment-filtered and omits child wrappers and Core containers without explicit environment | Inventory validated applications explicitly; permit omitted container environment only under its validated parent; custom owners still require exact environment |
| F081 | 6 | High correctness | `operations/local/cleanup.py` | Post-deletion verification | CLI application-scoped resource listing requires the already-deleted application to exist | Use the authenticated native resource-group inventory after deletion; actual data deletion and the original failed reset record remain separately recorded |
| F082 | 6 | Medium safety | `operations/local/cleanup.py` | Complete owner absence | Application-only inventory can miss orphans; previously deleted known IDs can reappear during later removals | Cross-check complete native workload inventory, reject all remaining child Terraform state, track removed IDs cumulatively, and require final management native emptiness; regressions, fix reviews, and real reset pass |
| F083 | 6 | Medium verification | Private persistence probe | Pod termination | API-restart helper overrides datastore shutdown grace to five seconds while the candidate claimed graceful shutdown | Preserve the configured Pod grace and UID precondition; explicitly make no clean-exit/crash-durability claim; corrected before datastore mutation |
| F084 | 6 | Medium verification | Private persistence probe | Durable API snapshots | Reduced status projections omit persisted histories and report details | Compare complete management/control API fields and full paginated histories plus complete data responses; actual bound rerun passes |
| F085 | 6 | Medium verification | Private persistence evidence | Verifier provenance | Original supplemental result names only application source and omits post-state digests | Separate rerun binds verifier/tests/helpers/inputs to hashes and clean source, retaining post-API and post-database digests; independently verified |
| F086 | 6 | Medium security | Shared data workload and provider prerequisites | API Kubernetes identity | Radius generates namespace-wide Secret-reader permissions for the mounted data-API account, exposing parent `CONTROL_DSN` through the Kubernetes API | Use a distinct ConfigMap-get-only runtime account through the supported Pod override; shared Azure/local source and actual-token denial tests reviewed; fresh live proof pending |
| F087 | 6 | Medium verification | Private PostgreSQL persistence snapshot | Security catalogs | Role/RLS flags omit policy predicates, memberships, and object ACLs | Include ordered policies, memberships, schema/relation/column/function privileges and definitions; all three real PostgreSQL queries and bound persistence rerun pass |
| F088 | 6 | Medium correctness | `operations/local/cleanup.py` | Verification path input | Documented repository-relative record path was interpreted as state-relative twice | Normalize only the known `.state/local` prefix, then retain private-path/traversal/symlink guards; exact documented command and entrypoint regression pass |
| F089 | 6 | Medium verification | Data API permission probe | Named grants | Unnamed authorization reviews miss resource-name-limited token minting or ConfigMap mutation grants | Add named account/map/parent-Secret reviews and ConfigMap watch checks; executable probe and named-grant refusal tests and fix review pass |

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

The F060 reusable gate helper is finalized and independently reviewed; all
46 gate tests pass. It verifies the exact public Radius owner route and waits
for both Radius-record and named-Secret absence. The source hash is
`a5f26810feecc467b44e5894861e57db16c332bee61d6d3b483877d3a7ad6177`.
No new Redis was created to repeat the already verified postconditions, and
the failed `create6` payload remains immutable.

The isolated-child recovery preview passed with the exact old NIC-tag ID,
empty Azure app group, sole failed Redis record, and management Radius owner.
`final-isolated-recovery-execute` is now removing that empty child through its
owner. Actual AKS/node-group absence and the remaining normal teardown still
need verification.

That recovery completed through management Radius at 11:59:13Z. Independent
Azure reads at 12:02:39Z verified the isolated data AKS and managed-node group
absent and its app group empty. Its exact temporary Reader assignment was
removed with the node group; independent role readback confirmed that only
that lease disappeared and the original 21 grants remain.

The remaining normal cleanup preview passed. `final-radius-execute2` started
at 12:06:12Z against the unchanged reviewed source, proceeding to shared data
and the two control applications before their cluster owners. A permanent
regression now exercises the actual cleanup path when the CLI returns zero
but an app remains after its Azure resources are removed; it proves that no
cluster-owner or provider deletion follows. All 51 cleanup tests and 15
subtests passed. This adds no production fallback or behavior change.

The second normal execution removed the shared data app's Azure resources
but again stopped on its legacy Radius app/Redis metadata at 12:29:50Z.
The shared app group is independently empty. Its first explicit recovery
preview exposed F061: unlike the normal Radius-only path, the copied helper
still queried every allocated node group, including the deleted isolated
data group whose Reader assignment was already gone.

The helper now follows the existing normal-cleanup pattern: discover live AKS
from verified non-node groups, then verify every live cluster's node group.
It does not treat Forbidden as absence, omit any live ownership check, or add
broader permissions. Six existing Radius-only regressions covering this
pattern passed, as did the direct walkthrough and independent security review.
The newly named immutable payload's actual bytes were verified before
`final-shared-recovery-preview2` started. The prior failed preview is retained.

That corrected preview passed with the exact shared NIC-tag ID, sole failed
Redis record, empty app group, and management Radius owner.
`final-shared-recovery-execute2` started at 12:47:41Z, removing only the verified
empty shared data cluster through management Radius. Its actual absence,
remaining control-plane cleanup, temporary-access removal, and bootstrap
teardown still require verification.

The shared empty-child recovery completed through management Radius at
12:56:12Z. Independent Azure reads at 12:58:43Z verified that AKS and its
managed-node group absent and the app group empty. Its temporary Reader lease
also disappeared with the node group; the original 21 grants plus three
remaining temporary Readers were independently verified.

Both data clusters are now removed. The next normal owner preview passed,
and `final-radius-execute3` started at 13:01:51Z to remove the two control
applications/clusters and then management's applications. Bootstrap
management AKS, shared foundation resources, and final role cleanup remain
separate pending steps. The verified images are cached locally before registry
deletion; no local cluster or application has been started.

`final-radius-execute3` completed at 13:38:03Z with
`radius_resources_removed`. Independent Azure verification at 13:42:00Z found
all four tenant AKS clusters and their node groups absent, and all five app
groups empty. The last temporary Reader and `radius-reset` federation were
then removed; readback at 13:43:05Z confirmed all temporary access absent and
the original 21 grants/federation unchanged. The project-scoped Docker registry
login was also removed after caching the verified images.

Only bootstrap resources remain. Their explicit provider-only preview passed
strict ownership/custom-role checks and contained exactly one AKS deletion:
the bootstrap management cluster. This is not an automatic fallback from a
Radius failure: the Radius-owned phase and actual absence were verified first.
The direct walkthrough and independent security review were clean. Execution
rechecked all child/app absence and temporary-access removal before starting
the existing provider-only cleanup path. Its warning remains visible; no
child AKS is being deleted directly. Final group/role absence still needs proof.

### Azure phase closed, 2026-09-10

Bootstrap deletion removed all remaining owned groups and the management AKS.
The first final check reported custom-role catalogue leftovers. Exact GETs
returned `RoleDefinitionDoesNotExist`, and a later run of the **unchanged**
full verifier also confirmed absence. No ownership check was relaxed and no
additional resource scope was deleted to resolve that inconsistent observation.

The persisted independent result,
`final-cleanup/final-verification.json`, is `clean` at 14:29:36Z: all owned
resource groups, project custom roles/assignments, and active project-tagged
resources are absent. The vault remains soft-deleted, not purged, with scheduled
purge at 2026-09-17T14:14:19Z. Its protection was not disabled. The archived
Azure state is historical evidence and must not be treated as active endpoints
or reused cluster identity.

Final `make check` passed 522 tests and 245 subtests, all 22 Bicep files, three
generated extensions, Ruff, and ShellCheck. Fifty opt-in dependency tests were
explicitly skipped. Independent Azure closeout rubber-duck and security reviews
found no blockers within the recorded ownership/evidence scopes. They retained
the separate failed/fresh and successful/existing-state records and the
documented demo limitations. This completes Azure, not the overall plan:
local implementation and its five-cluster acceptance are next.

### Local executor gate source

The bounded one-child gate now has a Terraform kind Recipe, a derived management
`dynamic-rp` executor image, explicit socket overlay, immutable in-cluster HTTP
module archive, protected access Secret/state, bootstrap operations, and a
separate harness. Shared application declarations, Azure Recipes, and custom
type schemas are unchanged. This is not the full `LocalProvider` or a live
local deployment.

The initial independent walkthrough found no static integration blocker. The
security review found F062: a different target namespace does not change an
impersonated service account's identity. The fix checks `default` accounts from
all three relevant namespaces against both protected namespaces for `get`,
`list`, and `watch`, preserving the original outsider check and adding
cross-namespace coverage. Each of 18 granted-permission test cases invokes the
real gate entrypoint and stops before child creation. The direct fix walkthrough
and independent security fix review were clean.

All 54 local Python tests, two Terraform mock-provider tests, Ruff, ShellCheck,
and Terraform format/validation passed. CI and `make check` now include these
cloud-free surfaces with pinned Terraform 1.15.8. No image build, container
creation, kind bootstrap, port binding, or state-lifecycle proof has run yet.
Actual image contents, Docker Desktop socket permissions, encrypted state,
child TLS/networking, PostgreSQL/Envoy, and Radius-owned deletion remain live gates.

The integrated `make check` passed 576 tests and 245 subtests, with 50 explicit
dependency skips, all Bicep/type checks, both Terraform mock-provider tests,
Ruff, and ShellCheck. The CI change adds only checksum-pinned Terraform setup
and the same Make entrypoint; it adds no cloud credentials or live provisioning.

The native arm64 executor/operator images were built and inspected. The derived
`/dynamic-rp` binary matches the pinned upstream bytes; UID 65532, Docker
29.2.1, Terraform 1.15.8, Radius 0.60.2, and kubectl 1.35.7 passed actual
execution checks. Management kind bootstrap then succeeded, and Radius's
management-only overlay proved the same Docker daemon and inspected binary.
Installation stopped on the required real etcd encryption check.

F063 is a live schema mismatch, not a missing Secret: the Secret existed but
its stored value lacked the encrypted prefix. The API server had no encryption
flag, and its generated `/kind/kubeadm.conf` used v1beta3 with map-shaped
`extraArgs`. The pinned kind source confirms that template even for Kubernetes
1.35. Both the management encryption and child SAN patches now use v1beta3.
The child SAN list preserves kind's loopback names as well as the explicit
internal name, so applying the patch does not break the provider's host-facing
readiness connection.
Management also creates a harmless probe Secret and verifies its actual etcd
ciphertext before publishing bootstrap readiness or allowing Radius installation.
An old readiness marker without that proof is refused.

All 56 local Python tests and both Terraform mock tests passed after the fix.
The direct walkthrough checked actual generated configuration and pinned kind
source; the independent security fix review was clean. No Terraform state or
child cluster was created. A separately reviewed reset preview verified the
exact failed management Docker ID, no children/state, and unchanged pre-existing
containers. Only that operator-owned bootstrap will be removed and recreated
with a fresh key; failed non-secret records are archived and no plaintext Secret
contents or etcd dump are exported.

The complete check passed 578 tests and 245 subtests, 50 explicit dependency
skips, and both Terraform mock tests after the final patch/SAN correction.
The final security follow-up confirmed that preserving the loopback SANs does
not weaken the child's explicit CA/name verification. Existing inspected
images are unchanged; only bootstrap and published Recipe source changed.

The exact failed management node was removed through kind after reviewed
ownership/no-child/no-state checks. Non-secret failure records are retained
under `history/unencrypted-20260910T163442Z`; the old key and kubeconfig were
removed only after node absence and unchanged-other-container proof. Fresh
bootstrap then passed the harmless-Secret ciphertext check, and Radius
installation passed its actual encryption, socket/daemon, binary, and module
service checks. No image rebuild or live API-server patch was substituted.

The first child request, run `47e45daf2919`, passed the live Secret-denial checks
but was rejected by Radius: the generic create CLI selected
`2023-10-01-preview`, while `Demo.Platform/clusters` registers
`2025-08-01-preview` (F064). No child container or Terraform state was created.
The gate now submits once through the authenticated native Radius API with
the exact version, retains all absence/identity checks, and waits for actual
`Succeeded` state while observing the kind-provider process. HTTP acceptance
alone cannot pass; failed/unknown states and the 900-second deadline stop the
gate without replay or automatic cleanup. Read/list/delete retain their
version-discovering CLI paths.

All 58 local Python tests pass, including exact PUT routing, accepted/creating
state polling, terminal failure propagation, and pre-creation security refusals.
The direct walkthrough traced the pinned CLI client, and the independent
security fix review was clean. The original rejected run remains failed.

The version-correct `kubectl` request was also rejected before admission, run
`973c3b65ee85`. A bounded provider-log read identified `unsupported Content-Type`
(F065); no child resource, Docker container, or Terraform state exists.
The replacement loads only the private project kubeconfig/context and its
private CA/client-certificate files, requires the exact TLS management endpoint,
and sends JSON through HTTPX with environment proxies and redirects disabled.
It does not change credentials, authorization, or the Radius ownership path.

Security review of an intermediate, unreleased Kubernetes ApiClient transport
found F066: disabling connection retries did not stop redirected PUT replay.
That candidate was replaced before live execution. Tests use a real HTTPX
client with a simulated server response to verify that 301, 302, 307, and 308
responses at both same-origin and foreign URLs produce one request and an
explicit failure. A transport disconnect is also not retried. All 70 local
Python tests and the direct walkthrough/security fix review passed. The failed
gate records remain unchanged; live JSON/mutual-TLS admission still needs proof.

The integrated check passed 592 tests and 245 subtests, with 50 explicit
dependency skips and both Terraform mock-provider tests. The final transport
uses the project's existing mutual-TLS credentials, not a new identity or
privilege grant; no container image or installed Recipe change is required.

### Local milestone 5 passed, 2026-09-10

Run `d3fd13ce214f` passed from 18:14:35Z to 18:18:18Z, after source `9190bb0`
was committed at 18:14:18Z. It observed the actual kind-provider process in
management `dynamic-rp`, verified Radius-owned Terraform state and encrypted
Secrets, and checked the child's original CA and explicit server name, including
a real rejection of an incorrect name. Child Radius and its workload were
deployed. The child reached password-authenticated parent PostgreSQL through
internal port 31543, rejected a wrong password, and served the run-specific
Envoy response through reserved loopback port 35491.

The same run deleted the child through Radius and verified child Docker,
Terraform state, and access-Secret absence, with the original containers
unchanged. Independent checks confirmed those absences and management
readiness. The original run hash is
`3b805b31b35844a4e859f41d2bb670739c609a78d6125938cef52a2b99544b5e`;
`evidence/milestone5-proof.json` records the source hashes, timestamps, and
actual image inspection. Failed earlier runs were not relabeled.

Independent live-phase rubber-duck and security reviews found no blockers in
this bounded proof. Management remains intentionally for the next phase.
The full local provider, persistent PostgreSQL/Redis/gateway Recipes,
five-cluster shared/isolated scenario, and both outages are still pending.

### Full local implementation in progress

Local Recipes, provider wiring, and acceptance/outage support are being added
behind the proved cluster mechanism. Application image preparation remains
separate: native API and privileged provisioner images are built locally, not
pushed to an external registry. Child image loading belongs to the Radius
cluster Recipe; the public API and runtime provisioner receive no Docker socket.

The image-boundary review found F067 before any new runtime build. Inspection
now requires the successful build's exact immutable image IDs. Trusted host
code hashes exported, never-started container filesystems: source files,
vendor-pinned administrative binaries, extension payloads, and the interpreter/
dependency files. The API privilege exclusion is also checked from that export.
Only afterward do image-executed import smoke checks run by immutable ID.
Seventeen tests cover missing build evidence, changed tags/runtime, replaced
administrative tools, host-side hashes, and API exclusion. The direct walkthrough and independent security
fix review were clean. This source verification is not a local deployment claim.

The host-side export parser also ran against the retained immutable Azure API
image without starting its container. It matched all 23 source/manifest files
and hashed the interpreter/dependency tree. This checks the actual export/read
mechanism only; it is not a new native runtime image build or full-local proof.

The assembled local implementation passed 779 Python tests and 254 subtests,
50 explicit dependency skips, and 21 Terraform mock-provider tests. Its
independent full-phase security review found no new vulnerabilities, but the
rubber-duck review reproduced five producer/consumer mismatches (F068-F072).
Passing mock tests did not prove those contracts. Corrections remain required
before building or deploying the full local runtime; no live tenant admission
has been attempted with this implementation.

F068-F072 are corrected against the actual producer contracts, not relaxed
aliases. The module server accepts contained atomic ConfigMap projections;
only the exporter publishes aggregate endpoints; child owners match
`provision-SLOT`/`cluster-SLOT`; PostgreSQL identity matches the exact private
node and 31543; cleanup recognizes only the configured image-import Terraform
resource in addition to the kind cluster/access Secret. The fix walkthrough
and independent security review passed.

An additional actual CLI-help check found F073 before execution: generic
`resource create` has no `--group` flag. Environment registration now uses its
already explicit, verified workspace group rather than an unsupported flag;
custom cluster Bicep deployment keeps its supported group flag. Command-path
regressions and the independent security fix review passed. The focused
provider/export/cleanup/Recipe integration suite passed 235 tests and 13 subtests.
These corrections require new native image builds and a fresh full acceptance
run; they do not change the earlier one-child proof.

The integrated source now passes 847 Python tests and 258 subtests, with
50 explicit dependency skips, all 23 Bicep files, three generated extensions,
21 Terraform mock-provider tests, Ruff, and ShellCheck. The shared application
declarations, dependency modules, type schemas, SQL, and public API image
allowlist remain unchanged. These are source/readiness checks; the next
boundary is a native image build, host-side content verification, management
deployment, and actual five-cluster acceptance.

The first native build completed both Docker images but correctly stopped at
the host-side content guard. A second trusted export diagnosed exactly one
extra copied file: `operations/local/dynamic-rp-overlay.yaml`; there were no
missing or changed files (F074). Both worker source manifests now include that
real input, with a regression confirming it remains excluded from the API.
No image was loaded or deployed from the incomplete build, and its immutable
tags are not overwritten. The corrected committed source will receive new tags.

The corrected `114e118` native images passed trusted host filesystem, tool,
interpreter, extension, and source checks, followed by import smoke checks.
They were loaded into management only. All four new immutable module servers
became ready. Full setup then stopped at the applications RP's Terraform init
container, before any tenant, database, PVC, or child creation.

F075 was a Unix ownership ordering error: the init had only CHOWN, not the
capability to chmod another UID's files. The fix takes ownership of the fixed
Terraform paths before mode changes and returns directory ownership last.
It adds no capability, socket, host mount, or credential access. The exact init
ran twice in a real restricted Linux container, with only CHOWN and a
UID-65532-owned tmpfs, and produced the expected final private modes/ownership.

Seventy-one provider tests and the direct walkthrough/security fix review
passed. A separately guarded, child-free/state-free operational repair applied
that same initializer to management's applications RP. The live process now
runs as UID/GID 65532 and executes Terraform 1.15.8 from its mode-0700 layout.
Failed setup inputs and inspected image records are retained under
`history/terraform-init-permission/`. Only the two unchanged setup attempt files
were cleared after those checks; no API onboarding operation was replayed.
The privileged source must be rebuilt before proceeding to child provisioning.

The rebuilt `fe753ca` images passed host-side inspection and management loading.
The next setup passed the repaired Terraform stage and reached environment
registration, but its final identity check exposed F076: Radius returned the
correct group/environment with lowercase `resourcegroups`. The new local
provider, exporter, and cleanup now share exact case-insensitive Radius-ID
comparison, as the earlier gate already required. Kubernetes names, slots,
network endpoints, credentials, image IDs, and namespace UIDs retain their
existing checks; a different group/owner is not accepted.

The provider/export/cleanup suite passed 187 tests and 13 subtests, including
both wire spellings through complete cleanup and foreign-owner refusal. The
direct walkthrough and independent security fix review passed. Actual
management group/environment reads now pass the same guard. The second
data-free setup attempt is archived under `history/radius-id-casing/`; checks
again confirmed no child, Terraform state, database, PVC, or initialized plane
credentials before clearing only its two attempt files for the rebuilt source.

The next complete setup succeeded. PostgreSQL's first Recipe invocation then
exposed the same wire-casing assumption inside all three application Recipe
validators (F077). Those validators now compare canonical lowercase Radius IDs;
slot, application, namespace, type, private address, port, and credential checks
remain unchanged. Actual-wire fixtures and foreign-group refusal tests pass:
24 Terraform mock-provider tests and 51 Recipe/publication tests. The direct
walkthrough and independent security fix review were clean.

The failed PostgreSQL resource created no managed Terraform resources or
StatefulSet. Its normal Radius deletion also failed on the old module's
validator. No state edit or force deletion was attempted. A separately reviewed
bootstrap reset preview verified the exact management node, no children, no
application Pods/StatefulSets, one unbound Pending provisioner PVC, one empty
Terraform backend, and no initialized database. Only that operator-owned,
empty bootstrap may be removed and recreated. Failed records are archived;
unrelated containers, images, network, and global contexts must remain intact.

Fresh management from `a5af309` deployed successfully with persistent PostgreSQL
and provisioner storage, HTTP health 200, missing-key 401, and
`provisioner_ready`. The full `all` run `49f9dca7593c4e3c8ff7e201544647e8`
began at 08:12:35Z on September 11. Shared-a was admitted, both shared kind
clusters were created through management Radius with image import, both child
Radius installations completed, and control PostgreSQL/API deployment passed.
The operation failed at `data-application` at 08:22:09Z: the Redis Recipe's PVC
creation was denied to `radius-system:applications-rp` (F078). The run remains
failed; no second or isolated tenant, configuration matrix, or outage was run.

Real authorization reads confirmed PVC create was denied while StatefulSet
create was already allowed. The fix therefore grants only PVC lifecycle verbs
through a Role/RoleBinding in each exact data namespace. It adds no cluster-wide
rights, Secret permission, runtime API authority, or Docker access. Tests invoke
prerequisites for all five slots and verify only data receives the exact grant;
the direct walkthrough and independent security review passed.

The exporter separately stopped because a late agent-only optional hardening
edit changed the source during acceptance. Its source guard was correct. The
exact patch was preserved in protected history, only those agent edits were
restored to the deployed commit, and verified export resumed without a tenant
replay or guard waiver. It did not cause the later Redis permission failure.

Before another admission run, an installed-client check also caught F079.
The actual PostgreSQL probe now uses `connection.pgconn.ssl_in_use`, retains
its local-only DSN/TLS checks, and passes execution inside the running management
API. It reports the real private node endpoint and no credential values.
The focused provider/harness suite passed 126 tests and 13 subtests; the direct
walkthrough and security fix review were clean. Failed operation/evidence and
the existing shared resources remain pending explicit owner-ordered reset,
not automatic provisioning recovery.

The exact missing PVC Role/RoleBinding has now been applied to the existing
shared data namespace only, after live node-ID, cluster-UID, namespace-UID,
project-label, and name-absence checks. Actual authorization reads confirmed
get/create/delete PVC permission for applications RP. This permits its normal
owner cleanup; it did not replay the failed deployment or grant StatefulSet,
cluster-wide, or application-API privileges. Protected
`diagnostics/redis-storage-permission-proof.json` records the scope.

### Failed local admission reset, 2026-09-11

The normal cleanup contract still requires all five exported targets. A separate
protected, one-off reset was reviewed for exactly the failed management/shared
pair. Its preview bound the original failed operation, null pair inventory IDs,
three Docker/cluster/namespace identities, original image references, Radius
application owners, Terraform backend UIDs/lineages/serials, access Secret UIDs,
and the exact PVC-only Role. No onboarding operation was replayed or adopted.

Live inventory exposed F080: bare `rad resource list` selects the current
environment and omits Core containers whose environment exists only on their
application. Application-scoped enumeration fixes discovery without accepting
a wrong explicit environment or a custom resource without its exact owner.

The first reset began at 09:55:21Z. It quiesced management, deleted the shared
data application through child Radius, then stopped at F081: the CLI cannot
list resources for an application that no longer exists. Its failed journal
and one-shot marker were retained. The continuation independently proved the
data application, its Radius records, backends, StatefulSets, and PVCs absent;
it did not repeat that deletion or restart management.

The independent correctness review found F082 before continuation. The native
resource-group endpoint returns all resource IDs/types regardless of application
or environment, including records outside known applications. Preflight now
compares that complete workload inventory with validated owners. Child deletion
also requires no application records, workload records, or Terraform backend
Secrets. Environment/application definitions and retained ARM deployment-history
records are distinguished from workload owners, not claimed deleted. A follow-up
review reproduced reappearance of an earlier deleted ID; cumulative removed-ID
tracking and a final empty native management inventory close that gap.

The direct fix walkthrough, independent security fix reviews, 81 tracked cleanup
tests, and 29 private reset/continuation tests passed. Regressions exercise actual
command paths, complete-inventory/foreign/pagination refusal, orphan state after
app deletion, reappearing known IDs, unchanged failure records, and no automatic
continuation replay.

The separately previewed continuation finished at **10:33:36Z**: control
application removal, both child deletions through management Radius, actual
Docker/backend/access absence, empty wrapper removal, management application
removal, then only the bootstrap-owned management kind cluster. Independent
verification at **10:34:17Z** confirmed all three original project node IDs and
project node names/labels absent, with all **13 unrelated containers retained**.
Protected evidence is `evidence/reset-49f9dc-verified.json` and
`recovery-49f9dc/remaining-journal.json`; the latter hashes to
`b3cbd34bc1edcc09696a0fd59b3666bdf06e0fb3f332286555d20495e68b2686`.
The original acceptance and first reset still say failed. This is cleanup proof,
not passing full-local acceptance, persistence, or outage evidence.

After independent absence proof, 17 exact obsolete bootstrap/deployment files
were archived and retired for a fresh environment. The previous runtime image
review was also archived; gate evidence, failed runs, credentials history, and
the reset journals were retained. No broad state-directory cleanup occurred.
The final source checkpoint passed `make check`: **872 tests, 263 subtests,
50 explicit dependency skips, 23 Bicep files, 24 Terraform mock-provider tests,
Ruff, and ShellCheck**. Fresh images and a new full acceptance run remain required.

### Fresh full local proof and final security correction, 2026-09-11

Fresh native arm64 API/provisioner images from committed `786c553` passed
host-side filesystem, administrative-tool, interpreter, extension, and source
inspection before loading. Fresh management used a newly verified encryption
key and cluster identity. Real HTTP health/authentication, worker readiness,
bound PostgreSQL/provisioner PVCs, and zero tenant/operation rows were checked.

Full `all` run **`5ff3571f29664fbc9639b175c0b5e870` passed**, from
**10:48:17Z to 11:17:50Z**, after its source commit. It created two shared tenants
and one isolated tenant through management Radius, with exactly five distinct
clusters. It proved shared reuse, isolation, paused-data-reconciler immediate-child
readiness, 17 workload source/image observations, configuration/counters,
authentication, complete timelines, and repeated-poll idempotency.

Both real parent PostgreSQL links were blocked in the reconciler Pod network
namespace. Data remained available for 65.35 and 64.60 seconds; management
reporting recovered in 10.34 seconds. A data API replacement while its parent
remained blocked recovered in 7.27 seconds; control reconnection applied only
the latest requested version. Original network rules were restored exactly:
physical restoration took 4.23 seconds for management and 2.61 for control.
This is one passing fresh run, not a relabeled continuation of failed `49f9dc`.

The supplemental persistence probe was corrected for F083-F085 and F087 before
its final, separately named run. Bound proof
`persistence-5ff3571f29664fbc9639b175c0b5e870-bound.json` passed from
**11:46:11Z to 11:47:24Z**. All three PostgreSQL and both Redis Pods acquired
new UIDs while retaining their StatefulSet, PVC, and PV identities. Complete
API state/history/counters/configuration and PostgreSQL system/security catalogs
matched before and after. Per-instance recovery was 9.36-12.76 seconds. Verifier,
17 tests, imported helpers, configuration, inspected images, and acceptance
evidence are hash-bound; post-state/database digests were independently checked.
The result hashes to
`eab1393fab6eb526ae5c4c78349681e1f78a3e3dc7278908cd96f1c3508b0e60`.
The earlier unbound result remains unchanged. This is Pod-replacement persistence,
not a clean-process-exit, forced-crash, or HA claim.

A live five-node resource sample measured roughly 9.22 GiB across the kind nodes
on the existing 10-CPU, 23.43-GiB Docker Desktop daemon. No daemon configuration
or unrelated project was changed; this is one observation, not a capacity limit.

The phase correctness review accepted the full scenario evidence but requested
the stronger supplemental provenance. The security review found F086 by making
an actual authenticated Secret request from the data API Pod: absent environment
credentials were not sufficient while Radius granted its token Secret access.
The original full acceptance result therefore does not prove this security
boundary. The reviewed fix preserves Radius's resource/account names and Redis
connection but selects a precreated `data-api-runtime` account with ConfigMap
`get` only. Radius builds its generated RoleBinding before the supported Pod
override; no Role patch, timing window, new controller, or Radius fork is used.
Both Azure/local prerequisites and local export agree. Acceptance now checks the
actual Pod identity, authenticated Secret GET/list 403 responses, and broad/named
permission denials, including the F089 token-minting case.

Before changing image inputs, the original five-cluster environment was removed
using the normal operator, not the partial-reset helper. Cleanup
**`e29e3d2caf0e`** ran **11:47:46Z-11:53:00Z** in Radius owner order.
Independent verification passed at **11:56:47Z**, retaining all 13 unrelated
containers. F088 was then corrected and the exact documented relative-path
verification passed at **12:03:46Z**; its output is retained in
`evidence/cleanup-e29e3d2caf0e-verified.log`.
Fresh images and a new run must prove the final API-identity correction before
phase close. Historical Azure proof predates this shared-source correction;
no new Azure deployment is claimed.

The final identity/probe source passes **890 tests, 264 subtests, 50 explicit
dependency skips, 23 Bicep files, 24 Terraform mock-provider tests, Ruff, and
ShellCheck**. Azure continuation fixtures exercise the same runtime-account
contract; no compatibility bypass was added. Only 36 named inputs from the
verified deleted deployment were archived under
`history/acceptance-5ff3571f29664fbc9639b175c0b5e870/`, including the two restored
fault journals. Their original acceptance references describe their former
paths; archived bytes and retirement hashes preserve the evidence without
letting obsolete Pod identities become active fault-cleanup inputs.
