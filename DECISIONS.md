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

## D009 - Isolated tool and registry configuration

Keep Azure CLI in its own container virtual environment: its pinned SDK versions
conflict with the certificate SDK versions. Use fully resolved dependency files.
Use project-specific Docker credentials; the global credential helper stalled
public image resolution. Never modify the user's global Docker configuration.
Exclude state, kubeconfigs, keys, and credentials from Docker build contexts.

## D010 - Certificate ownership and staging

Keep one project Key Vault, but restrict each gateway and issuer to its own
deterministic certificate/account objects. A child identity must not read or
replace other planes' certificates. Keep staging and production issuance
distinguishable in certificate tags, and never reuse a staging certificate as
the final trusted-HTTPS certificate.

## D011 - Radius workspace isolation requires an isolated HOME

Live Radius 0.60.2 workspace creation reads `HOME/.kube/config` even when
`KUBECONFIG` is set. Each cluster therefore gets a project-owned home directory,
a link to its exact kubeconfig, and the verified Bicep compiler. Preserve the
Azure CLI cache path explicitly; never add contexts to the user's global file.

## D012 - Operator access follows observed egress, not a broad CIDR

The laptop's outbound network changed during implementation. Record the actual
observed public IPv4 addresses in ignored environment state and authorize each
as `/32`, plus the project's fixed NAT address. Do not open a subnet or
`0.0.0.0/0` to compensate for a timeout. Refresh/revalidate IaC when egress
changes; Azure's authenticated command channel can distinguish cluster health
from operator reachability without exposing the Kubernetes endpoint.

## D013 - Private Recipe authentication is separate from Azure provisioning

Each Radius environment explicitly configures Bicep registry authentication
through a workload-identity SecretStore. Granting AcrPull and registering the
Azure provider alone did not authenticate Recipe downloads. This correction
was verified by a real private PostgreSQL Recipe deployment.

## D014 - Use an Azure image builder when the local package CDN is unreachable

The local package CDN failed TLS handshakes repeatedly with multiple clients.
Build in the project ACR with the same pinned Dockerfiles, preserving complete
build/push output. Pull the resulting image and compare its actual source
contents before deployment. Never disable TLS verification to make a build pass.

## D015 - Bootstrap Jobs use known Radius configuration, not extra Secret access

The in-cluster bootstrap identity can use Radius but cannot list its Helm release
Secrets. `rad workspace create` therefore reports "not installed". Generate
the protected workspace configuration from already verified cluster metadata
and verify a real Radius API read instead of granting more Secret permissions.
An ephemeral operator Job can be launched through authenticated AKS Run Command
when the laptop's direct Kubernetes connection is unreliable. Tenant cluster
creation still happens through Radius, never through a direct AKS create call.

## D016 - Application Gateway's trusted-service certificate path

Keep Key Vault `publicNetworkAccess=Disabled`, its private endpoint, and exact
per-plane certificate RBAC. Enable the documented `AzureServices` network
exception for Application Gateway's certificate-validation path. A valid
certificate and correct object-scoped identity were insufficient with
`bypass=None`. This does not grant other identities access to certificate data
or enable arbitrary public clients.

## D017 - Flat cluster Recipes with per-slot Azure scopes

Radius's deployment engine rejected references to nested cross-resource-group
Azure deployment modules. Keep the cluster Recipe flat and register one
management-Radius environment per allocated child slot, scoped to that slot's
cluster resource group. The child-cluster application definition remains
unchanged. Real Azure Activity Log evidence confirms Radius creates the cluster;
the coordinator only bootstraps its Radius installation and workloads afterward.

## D018 - Persistent operator state is separate from runtime provisioning state

Run the initial management deployment in a scoped operator Job when direct
laptop Kubernetes access is unreliable. Its full operator credentials and
deployment records persist on `operator-state`; runtime provisioning receives
only its restricted seed and uses `provisioner-state`. Both use the tagged
project Azure Disk StorageClass. Standardize kubeconfigs as `<slot>.kubeconfig`;
the earlier management gate path is only a local alias, never a global context.

## D019 - Kubernetes private-volume permissions

Use `fsGroupChangePolicy: OnRootMismatch` with the fixed workload UID/GID.
Default recursive ownership handling widened generated credential files from
0600 to 0660 on remount. Keep strict credential permission checks; do not weaken
them to hide a mount-policy error. An explicit repair and a real second mount
verified that private modes are retained.

## D020 - Prove behavior first; organize the repository now

2026-09-09: the user asked to continue the approved end-to-end plan and defer
deeper refactoring/simplification until it is proven. Make a behavior-preserving
layout change now: visible management/control/data packages, separate platform
operations and demonstration harness, separate image packaging, and only three
plane application definitions. Remove the obsolete todo example and update the
README, Makefile, imports, image build paths, and tests together. Do not change
SQL authorization, reconciliation semantics, or the agreed Azure/local topology
as part of this organization pass. Preserve deployment state and credentials.

## D021 - Authorize the actual Radius API transport

Radius 0.60 uses the aggregated Kubernetes API, not just pod port-forwarding.
The management worker therefore needs cluster-scoped access to `api.ucp.dev`,
restricted to `planes/local` named `radius`. It does not receive Kubernetes
cluster-admin or Secret-list permission. The real service-account request
changed from 403 to 200 after that exact grant.

## D022 - Keep network-dependent demo execution in the harness

When the laptop cannot reach AKS directly, run the existing harness in a
management-cluster Job using a dedicated harness identity and
verified tool image. Supply committed source through a Git bundle so provenance
remains real. Keep its state separate from operator and runtime-provisioner
state. This adds no application plane, VPN, or jump host, and does not change
the child-initiated reconciliation or outage contract.

## D023 - Separate harness inspection from runtime provisioning

The coordinator's AKS bootstrap permissions do not include Application Gateway
or delegated-subnet reads. Give the opt-in harness its own workload-federated
identity: Reader on project app/cluster/platform groups and AKS Cluster User
plus AKS RBAC Cluster Admin on the allocated cluster groups. These cluster
permissions support trusted-operator Secret export, pod inspection, and custom
Cilium outage policies; they are not a tenant isolation boundary. Grant no
direct Azure resource writes, role delegation, or Key Vault data access to this
identity. Keep the coordinator's existing runtime grants unchanged. The harness
must refuse missing identity configuration rather than fall back to coordinator
or human credentials.

## D024 - Continue observation, never replay an accepted provisioning operation

An exporter failure stopped the acceptance harness after its first tenant
request had passed admission, duplicate, and busy-response checks. The runtime
operation kept running normally. Provide a narrow, opt-in harness continuation
from that first-tenant evidence only: verify the original admission and current
operation identity, observe completion, then execute the remaining scenario.
Retain the failed record and link it from fresh evidence with both source
commits. Never retry provisioning, reset tenant state, skip later assertions,
or describe a continued run as an uninterrupted fresh run. This does not add
runtime recovery or a general-purpose resumable test workflow.

The allowed boundary also includes the first tenant's read-only verification
before any second admission, pause, update, increment, or fault. Those checks
must all rerun; saved observations never substitute for current verification.

## D025 - Give bootstrap RBAC its own resource names

Radius generates container Roles under container names. Bootstrap's additional
ConfigMap grants use `data-api-configmaps` and `data-reconciler-configmaps`
Role/Binding names instead. Bind the original service accounts; do not force
field-manager ownership or expand permissions to resolve a naming collision.

## D026 - Order existing-resource reads in Radius Recipes

Radius evaluates declared existing Azure resources during Recipe execution.
When a private endpoint creates the NIC being referenced, put the endpoint
dependency on that existing NIC declaration as well as on its tag extension.
An existing declaration is not merely a compile-time resource ID in this path.

## D027 - Reset a failed demonstration through its Radius owners

Do not add automatic retry or repair a failed onboarding record. Remove the
failed demo's Radius applications and child clusters in owner order, preserving
the bootstrap foundation for a fresh run. `clean-azure.py --radius-only` has an
explicit partial result and keeps credentials/evidence. It never deletes Azure
groups or role assignments directly, so scoped in-cluster operator access is
enough. Full teardown keeps its role checks and remains a separate acceptance
gate. Managed node-resource ownership checks remain mandatory for every live
AKS in the partial path; give the scoped operator read access to those groups.
Without a live AKS, Radius-only cleanup cannot delete the corresponding node
group: report it as uninspected, never absent. Full cleanup still checks all
allocated groups, including orphans.

## D028 - Operational safety checks must survive Python optimization

Use unconditional validation and exceptions, not `assert`, for deployment,
cleanup, authentication, ownership, and evidence guards, including disposable
operator scripts. Route mutations through the existing guarded command helpers.
When code is embedded in an immutable ConfigMap, regenerate and verify the
actual upcoming execution reference after a fix. Keep old failed manifests as
historical evidence rather than treating a repaired source file as a deployed fix.

## D029 - Target the actual local Terraform runtime

Radius 0.60.2 executes a custom cluster's Terraform Recipe inside `dynamic-rp`,
not a separate Job. The local image/socket overlay therefore targets only
management's `dynamic-rp`, preserving its entrypoint and Terraform layout.
The chart has image overrides but no generic volume/security-context knobs:
make the version-pinned installation patch explicit, not an invented Helm value.
Use a supported in-cluster HTTP module archive rather than a local-path
validation gap or human Git credentials. Serve only immutable static Recipe
content, never Terraform state or credential directories. Keep a Radius installation per child,
CA-verified child API access, and no automatic interrupted-provisioning recovery.
Socket permissions, sibling API SANs, and actual execution remain live gates;
local implementation still waits for Azure acceptance.

## D030 - Separate Pod shutdown time from configuration catch-up

The acceptance harness waits for the data reconciler's configured termination
grace plus 30 seconds of controller margin when pausing it. Validate that grace
before changing replicas and restore the same Deployment in `finally`.
Do not force-delete Pods or weaken the independent 30-second outage catch-up
requirement to accommodate normal Kubernetes shutdown.

## D031 - Keep governance controls separate from test recovery

Treat an external cluster stop as an interruption, not an application failure.
The requested resource tags are not a guarantee against every governance action.
Do not modify automation or policy to keep tests running. For this authorized
deployment, use ordinary start operations only on verified project clusters,
then inspect recorded state. Never convert interrupted work into success or
automatically replay it; repeated stops require an approved uninterrupted window.

## D032 - Repair ownership metadata only with independent proof

If external writes remove a required tag, keep cleanup fail-closed. A missing
tag is not permission to adopt a resource. For a state disk, verify the exact
live namespace, PVC/PV UIDs, CSI handle, StorageClass, and Azure disk identity
before a narrowly scoped repair. Merge only the requested ownership tags,
preserve other properties and tags, and verify the result. Do not change
governance controls or repeatedly fight external writes.

## D033 - Track independently owned Recipe lifecycle roots

The desired lifecycle roots are the cache and private endpoint, but live
verification disproved the assumption that the explicit output list controls
all tracking. Radius 0.60 also adds every implicitly created template resource.
Removing a tag extension from the output list alone does not remove it from
managed state. Keep this correction visible: virtual tag metadata needs a
separate application mechanism that does not become an unsupported Recipe
deletion target. Verify actual stored tracking, not only compiled outputs.

## D034 - Dispose of unusable metadata only through a verified lifecycle owner

Do not edit Radius's backing store to clear a stuck record. During whole-demo
reset, an empty child may be removed through its management Radius cluster
owner only after verifying its Azure app group empty, exact cluster ownership,
and the known terminal metadata that cannot be deleted normally. Keep this an
explicit reviewed recovery, not an automatic cleanup bypass or a claim that
the original application deletion succeeded.

## D035 - Separate discovery from convergence and label existing-state proof

Allow up to 300 seconds for the initial export of a successfully provisioned
pair's endpoints. The exporter reads five clusters serially; that administrative
discovery is not application convergence. Keep normal API and applied-state
checks, including outage recovery, bounded to 30 seconds.

When admission already succeeded but harness discovery failed, keep the working
tenants. An explicit `verify-existing` run creates no tenants and claims no
fresh admission, historical reuse, or paused-admission proof. It checks current
topology, configuration, counters, authentication, timelines, idempotency, and
both outages. Keep the original failed admission record unchanged and report
the two evidence scopes separately rather than inventing a general resume path.

## D036 - Apply generated NIC metadata with the existing Recipe identity

Keep Azure-generated Redis NIC tags outside the Bicep Recipe so Radius does
not track an unsupported tag-deletion target. After each data deployment, run
a bounded Job under the existing per-plane `applications-rp` workload identity.
Do not add coordinator tag permissions or another identity.

Before merging tags, prove the resource group, Radius ownership tags, cache,
private endpoint, subnet, and NIC backlink match the allocated plane. Preserve
other tags and network properties, read the tags back, and wait for actual Job
success. Failures remain visible for explicit operator cleanup. Existing
Radius records containing the old tag extension are not repaired by this
source change; prove the new create/tag/delete lifecycle on a fresh resource.

## D037 - Keep deletion with Radius when its CLI preflight is broken

Radius 0.60.2's generic delete command rejects a generated SecretStore's empty
application ID before sending DELETE. For that verified gate resource, use
the authenticated native Radius resource API after checking exact ownership,
output references, and former-owner absence. Wait for the Radius resource and
its backing Secret to disappear, then verify the surrounding resources.

This is a specific client compatibility correction, not a general force-delete
fallback. Do not alter stored ownership fields, edit Radius's backing store,
or bypass its resource lifecycle by directly deleting the Kubernetes Secret.
