# Three-plane Radius implementation

Status: Validated (bootstrap infrastructure only)

Current gate: bootstrap source corrections F003-F006 are implemented,
fix-reviewed, and pass renewed ARM what-if/validation.
The first real ARM what-if reproduced F003 before any resource creation.
Application SQL/API phase is committed (`b0a5f4a`) but Azure runtime acceptance
has not occurred. Do not treat that commit as a deployment result.

## Authorization and context

The user authorized implementation, real Azure deployment, automatic commits,
phase-by-phase verification, and repeated rubber-duck/security reviews on
2026-09-09. They requested `SecurityControl=Ignore` on taggable resources.
This is a resource tag, not permission to disable Azure Policy or other controls.

Use the authenticated subscription
`a3ed6c04-563f-4855-ac84-bdf1e5fbc3fc`
(`MCAPS-Hybrid-REQ-38794-2022-mahmutcanga`), region `centralus`. Live preflight
found PostgreSQL subscription restrictions in `eastus2` and `westus2`; Central US
supports the required PostgreSQL, AKS, and Redis services. No budget ceiling was requested.
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

## 7. Validation Proof

2026-09-09T08:19:28Z: `uv run python scripts/validate-bootstrap.py` passed
the bundled Bicep compile, real `az deployment sub what-if`, and
`az deployment sub validate`. Exact template and parameter hashes are stored
in `.state/azure/validation.json` and checked again before creation.
What-if cannot calculate some not-yet-created identity-based role-assignment
IDs; these are reported as unsupported preview details, not deployment proof.

F003/F004 fix walkthrough: compiled resource scopes are correct, federation
loops serial, 10 compiled-contract tests and 3 setup tests passed.
F005 fix security review: no remaining vulnerability in exact per-plane
Key Vault object grants. F006 restart fix reviews and script regression pass.
Live creation/import/identity projection remain explicit deployment gates.

This validation authorizes only the bootstrap infrastructure deployment.
The provisioning tool image subsequently built successfully in ACR after adding
ICU and resolving Linux dependencies against the API lock. Its actual scripts,
verified compiler binaries, and nonroot identity were checked in AKS.

## Live integration proof

Management AKS/Radius, private PostgreSQL with verified TLS, real Application
Gateway/private backends, staging/production ACME issuance, and trusted HTTPS
have passed their live gates. Radius created a child AKS under a per-slot Azure
scope; Activity Log attributed creation to management Radius's identity.
The coordinator bootstrapped child Radius and exercised a child HTTP workload.
Full tenant onboarding, live outages, local deployment, and teardown remain open.

## Verification

Require real Radius-owned cluster creation, child reporting, tenant separation,
monotonic version/timeline semantics, public HTTPS with demo-key rejection,
private database reachability, and actual parent-link outages. Inspect rebuilt
image contents and deployed image IDs. Preserve sanitized timestamped evidence.
Use targeted tests before broad suites. Do not add workflow/HA/recovery systems.
