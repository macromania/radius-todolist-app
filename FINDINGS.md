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
| F002 | 1 | Deployment blocker | Azure PostgreSQL regional capability | n/a | Subscription cannot provision PostgreSQL in East US 2 or West US 2 | Resolved for preflight: Central US capability checks and both fix reviews passed; actual provisioning remains a phase gate |
| F003 | 1 | High correctness | `infra/bootstrap/azure.bicep` | 144-156, 245-247 (initial) | Compiled module scopes include invalid loop expressions in non-loop references | Resolved: compiled-contract tests, fix walkthrough, real ARM what-if/validate pass |
| F004 | 1 | High correctness | `infra/bootstrap/federation.bicep` | 11 (initial) | Concurrent writes to one identity's federated credentials can return 409 | Fixed with serial loops; fix walkthrough passes, live deployment pending |
| F005 | 1 | High security | `infra/bootstrap/platform-access.bicep` | 150-167 (initial) | Tenant-reachable gateway/issuer identities can read or replace other planes' certificates in the shared vault | Resolved in source: exact object grants, both fix reviews pass; live cross-plane denial still required |
| F006 | 1 | High correctness | `scripts/install-radius.py` | 38-59 (initial) | Existing workload-identity label means a patch does not restart pods after identity annotations | Fixed; explicit rollout restart, regression, and both fix reviews passed; live pod projection remains a deployment gate |
| F007 | 2 | Medium correctness | `src/plane_demo/control_api.py` | 35-52 (initial) | A delayed older-version success hides failure of the current desired version | Resolved: reproduced before fix, 37 related real-PostgreSQL integration tests passed after, both fix reviews clean |
| F008 | 3 | High correctness | `infra/radius/apps/challenge.bicep`, `workload.bicep` | initial workload integration | Challenge responder correctly returns 404 at /livez, so an inherited HTTP probe would restart it | Changed challenge to TCP probe; provisioner also explicitly Recreate; live rendering and fix reviews pending |

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

The initial source compiles but has not been deployed. Rubber-duck review
identified F003/F004/F006; security review identified F005 with confidence 9/10.
These are being fixed before deployment. The shared certificate vault remains
one project vault; certificate access will be scoped to each plane's objects.

| # | Severity | File | Lines | Vulnerability | Confidence |
|---|----------|------|-------|---------------|------------|
| 1 | 🟠 HIGH | `infra/bootstrap/platform-access.bicep` | 150-167 (initial) | Vault-wide gateway/issuer access permits cross-plane certificate access | 9/10 |

## Phase 2 - Application source review

The API/reconciler layer passed 77 tests using real PostgreSQL and Redis; one
Kubernetes test awaits the live cluster. The final-image smoke exercised
bootstrap and HTTP 200/401/422/202/409/503 behavior, nonroot execution, and
source-content hashes. The phase security review found no vulnerabilities.
The rubber-duck review found F007; it is fixed and both fix reviews found no
remaining issue. The existing image predates that fix and must be rebuilt and
re-inspected before deployment. These tests do not replace Azure end-to-end acceptance.

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
