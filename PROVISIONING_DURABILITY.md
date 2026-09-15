# Provisioning durability and portable scheduler placement

**Status: design direction, recorded September 14, 2026. No runtime
implementation or local scheduler product has been selected by this document.**

The provisioning workflow should use a scheduler backend selected for the
deployment environment:

| Deployment | Scheduler backend | Required connectivity |
|---|---|---|
| Azure | Azure Durable Task Scheduler | Workers connect to the managed Azure endpoint |
| Local or disconnected | A persistent scheduler hosted within the local management plane | Workers connect to the local management endpoint, not Azure |

This revises the earlier Azure-connected-only framing. A portable provisioning
system should not require a local or disconnected deployment to use an
Azure-hosted scheduler. Backend placement is an environment concern; the tenant
onboarding and activity contracts should remain shared.

**The management-local backend is a design requirement, not a claim that the
Azure Durable Task Scheduler emulator provides production durability.** A
persistent self-hosted runtime compatible with the chosen Python worker SDK
remains to be selected and verified. See
[backend capability boundaries](#backend-capability-boundaries).

This is not an implementation plan or permission to change resource ownership.
The [state-removal plan](docs/plans/remove-state-dependency.md) remains a separate
scope that explicitly excludes adding a workflow engine. Combining that work
with this direction requires an explicit implementation scope.

This also complements [RECOVERY.md](RECOVERY.md). Workflow continuity after a
worker or scheduler restart is different from recovery after losing an entire
cluster. The availability-first direction can restore a verified baseline and
hold interrupted infrastructure work for deliberate continuation. Durable
pending work must not bypass that recovery policy or replay against an
unrecovered management plane. Radius-only PostgreSQL and scheduler storage
remain separate responsibilities.

## Design intent

Keep the application model, workflow contracts, and execution placement
portable while allowing native services in each environment.

- Azure deployments can use the managed Azure scheduler rather than operate
  their own scheduler service.
- Local deployments can host their scheduler and durable state in the local
  management plane, without an Azure subscription, login, or runtime connection.
- Workers remain separate from the scheduler's durable state. Replacing a
  worker must not erase accepted operations or completed workflow stages.
- Scheduler state belongs to its service, not the operator's checkout,
  `.state` directory, or a provisioner container's temporary filesystem.
- Radius retains its application and resource-lifecycle responsibilities.
  Durable orchestration coordinates administrative work; it is not a second
  infrastructure owner.

This extends the management plane. It does not introduce a fourth application
plane or move ordinary data requests through an orchestration service.

## Deployment shapes

### Azure-managed scheduling

```text
Management API
  -> Management PostgreSQL: tenant request, placement, operation identity
    -> Reliable dispatch
      -> Azure Durable Task Scheduler: execution history and pending work
               ^
               | authenticated scheduler connection
               v
         Privileged Python workers
               |
               -> Management Radius: child-cluster provisioning
               -> Child setup and Radius installation
               -> Child Radius: application and dependency deployment
               -> Observe results and update business operation status
```

The scheduler is an Azure-managed service outside the worker and management
AKS lifecycle. Worker placement can vary, provided the worker can reach the
scheduler and its administrative targets with the required identities.

### Management-local scheduling

```text
Local management plane
  Management API
    -> Management PostgreSQL: tenant request, placement, operation identity
      -> Reliable dispatch
        -> Local scheduler service
             -> Persistent execution store
             <-> Privileged Python workers
                    |
                    -> Management Radius: local child-cluster provisioning
                    -> Child setup and Radius installation
                    -> Child Radius: application and dependency deployment
                    -> Observe results and update business operation status

  Local credentials, artifacts, discovery, and operational visibility

No Azure scheduler or cloud connection is required for local operation.
```

The scheduler and its storage can be hosted within the management cluster or
other management-owned local infrastructure. The exact hosting and storage
topology remains open. They must not depend on the ephemeral worker filesystem.
Initial management bootstrap must make them available before workers can use
them; it cannot depend on an already-running onboarding workflow.

Backend selection is explicit at deployment configuration time. This is **not**
automatic failover from Azure to local when connectivity fails. Existing
workflow histories must not be assumed transferable between backends.

## Responsibilities and sources of truth

| Component | Responsibility |
|---|---|
| Management API and PostgreSQL | Validate and accept tenant requests; own placement, business operation status, and child reports |
| Scheduler and its durable store | Own execution history, checkpoints, timers, pending work, and execution coordination |
| Worker activities | Perform bounded, verifiable administrative operations through the appropriate owner |
| Radius | Own its logical resources, Recipe execution, output-resource relationships, and application lifecycle |
| Provider APIs | Supply authoritative current resource state and access metadata |
| Environment's credential services | Own secrets and credentials used by authorized workers and runtimes |

The business database and scheduler have different purposes. Neither should
become a second manually maintained endpoint or kubeconfig inventory.
Operation identities, template versions, and correlation references can support
recovery, but current endpoints, credentials, and resource state must be
obtained through their owners.

Worker-to-scheduler connection setup and authentication may differ between
environments. These differences belong in integration/configuration boundaries,
not environment-name branches in the shared application declarations.

## Common workflow contract

The direction is to share onboarding semantics across backend placements:

- An accepted tenant request has a stable business operation identity.
- A reliable dispatch mechanism eventually associates that request with one
  intended workflow instance, without silently losing the database-to-scheduler
  handoff.
- Shared-pair creation is coordinated separately from each tenant's onboarding.
- Administrative stages record intent and verified outcomes.
- A repeated activity inspects the existing operation and resources before
  deciding whether another mutation is necessary.
- Transient errors can use bounded retries. Permission, quota, configuration,
  and ambiguous ownership failures remain explicit and actionable.
- Replacement workers must understand in-flight workflow and artifact versions.

The current `Provider` interface in
[provisioning.py](src/plane_demo/management/provisioning.py) is a useful starting
boundary. Its `ensure_child_cluster()`, `bootstrap_child()`, and `deploy_plane()`
operations are not automatically safe to replay as whole activities.
Database initialization, certificate issuance, and other multi-step side
effects require their own recovery semantics.

Durable Task's activity model permits repeated execution. Checkpointing does not
make external side effects exactly once. The important failure window is an
operation accepted by Radius or a provider before the worker records its result.
Recovery must discover whether that operation is absent, running, completed,
or failed before acting.

A failed tenant request must not trigger deletion of a shared pair used by
other tenants. Cleanup and compensation remain owner-aware administrative
operations, not automatic rollback of every previously successful stage.

## What disconnected operation means

For a prepared local deployment, loss of Azure or internet connectivity should
not prevent local tenant onboarding, workflow progress, or recovery from a
worker or scheduler process restart.

That requires more than a local scheduler endpoint:

| Dependency | Disconnected requirement |
|---|---|
| Scheduler and execution store | Run locally and persist state locally |
| Authentication and credentials | Available through local management services without cloud token acquisition |
| Images, Recipes, tools, and provider packages | Prepared and available from local sources before external access is removed |
| Resource creation and discovery | Use the local provider, Radius, and Kubernetes APIs |
| DNS and transport | Resolve and reach the required local endpoints |
| Certificates | Have a local issuance/trust or renewal arrangement that does not require public ACME services |
| Operational visibility | Expose status and retained diagnostic information locally |

Preparation can have a documented connected phase. The disconnected claim
applies only after the required artifacts and dependencies are available
locally. A development emulator run is not evidence of restart durability.

Local mode must not silently fall back to Azure. Optional export of diagnostics
after connectivity returns must not become a prerequisite for local progress.
Provisioning Azure resources still requires Azure access; a local scheduler
does not make cloud API operations available offline.

If workers lose access to their local management scheduler, their workflows
still face a coordination outage. Hosting the scheduler locally removes the
Azure dependency, not the dependency on a reachable scheduler.

## Durability and failure boundaries

| Failure | Intended behavior and remaining responsibility |
|---|---|
| Worker process or pod replacement | Recover workflow progress from the selected backend; verify ambiguous side effects before repeating them |
| Local scheduler process replacement | Recover accepted work and history from persistent storage, not memory |
| Azure connectivity loss in local mode | Local onboarding continues without contacting Azure |
| Azure scheduler connectivity loss in Azure mode | Preserve the pending operation; do not create a competing local workflow |
| Target cluster temporarily unreachable | Wait or fail explicitly according to policy; do not claim resource readiness |
| Local management cluster or storage loss | Recover the scheduler, business data, Radius state, credentials, and required access configuration from their recovery mechanisms |
| Site or Azure region loss | Follow a separately defined disaster-recovery design; backend placement alone supplies no recovery-time or data-loss guarantee |

Co-locating the local scheduler with management creates a shared failure
boundary. Persistent storage, backup/restore, and management recovery therefore
matter independently of worker restart handling.

A PVC or database connection is not by itself a complete scheduler durability
solution. The backend must correctly persist accepted work, history, timers,
coordination state, and acknowledgements, including recovery after abrupt
failure and protection against conflicting worker execution.

Scheduler history does not restore Radius's own resource state or recover
lost secrets. Similarly, a restored Radius installation does not automatically
reconstruct the onboarding workflow. Those services need compatible recovery
boundaries without competing sources of truth.

## Portability and adaptive cloud

The revised story is:

> Shared application and onboarding contracts, environment-native provisioning,
> and a durable scheduler placed within the environment's management boundary.
> Azure can use a managed scheduler; disconnected environments keep scheduling
> and execution local.

| Dimension | Direction |
|---|---|
| Worker hosting | Portable Python/container workers with environment-specific access configuration |
| Workflow semantics | Shared stages, idempotency expectations, failure handling, and readiness contracts |
| Scheduler placement | Azure-managed or management-local, selected explicitly |
| Resource implementations | Radius Recipes and provider adapters use each environment's native capabilities |
| Disconnected operation | A local management backend removes mandatory Azure scheduler connectivity |
| Governance and telemetry | Azure Arc and Azure monitoring can be used when appropriate, but are not prerequisites for local execution |

This is a portability target, not a claim that every current SDK backend is
interchangeable. Shared workflow code is the goal. The exact Python client,
runtime protocol, configuration, and feature compatibility must be established.
An alternative runtime requiring different workflow semantics cannot be hidden
behind a connection-string change.

Application portability also does not mean identical cloud resources. The
current kind-based local provider is a demo implementation, not proof of
production Azure Local, other-cloud, or on-premises support.

Azure Arc can provide inventory, policy, deployment management, and monitoring
for connected sites. It need not be present for a disconnected site to schedule
its local onboarding work. Work placement must remain explicit so an activity
cannot run on an unintended site or with the wrong administrative identity.

## Backend capability boundaries

The intended architecture and the currently documented product capabilities
must be kept separate.

| Backend or integration | Evidence and status |
|---|---|
| Azure Durable Task Scheduler with the Python SDK | Documented managed-service integration; Python SDK is GA |
| Azure Durable Task Scheduler emulator | Documented local development tool; its orchestration/entity state is in memory and it is not suitable for production |
| Persistent management-local scheduler with the chosen Python worker SDK | Required by this direction; runtime, storage, protocol compatibility, and support model remain unverified |
| Moving a running workflow between local and Azure backends | Not established and not implied by worker or workflow portability |

Microsoft's current hosting-model documentation describes the standalone
Durable Task SDKs as using Durable Task Scheduler. That documentation does not
establish a production, persistent, disconnected backend selectable through
configuration.

The local direction therefore must not be implemented merely by putting the
standard emulator in a Deployment and adding a volume. A compatible persistent
self-hosted runtime must be identified and validated. If the desired native
integration cannot provide it, the backend/SDK choice must be revisited
explicitly rather than weakening the disconnected durability requirement.

## Audit and visibility

Each environment needs a queryable operation history connecting the tenant
request, workflow instance, execution attempts, artifact versions, Radius
resources, provider operations, and verified outcomes.

Azure mode can use the scheduler dashboard and Azure telemetry. Local mode
needs an equivalent local operational view; it cannot depend on a cloud
dashboard. Execution history retention and a durable audit archive are separate
concerns, even when both initially use local management services.

Credentials, kubeconfigs, and tokens must remain behind protected references,
not workflow inputs, outputs, or logs. Backend-local authorization must preserve
the separation between public APIs, dispatch, privileged workers, and runtime
applications.

This scheduler direction does not create Azure ARM deployment-history entries
for Radius's Bicep Recipes. Native ARM deployment integration remains a
separate topic, described in
[HOW_PROVISIONING_WORKS.md](HOW_PROVISIONING_WORKS.md).

## Existing contracts remain in place

This direction does not select a new child-cluster owner or an ARM/Terraform
execution model. Bootstrap still owns direct management-cluster creation;
management Radius owns child clusters; child Radius owns its applications.

Control and data reconcilers continue pulling from their parents. Ordinary
data requests still depend only on local configuration, Redis, and the data
API's own key. Tenant readiness remains control-record creation rather than
scheduler completion or transitive data-plane health.

No current database status values, service accounts, resource names, identities,
or cleanup contracts are changed by this document.

## Evidence needed before claiming the direction works

These are outcome requirements, not completed tests or an implementation plan:

- Both backend modes exercise the same intended onboarding and failure contracts.
- A local worker and scheduler can restart after accepted work without losing
  the operation or duplicating completed infrastructure effects.
- A worker can recover when a provider accepted a mutation before its result
  was recorded.
- Prepared local onboarding and recovery succeed with external network access
  blocked and without Azure credentials.
- Concurrent shared-tenant requests do not create competing shared pairs.
- Target-site routing, identity boundaries, and explicit error reporting hold
  after retry or worker replacement.
- Data-plane requests continue during a scheduler outage when their own local
  dependencies remain available.
- Local management recovery restores the required service-owned state without
  depending on a previous operator checkout.

## Sources and related documents

Sources reviewed September 14, 2026:

- [Durable Task SDK overview](https://learn.microsoft.com/en-us/azure/durable-task/sdks/durable-task-overview)
- [Durable Task Scheduler architecture and emulator limitations](https://learn.microsoft.com/en-us/azure/durable-task/scheduler/durable-task-scheduler)
- [Hosting models and supported storage backends](https://learn.microsoft.com/en-us/azure/durable-task/common/choose-orchestration-framework)
- [Activity execution and idempotency](https://learn.microsoft.com/en-us/azure/durable-task/common/programming-model-overview#activities)
- [Current provisioning walkthrough](HOW_PROVISIONING_WORKS.md)
- [Recovery design direction](RECOVERY.md)
- [State-removal plan](docs/plans/remove-state-dependency.md)
- [Current architecture](docs/architecture.md) and [accepted demo limitations](docs/limitations.md)
