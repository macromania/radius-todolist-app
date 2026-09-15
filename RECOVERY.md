# Recovery design direction

Discussion captured on 2026-09-14.

**Status: design exploration, not implemented or verified.** This document
records the recovery target, stated topology preference, emerging recovery
policy, tradeoffs, and unresolved questions. It is not an implementation plan
or an executable recovery runbook. It does not supersede the current
[application contracts](docs/contracts.md), [decisions](DECISIONS.md), or
[accepted limitations](docs/limitations.md).

## Direction in brief

Keep Radius as the portable application and infrastructure abstraction while
making recovery independent of the lifetime of any one Kubernetes cluster.
Keep product-specific tenant, configuration, and workflow records in the
management/control PostgreSQL databases.

The preferred direction under discussion is **availability-first recovery**:
restore a verified, compatible infrastructure and application deployment
baseline, preserve current product data, and address interrupted infrastructure
changes deliberately after the affected plane is operational again.

This is different from automatically resuming the exact execution that failed.
It is also different from rolling every database back to an earlier snapshot.

| Topic | Discussion position |
|---|---|
| Failure target | Agreed: replace one AKS cluster after losing its Kubernetes state while external PostgreSQL survives |
| Radius database topology | Stated preference: each Radius installation has its own dedicated external, Radius-only PostgreSQL instance |
| Product databases | Keep management/control product state separate from Radius's internal state |
| Recovery policy | User leans toward the last successfully deployed baseline; availability-first recovery is the working recommendation, not a finalized protocol |
| Azure-native deployment history | Desired, but the supported integration path remains unresolved |
| Secrets and encryption keys | An independently protected recovery source is recommended; its form and scope are not agreed |
| Implementation | No recovery implementation, migration, or proof is authorized by this document |

## Failure model and boundaries

The target is loss of one cluster, not just a Radius pod restart. The replacement
cluster starts without the original Kubernetes objects, Secrets, queues, or
controller status.

For the proposed recovery model to preserve existing data, the affected plane's
external Radius PostgreSQL, product PostgreSQL where applicable, and managed
Redis must remain available or independently recoverable. Their lifecycle must
not follow deletion of the disposable cluster. The initial recovery target does
not cover simultaneous loss of those data services.

The following are not established:

- Regional disaster recovery or simultaneous cluster and database loss.
- A recovery-time or recovery-point objective.
- Zero downtime or transparent recovery during provisioning.
- Automatic adoption of arbitrary existing Azure resources.
- Recovery using PostgreSQL alone, with no surviving keys or other artifacts.
- Exactly-once execution across PostgreSQL, Radius, Kubernetes, and Azure.

Rebuilding a child and rebuilding management have different bootstrap paths.
Management Radius currently owns child-cluster creation. If management itself
is lost, an independent operator/bootstrap path must restore it before its
provisioner can coordinate children. This exploration does not authorize
replacing Radius-owned child creation with direct AKS creation in Python.

## Dedicated Radius PostgreSQL per installation

The intended separation is:

```text
Management Radius      -> dedicated external Radius PostgreSQL
Shared control Radius  -> dedicated external Radius PostgreSQL
Shared data Radius     -> dedicated external Radius PostgreSQL
Isolated control Radius -> dedicated external Radius PostgreSQL
Isolated data Radius    -> dedicated external Radius PostgreSQL

Management application -> separate management product PostgreSQL
Control applications   -> their separate control product PostgreSQL instances
Data applications      -> local ConfigMaps and their existing Redis dependency
```

For the current five-cluster allocation, this implies five Radius-only
PostgreSQL instances. It does not mean one shared database for the three
logical plane types, or one PostgreSQL instance per tenant when tenants share
a control/data pair.

Radius 0.60.2 implements a PostgreSQL resource-store backend. The linked
upstream documentation establishes backend support; it does not prescribe a
dedicated PostgreSQL instance per plane. That topology is the user's isolation
preference.

Within each installation's instance, preserve the component-store separation.
The pinned chart initializes separate `ucp`, `applications_rp`, and `dynamic_rp`
databases. Its resource table is keyed by normalized resource ID; independent
installations reuse Radius scopes such as `/planes/radius/local`. They must not
accidentally address the same resource tables.

The stock `database.enabled=true` Helm setting creates an **in-cluster**
PostgreSQL StatefulSet. It is not an external-database configuration switch.
External endpoints, schema initialization, credentials, verified TLS, and the
upgrade/configuration path still need evaluation.

Each Radius database must exist before its dependent Radius installation starts
and survive replacement of that installation's cluster. Database creation,
access recovery, and deletion ownership must not depend exclusively on the
Radius instance whose state the database protects.

Adding administrative PostgreSQL to a data-plane installation must not add it
to the data API's request path. Radius database availability affects Radius
administration; it should not become a new dependency for serving tenant data.

## What PostgreSQL preserves, and what it does not

Radius has separate resource-store, secret-store, and asynchronous-queue
subsystems. Changing the resource-store backend relocates one of them.

| State | Relevant boundary in the pinned implementation |
|---|---|
| Radius resource records | Can use the PostgreSQL backend instead of Kubernetes custom resources |
| UCP and resource-provider records | Each component needs the appropriate database configuration |
| Radius secrets | Use the separate secret provider, normally Kubernetes Secrets |
| Encryption keys for sensitive resource fields | Read from the versioned `radius-system/radius-encryption-key` Secret |
| Asynchronous work messages | Use the separate API-server-backed queue in the chart |
| Kubernetes Deployments, Services, ConfigMaps, RBAC, and controller status | Not moved into PostgreSQL by changing Radius's resource store |
| Terraform Recipe state, if used | The pinned executor uses a Kubernetes Secret backend |
| Bicep deployment-engine execution state | Full crash-recovery behavior was not established by inspecting the resource-store backend |

Preserved encrypted records can be unusable if their original key versions are
lost. Generating a new encryption key is not equivalent to recovering the old
keys. Access tokens may be reacquired, but long-lived credentials and original
encryption material need an explicit recovery strategy.

Likewise, a surviving resource record marked as provisioning does not prove
that a lost queue message will be reconstructed and processed safely.

A database of remembered resource state is not necessarily an accurate view of
the replacement cluster. A saved successful workload record can coexist with
an empty Kubernetes API.

## Availability-first versus latest-desired recovery

Two recovery policies were considered:

| Consideration | Latest committed desired state | Last successfully deployed baseline |
|---|---|---|
| Target | Complete convergence toward the newest accepted change | Restore a previously verified deployment |
| Interrupted infrastructure change | Continue after determining its actual outcome | Keep interrupted until explicitly retried or superseded |
| Core records | Desired revisions, operation identities, outcomes, and continuation information | Known-good artifacts/configuration plus records of incomplete changes |
| Main complexity | Safe continuation through partial execution | Reconstructing a compatible known-good baseline |
| Operator involvement | Lower for failures that can be reconciled automatically | Higher for deciding the disposition of unfinished changes |
| User expectation | Accepted changes should eventually complete | Restore availability first, then handle unfinished changes |

The user is closer to the second policy. It narrows the workflow-resumption
promise, but it does not eliminate partial-failure handling.

For example, Azure might create a Redis instance before the worker records
success. Returning to an older baseline does not remove that instance.
Recovery must identify the outcome and avoid duplicate creation or deletion
of an in-use dependency. An uncertain outcome remains uncertain until checked.

### Deployment baseline is not product data

The working recommendation separates these two policies:

**Infrastructure and application release:** recover the last successfully
verified deployment for the affected plane, provided it remains compatible
with the surviving services. Hold incomplete infrastructure changes for a
deliberate continuation or replacement request.

**Product configuration and data:** preserve the latest committed tenant and
configuration records in management/control PostgreSQL. Replacing AKS must not
discard later control-owned updates or tenant records.

```text
Last verified application release: release 12
Latest committed tenant message:  "hello"
Infrastructure release 13:        interrupted

Recovery target under discussion:
  Restore a compatible release 12 deployment.
  Preserve the tenant message "hello".
  Leave release 13 interrupted until explicitly addressed.
```

After the product reconcilers restart, they naturally pull the current desired
configuration. Restricting them instead to the last configuration previously
applied by data would require another policy and mechanism. That restriction
has not been selected.

### A known-good baseline needs more than a success flag

The necessary records would include the relevant image digests, Recipe
versions, application declarations, deployment parameters, resource identities,
and recoverable credential references for each plane. The exact checkpoint
format and storage location remain open.

Compatibility must be checked before restoring older application code or
reapplying infrastructure. An interrupted change might already have modified a
schema, rotated a credential, or changed a service configuration.

Recovery must not blindly replay old templates, re-run fresh-database
initialization against an existing database, or overwrite newer Radius records
with an old snapshot merely to make status appear consistent.

## Reconstruction requires a bootstrap stage

The current product reconcilers are deliberately narrow:

| Reconciler | Current responsibility |
|---|---|
| Control | Pull management's assigned tenant records and ensure initial control configuration without overwriting later control-owned updates |
| Data | Pull control configuration and apply tenant ConfigMaps |

They do not install themselves, create Radius, restore credentials, or rebuild
all Kubernetes workloads. Management's initial tenant message also cannot
reconstruct later control-owned configuration if the control database is lost.

The inspected Radius controllers do not establish a general "scan PostgreSQL
and rebuild an empty cluster" mechanism. Kubernetes-oriented controllers need
their input objects and status. For example, the Deployment controller returns
when its watched Kubernetes Deployment is absent.

The proposed conceptual sequence is:

1. Identify the affected installation and hold conflicting infrastructure
   mutations. Prevent the old installation from resuming writes.
2. Establish the outcome and ownership of any incomplete infrastructure work.
   Preserve surviving managed services and shared dependencies.
3. Create the replacement cluster through the appropriate bootstrap or parent
   Radius owner.
4. Restore network access, identities, required secrets/key versions, and
   installation configuration. A replacement AKS cluster has a new OIDC issuer;
   old workload-identity bindings cannot simply be assumed valid.
5. Connect the restored Radius components to their dedicated surviving
   PostgreSQL stores and verify access to their records.
6. Explicitly reconstruct the required application workloads from a compatible
   known-good baseline through supported deployment interfaces.
7. Start the product reconcilers and observe their application of current
   product configuration.
8. Establish recovery from fresh observations, then allow normal management
   requests and control configuration changes. Keep incomplete infrastructure
   changes visible until explicitly resolved.

This is a behavior model, not a proven ordered procedure or permission to run
recovery commands. The exact component startup ordering, queue disposition,
and handling of lost deployment-engine state remain unresolved.

## Interrupted Radius execution

The public source shows distinct mechanisms rather than one universal resume
protocol:

| Mechanism | Observed behavior | Recovery implication |
|---|---|---|
| Kubernetes template controller | Stores a polling resume token in Kubernetes object status | A pod restart and loss of the entire object/status store are different cases |
| Radius asynchronous worker | Processes a separate queue with bounded redelivery/retry behavior | External resource records do not replace the queue |
| Bicep Recipe driver | Creates a timestamped Radius deployment ID on each execution and polls for completion | Re-entering the driver is not automatically attachment to the earlier execution |
| Recipe error handling | Some Recipe failures are terminal | No promise that every failure is retried until success |
| Product provisioner today | Marks old running work `interrupted` and refuses automatic replay | A deliberate recovery/retry interface would be new behavior |

The deployment engine is a separate .NET service. Its public Radius callers
and integration documentation were inspected, but the referenced engine source
repository was not accessible during this discussion. Its internal storage,
restart behavior, and guarantees after losing Kubernetes state remain
unverified.

Radius also documents a separate state-archive abstraction used by startup and
shutdown tooling. This is snapshot/restore prior art, not evidence that
PostgreSQL alone recovers an unexpected cluster loss. Its coverage and
applicability to this external-database design need separate investigation.

## Native Azure execution remains a separate question

Azure already retains the actual AKS, PostgreSQL, Redis, gateway, and networking
resources created through Radius. Their resource state is not stored only in
Radius. What the current Bicep Recipe path does not produce is a native Azure
ARM deployment-history record for the Recipe execution.

An ordinary Bicep module does not change this. In Radius 0.60.2, the deployment
provider scope is separate from the native Azure resource scope:

```text
Deployment/module scope:
  /planes/radius/local/resourceGroups/radplanes

Native Azure resource scope:
  /subscriptions/<subscription>/resourceGroups/<azure-group>
```

Wrapping a Recipe in a module does not redirect it to Azure's deployment engine.
An explicit `Microsoft.Resources/deployments` declaration is not a verified
Bicep-only escape route either.

The integration being sought is:

```text
Radius Recipe
  -> supported integration submits a real ARM deployment
    -> Azure executes the Azure-only template
  -> Radius receives outputs and retains clear lifecycle ownership
```

A Terraform Recipe using AzureRM's resource-group template-deployment resource
was discussed as one candidate built from existing interfaces. It is not proven
in this repository and would add Terraform state and provider-specific deletion
behavior. No Bicep-only wrapper has been established.

Recipes that mix native Azure resources with Kubernetes objects would need a
clear execution boundary. The current PostgreSQL Recipe, for example, creates
both a managed Azure server and a Kubernetes initialization Secret.

Native ARM deployment history and external Radius PostgreSQL solve different
problems. Neither alone provides a complete onboarding workflow or cluster
reconstruction. Deployment history is also not an indefinite audit archive.
Low deployment frequency reduces pressure on its limits but does not make it a
backup or lifecycle-ownership record.

See [HOW_PROVISIONING_WORKS.md](HOW_PROVISIONING_WORKS.md) for the existing
execution path and the distinction between ARM resource state, deployment
history, and Radius state. Azure deployment stacks were discussed as a native
resource-set management capability; they are not a selected integration.

## Visibility, ownership, and portability

The planes could retain a selected view of Radius activity: logical resource
IDs, requested revisions, Recipe versions, observed status, non-secret output
IDs, attempt references, sanitized errors, and observation timestamps.

The current implementation already records coarse provisioning stages and
events transactionally in management PostgreSQL. It does not provide a complete
recoverable journal for every Radius action. A more detailed view would support
operators and future recovery, but would not substitute for Radius state
recovery.

Observations must retain their freshness and scope. If Radius becomes
unreachable, the plane can show the last known result and its age. It must not
turn a historical success into a current health assertion.

An administrative observer or provisioner should collect infrastructure
information. Public APIs and product reconcilers should not receive Azure
administrative credentials simply to display it.

Portability remains at the application and workflow contract boundary.
Provider-specific deployment IDs, operation references, and recovery mechanics
can stay behind provider implementations. PostgreSQL backend support does not
make every Azure recovery action portable by itself.

Keep one lifecycle owner per resource. An external workflow can coordinate and
observe Radius without independently changing the same resources through
another writer. Synthetic deployments created only to populate Azure history
would not provide trustworthy execution evidence.

The existing contracts remain intact:

- Management owns fleet/provisioning coordination; control owns tenant
  configuration.
- Management tenant readiness means control-record creation, not Radius or AKS
  provisioning success.
- Control's applied report means data ConfigMap application, not transitive
  dependency health.
- Children pull configuration and report their own progress.
- Data API requests depend on local ConfigMaps, Redis, and the API's own key,
  not Radius PostgreSQL or a parent database.
- Recovery of one tenant must not delete a shared pair used by other tenants.

## Open questions and evidence needed

| Question | Why it matters |
|---|---|
| Which secrets, original key versions, artifacts, and access records survive outside AKS? | Preserved metadata alone can be unusable |
| How are external Radius databases bootstrapped, configured, and upgraded? | The stock PostgreSQL chart option is in-cluster |
| What is the exact known-good checkpoint, and how is compatibility established? | A success flag cannot safely reconstruct an installation |
| What happens when first-time onboarding fails before any successful baseline exists? | There may be no previous deployment to restore |
| How are old writers excluded and uncertain operations resolved? | Replacement must not create concurrent owners or duplicate resources |
| What is recoverable from the queue and deployment-engine stores? | Lost work messages and saved resource records are not equivalent |
| Which supported path produces genuine native ARM deployment records? | A normal Bicep module wrapper does not establish that behavior |
| How are operator retries/new changes exposed after recovery? | Current terminal provisioning operations cannot simply be resumed |
| What are the backup, retention, recovery-time, and recovery-point requirements? | External placement alone is not a durability guarantee |

Evidence would need to cover management, control, and data replacement
separately, with surviving managed services and product data. It would also
need to cover an interrupted operation, missing key material, stale status,
and an old installation attempting to return.

No fresh-cluster recovery, safe re-adoption, automatic workload reconstruction,
or external Radius PostgreSQL deployment has been proven by this document.
Actual results continue to belong in [FINDINGS.md](FINDINGS.md).

## Source references

The Radius references below are pinned to 0.60.2. Documentation and source
inspection describe behavior; they do not replace a live recovery test.

| Source | Relevance |
|---|---|
| [Radius state persistence](https://github.com/radius-project/radius/blob/v0.60.2/docs/architecture/state-persistence.md) | Separate resource, secret, and queue stores; PostgreSQL backend |
| [PostgreSQL initialization](https://github.com/radius-project/radius/blob/v0.60.2/deploy/Chart/templates/database/configmap-initdb.yaml) | Separate UCP, Applications RP, and Dynamic RP databases |
| [PostgreSQL StatefulSet](https://github.com/radius-project/radius/blob/v0.60.2/deploy/Chart/templates/database/statefulset.yaml) | The built-in chart database runs inside Kubernetes |
| [Encryption-key provider](https://github.com/radius-project/radius/blob/v0.60.2/pkg/crypto/encryption/keyprovider.go) | Original key versions are read from a Kubernetes Secret |
| [Deployment-engine architecture](https://github.com/radius-project/radius/blob/v0.60.2/docs/architecture/deployment-engine.md) | Template execution, callbacks, operation polling, and engine boundaries |
| [Controller architecture](https://github.com/radius-project/radius/blob/v0.60.2/docs/architecture/controller.md) | Kubernetes inputs and persisted controller status |
| [Deployment controller](https://github.com/radius-project/radius/blob/v0.60.2/pkg/controller/reconciler/deployment_reconciler.go) | Missing Kubernetes Deployment does not trigger reconstruction from PostgreSQL |
| [Bicep Recipe driver](https://github.com/radius-project/radius/blob/v0.60.2/pkg/recipes/driver/bicep/bicep.go) | Per-execution deployment IDs and separate provider scopes |
| [Asynchronous worker](https://github.com/radius-project/radius/blob/v0.60.2/pkg/armrpc/asyncoperation/worker/worker.go) | Queue-dependent execution and bounded retries |
| [Recipe resource controller](https://github.com/radius-project/radius/blob/v0.60.2/pkg/portableresources/backend/controller/createorupdateresource.go) | Saved resource processing and terminal Recipe errors |
| [State archive](https://github.com/radius-project/radius/blob/v0.60.2/docs/architecture/state-archive.md) | Separate snapshot/restore abstraction |
| [Azure deployment-history retention](https://learn.microsoft.com/en-us/azure/azure-resource-manager/templates/deployment-history-deletions) | History is bounded and independent of deployed resource existence |
| [Azure deployment stacks](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/deployment-stacks) | Native managed resource sets, not a selected solution |
| [AzureRM template-deployment resource](https://github.com/hashicorp/terraform-provider-azurerm/blob/main/website/docs/r/resource_group_template_deployment.html.markdown) | Candidate real ARM deployment integration; provider behavior must be version-pinned if selected |
