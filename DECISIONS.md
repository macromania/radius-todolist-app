# Implementation decisions

The approved three-plane design is the source of scope. This log records
implementation choices, not unverified claims.

## D001 - Explicit authorization and environment

2026-09-09: implement the approved plan, deploy real resources, verify each phase,
and commit verified work automatically. Use the already authenticated Azure
subscription and the project's existing `eastus2` region. Record changed
assumptions here rather than silently switching deployment targets.

## D002 - Project isolation and tags

Use the new project prefix `radplanes` and resource groups `rg-radplanes-*`.
Do not reuse or delete legacy `rg-todolist-*` resources. Apply
`SecurityControl=Ignore`, `project=radplanes`, and `managedBy=radius-todolist-app`
to every taggable resource and explicitly configure AKS node resource tags.
Do not disable policy enforcement to make a deployment pass.

## D003 - Simple development workflow

Use Python 3.13, uv, FastAPI, Psycopg, the Kubernetes Python client, Redis,
pytest, and Ruff. Make targets provide the operator interface. No UI, broker,
general-purpose workflow framework, automatic provisioning recovery, or
tenant migration. Azure deployment precedes local deployment.

## D004 - Verification and review records

Each phase records commands, actual outcomes, and review findings in
`FINDINGS.md`. Findings retain stable IDs and their evidence after resolution.
Run rubber-duck and security reviews for each phase and each coherent finding
fix. Review scope is the phase/fix, not repeated whole-repository speculation.

## D005 - Existing conventions

Keep application Bicep environment-independent. Use the Bicep compiler bundled
with Radius, project-specific Radius configuration and kubeconfigs, and the
reserved local host ports in `ports.env`. Preserve TLS, private-networking,
secret-output, URL-encoding, and immutable Recipe-publication invariants.

## D006 - Subscription-scoped operator identity

Acquire a Microsoft Graph token explicitly for the project subscription before
reading `/me`. `az ad signed-in-user` and the Graph path in `az rest` can otherwise
use the global default tenant. Use a fixed verified HTTPS destination without
redirects or token logging. This was discovered and corrected by the phase-zero
rubber-duck review, then exercised against the real login.

## D007 - Workload identity installation

Registering Radius's Azure credential is not sufficient by itself. Annotate all
four Radius service accounts (`applications-rp`, `bicep-de`, `ucp`, `dynamic-rp`)
with their managed identity and label their pods for the AKS workload-identity
webhook. Verify projected identity configuration in the running pods. The
follow-up on radius-project/radius#12278 identifies this as the missing setup;
do not fall back silently to a long-lived service-principal secret.

## D008 - Central US after a real regional restriction

Azure PostgreSQL capability queries rejected provisioning in both `eastus2` and
`westus2` for this subscription, returning no versions or editions. Use
`centralus`, where PostgreSQL 16/17/18 and the chosen General Purpose SKUs are
available, AKS 1.35.7 is supported, Managed Redis is listed, and DSv5/regional
capacity is 100 cores with zero in use. Preflight now rejects an empty PostgreSQL
capability result instead of deploying into a known-restricted region.
This supersedes D001's initial region while preserving its subscription.
