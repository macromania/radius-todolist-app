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

F001 first correction (`az rest --subscription`) failed the fix walkthrough:
Azure CLI's Graph request path can still acquire the default tenant's token.
The final correction explicitly acquires a subscription-scoped Graph token with
`az account get-access-token` and sends it over verified HTTPS directly to
Graph `/me`, without redirects or token logging. Regression tests inspect every
Azure command from the actual preflight path, the token passed to Graph, and
redirect rejection. No global `az account set` is used.

## Accepted scope limitations

The design is a synthetic-data, trusted-operator demonstration. A compromised
management demo key can authorize costly provisioning; the user explicitly
declined additional product limits for that scenario. This is not a fixed
security claim. Local Docker daemon access is powerful and project labels do
not confine it. High availability, automatic renewal, and interrupted
provisioning recovery are out of scope.
