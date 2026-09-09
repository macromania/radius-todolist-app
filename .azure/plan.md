# Three-plane Radius implementation

Status: Approved - Preparing

## Authorization and context

The user authorized implementation, real Azure deployment, automatic commits,
phase-by-phase verification, and repeated rubber-duck/security reviews on
2026-09-09. They requested `SecurityControl=Ignore` on taggable resources.
This is a resource tag, not permission to disable Azure Policy or other controls.

Use the authenticated subscription
`a3ed6c04-563f-4855-ac84-bdf1e5fbc3fc`
(`MCAPS-Hybrid-REQ-38794-2022-mahmutcanga`), region `eastus2`, matching the
approved plan and existing repository. No budget ceiling was requested.
Use explicit subscription/context flags, never change global defaults.

The detailed approved ExecPlan is in this session:
`/Users/mahmutcanga/.copilot/session-state/daf82509-6410-4fa2-a026-195b90036494/files/three-plane-execplan.md`.
Durable implementation decisions and phase results belong in `DECISIONS.md`
and `FINDINGS.md`, with runtime evidence in ignored `.state/`.

## Architecture

A bootstrap Bicep deployment creates management AKS and its foundation.
Management's separate API and singleton provisioner use their own PostgreSQL.
The provisioner asks management Radius to create tenant clusters through custom
Recipes; it never creates those clusters directly. Each child cluster has Radius.

Two shared tenants share one control/data cluster pair. An isolated tenant gets
a dedicated pair. Control polls management PostgreSQL, creates its local tenant
record, and reports back. Data polls control PostgreSQL, applies a ConfigMap,
and reports back. Parents expose only their immediate child's state/timeline.
Control owns subsequent configuration updates. Data serves the applied message,
version, and an independent Redis counter without a parent dependency.

Azure uses AKS, private PostgreSQL Flexible Server, private Azure Managed Redis,
and one Application Gateway per plane with private load-balancer backends.
Let's Encrypt certificates use Azure-provided DNS names and one private Key
Vault. Initial certificates and explicit renewal only; no automatic renewal.
Local follows after Azure: kind clusters, PostgreSQL/Redis containers, Envoy
gateways on reserved loopback ports. Local cluster Recipes use Terraform kind.
The application Bicep definitions do not branch on environment.

## Delivery phases

1. Establish toolchain, contract tests, documentation, and explicit command scopes.
2. Prove Azure Radius cluster, database, gateway, and certificate integrations.
3. Implement APIs and child-initiated reconciliation, including database tests.
4. Demonstrate Azure shared/isolated onboarding and parent-outage independence.
5. Prove local Radius kind provisioning and repeat the complete scenario.
6. Resolve findings, repeat reviews on fixes, verify cleanup, finish documentation.

Each phase has real validation, rubber-duck and security reviews, findings,
and a commit. Do not label a compile-only check as a successful deployment.
Before cloud deployment update this status to `Ready for Validation`, invoke
azure-validate, record its result, then invoke azure-deploy.

## Execution boundaries

Only this project's tracked legacy files are replaced; `.git`, ignored secrets,
and unrelated clusters/resources are untouched. Use project-owned resource
groups `rg-radplanes-*`, project IDs, and explicit ownership manifests.
No broad deletes, global kubeconfig changes, public databases, or public Radius
administrative API. Runtime identities cannot grant Azure roles.
Full teardown is part of acceptance; expensive resources are not left running
by default. Key Vault purge protection remains enabled.

## Verification

Require real Radius-owned cluster creation, child reporting, tenant separation,
monotonic version/timeline semantics, public HTTPS with demo-key rejection,
private database reachability, and actual parent-link outages. Inspect rebuilt
image contents and deployed image IDs. Preserve sanitized timestamped evidence.
Use targeted tests before broad suites. Do not add workflow/HA/recovery systems.
