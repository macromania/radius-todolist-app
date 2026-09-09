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
| F004 | 1 | High correctness | `infra/bootstrap/federation.bicep` | 11 (initial) | Concurrent writes to one identity's federated credentials can return 409 | Fixed with serial loops; fix walkthrough passes, live deployment pending |
| F005 | 1 | High security | `infra/bootstrap/platform-access.bicep` | 150-167 (initial) | Tenant-reachable gateway/issuer identities can read or replace other planes' certificates in the shared vault | Resolved in source: exact object grants, both fix reviews pass; live cross-plane denial still required |
| F006 | 1 | High correctness | `scripts/install-radius.py` | 38-59 (initial) | Existing workload-identity label means a patch does not restart pods after identity annotations | Fixed; explicit rollout restart, regression, and both fix reviews passed; live pod projection remains a deployment gate |
| F007 | 2 | Medium correctness | `src/plane_demo/control_api.py` | 35-52 (initial) | A delayed older-version success hides failure of the current desired version | Resolved: reproduced before fix, 37 related real-PostgreSQL integration tests passed after, both fix reviews clean |
| F008 | 3 | High correctness | `infra/radius/apps/challenge.bicep`, `workload.bicep` | initial workload integration | Challenge responder correctly returns 404 at /livez, so an inherited HTTP probe would restart it | TCP probe/Recreate corrected and fix-reviewed; live rendering gate pending |
| F009 | 3 | High correctness | `scripts/publish-artifacts.py` | 67-107 (initial) | Existing tag accepted without verifying its trusted source/digest record | Resolved: regression tests and both fix reviews pass; real publication succeeded |
| F010 | 3 | Medium correctness | `scripts/issue-certificate.py` | 131-144 (initial) | Staging issuance could replace the live versionless certificate | Resolved in source: validation-only staging, regression and both fix reviews pass |
| F011 | 3 | Medium correctness | `scripts/issue-certificate.py` | 100-109 (initial) | Failed first issuance discarded a newly registered ACME account | Resolved in source: finally persistence, regression and both fix reviews pass |
| F012 | 3 | High security | `containers/provisioner/Dockerfile` | 41 (initial) | Bundled Bicep executable downloaded without digest verification | Published per-architecture SHA-256 verification added; both fix reviews pass, build gate pending |
| F013 | 3 | Medium security | `.dockerignore` | 1-16 (initial) | Build context omitted several credential-file exclusions | Resolved: credential patterns, explicit runtime-script copies, both fix reviews pass |
| F014 | 1 | Connectivity blocker | Management AKS administrative endpoint | live | Operator connection times out despite healthy AKS | Observed /32 refresh restores access; parsed string guard and both final fix reviews pass; changing operator egress remains an operational prerequisite |
| F015 | 1 | High correctness | `scripts/install-radius.py` / Radius workspace loader | live | Radius workspace enumeration ignores KUBECONFIG and reads HOME/.kube/config | Resolved: per-cluster HOME, actual workspace/credential registration and identity verification, both fix reviews pass |
| F016 | 1 | High correctness | `infra/radius/environments/project-azure.bicep` | live | Azure provider credentials do not automatically authenticate private Recipe downloads | Resolved: documented WI registry SecretStore, real download/deploy and both fix reviews pass |
| F017 | 1 | Deployment blocker | `infra/radius/recipes/azure/cluster.bicep` | live | Cross-resource-group nested Azure modules fail in Radius deployment-engine evaluation | Resolved: flat Recipe/per-slot scope, both fix reviews, actual AKS creation by Radius identity, child Radius installation and HTTP workload proof passed |
| F018 | 3 | High correctness | `src/plane_demo/providers/azure.py` | 374-380 (initial) | Environment registration omitted required private-registry authentication inputs | Corrected and fix-reviewed; included in current 117 passing coordinator tests; full onboarding pending |
| F019 | 3 | High correctness | `src/plane_demo/providers/azure.py` | 932-936 (initial) | Management deployment omitted provisioner managed-identity client ID | Corrected and fix-reviewed; current coordinator tests pass |
| F020 | 3 | Medium correctness | `src/plane_demo/providers/azure.py` | 678-687 (initial) | Incomplete PostgreSQL Recipe state could be replayed before intent persisted | Pre-submission intent/refusal implemented and fix-reviewed; current coordinator tests pass |
| F021 | 3 | Medium correctness | `src/plane_demo/provisioning.py` | 95-127 (initial) | Malformed operator network fields were passed to infrastructure | Parsed IPv4/CIDR validation implemented and fix-reviewed; current coordinator tests pass |
| F022 | 4 | Medium security | `scripts/fault-parent-link.py` | 76-79, 127-140 (initial) | Caller working directory and arbitrary project identity defined the mutation boundary | Acceptance agent fixing immutable repository/project scope |
| F023 | 4 | Medium correctness | `scripts/test-e2e.py` | 623-646 (initial) | Ignored message updates or reset counters could pass acceptance | Acceptance agent adding value/counter assertions |
| F024 | 4 | Medium correctness | `scripts/test-e2e.py` | 471-478 (initial) | Stale applied version or missing report time could pass | Acceptance agent checking exact child report |
| F025 | 4 | Medium correctness | `scripts/test-e2e.py` | 666-680 (initial) | Empty control timelines could pass | Acceptance agent requiring expected state-change/report events |
| F026 | 4 | Medium correctness | `scripts/test-e2e.py`, `scripts/fault-parent-link.py` | recovery deadline (initial) | Cleanup time was excluded from catch-up timing | Acceptance agent using one recovery deadline |
| F027 | 4 | Medium correctness | `scripts/test-e2e.py` | provenance checks (initial) | Coordinator code and replacement data pod omitted from source verification | Acceptance agent extending actual image/source checks |
| F028 | 3 | High correctness | `containers/provisioner/Dockerfile` | tool runtime check | Bicep binary cannot start without ICU on the slim base image | Resolved: libicu72, real Bicep startup/build and both fix reviews pass |
| F029 | 3 | High correctness | `containers/provisioner/*requirements*`, `azure-cli.txt` | dependency resolution | Certificate overlay upgraded urllib3 beyond Kubernetes support, and macOS resolution omitted Linux distro dependency | Resolved: Linux/API-constrained lockfiles, actual tool checks and compatible package checks in ACR build, both fix reviews pass |
| F030 | 1 | Deployment blocker | Application Gateway / private Key Vault integration | live HTTPS update | App Gateway cannot access the issued certificate reference despite scoped identity grants | Resolved: documented trusted-service network exception, both fix reviews, App Gateway Succeeded and normally verified HTTPS request passed |
| F031 | 3 | High correctness | Radius workspace creation under restricted runtime identity | live | Helm installation check needs Secret-list permission that the runtime identity intentionally lacks | Known workspace + actual Radius API validation implemented and fix-reviewed; latest full coordinator suite passes |

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

The initial acceptance/fault tooling passed 36 offline tests. It has not yet
run live faults. Source reviews identified F022-F027, now being corrected.
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
