# Remove workstation state from the three-plane POC

This ExecPlan is a living document. Keep `Progress`, `Surprises & Discoveries`,
`Decision Log`, and `Outcomes & Retrospective` current. It uses the OpenAI
Cookbook ExecPlan format. On September 14, 2026 the user approved implementation,
rubber-duck/security review in every step, verification, commits, and pushing
verified work. A subsequent instruction requires all operator/demo scripts
under `scripts/` and Make-driven entrypoints from the repository root.
The operator layer should use small Bash scripts and native CLIs for
straightforward sequences, not a new Python orchestration framework.

This is the current plan for the simplification work. It supersedes the original
plan's requirements for durable `.state` files, exported endpoint inventories,
provisioner filesystem credentials, and required local evidence directories.
It does not rewrite the completed runs recorded in `FINDINGS.md`.

## Purpose / Big Picture


An operator should configure the demo once with a small `demo init` command,
then run the Azure or local walkthrough from a checkout containing only source
and a git-ignored `.env`. The operator must not recover a previous `.state`
directory to inspect, use, or remove an existing deployment.

Azure application secrets belong in one shared project Key Vault. Cluster
access and endpoints come from live Azure, Radius, and Kubernetes APIs, not
files or database columns. The management database retains tenant placement,
operation progress, and application reports. Parameters and manifests are
generated when used and discarded afterward.

Local demonstrates the same tenant, configuration, counter, and outage behavior
using Docker Desktop, kind, Kubernetes, and local scripts. It does not require
an Azure login, Key Vault, a subscription, a cloud registry, or cloud APIs.
Neither environment depends on checkout files mounted into running clusters.

The POC remains about Radius and the three planes. Do not add cost controls,
approval workflows, a secret-management framework, a discovery service, or a
general-purpose workflow/retry engine.

## Progress


- [x] (2026-09-14) Read the current implementation and the user's approved simplification inputs.
- [x] (2026-09-14) Draft the revised ownership, `.env`, discovery, local, and acceptance contracts.
- [x] (2026-09-14) Initial security review found no vulnerabilities; rubber-duck review identified missing execution details.
- [x] (2026-09-14) Both follow-up reviews passed after clarifying private-vault client access, naming, command stages, SQL initialization, local artifacts, orphaned-fault recovery, and node-owned encryption.
- [x] (2026-09-14) Transaction-owned schema initialization implemented, reviewed, and verified on real disposable PostgreSQL 17.8; trigger/index enforcement drift fixes verified. Committed/pushed as `dd1526b`.
- [ ] Complete script relocation under `scripts/operations`, `scripts/harness`, and `scripts/recipes` with root Make entrypoints.
- [ ] Implement the `.env` initializer and shared configuration loader.
- [ ] Implement temporary workspaces and live metadata discovery.
- [ ] Move Azure credentials to Key Vault and local credentials to Kubernetes.
- [ ] Remove database endpoint inventories and filesystem operation markers.
- [ ] Simplify local bootstrap and remove checkout-file mount dependencies.
- [ ] Wire manual commands, API response contracts, scenarios, and cleanup.
- [ ] Prove Azure first and local second, including fresh-checkout and empty-worker recovery.
- [ ] Retire the old runtime paths, update documentation, and commit the verified implementation.
- [ ] After implementation and verification, consolidate `docs/`, remove obsolete/duplicate documents and this ExecPlan, and update all surviving links.

## Surprises & Discoveries


The current `Credentials.runtime_seed()` includes management credentials only.
Starting an empty provisioner directory can restore that seed but not existing
child passwords. A new generated password is not a recovery of the password
already installed in PostgreSQL.

`AzureProvider.get_access()` already calls `az aks get-credentials`. Durable
kubeconfigs are therefore an implementation choice, not necessary discovery
state. Radius 0.60.2's CLI does need a scoped HOME containing its kubeconfig;
a temporary HOME can meet that requirement without becoming durable state.

`management.pairs` currently stores `control_cluster_id`, `data_cluster_id`,
`control_url`, and `data_url`. `OperationStore.complete()` writes them only when
the entire pair finishes. Removing exported JSON alone would leave this second
endpoint/infrastructure inventory in place.

The current local bootstrap bind-mounts
`.state/local/management/encryption.yaml` into the kind node. That is an actual
running-cluster dependency, not merely a CLI cache. Its replacement must be
proved across management node stop/start, not just by deleting host metadata.

There is an irreducible local execution constraint: kind creates Docker
containers. The current management Radius executor therefore needs access to
Docker Desktop. Moving or deleting files cannot remove that need. This plan
removes checkout-file mounts and host-specific access files, not the standard
container runtime needed by kind. The minimal Radius executor access described
below remains explicit.

The repository already ignores `.env` and `.env.*` in Git and excludes them
from Docker build contexts. Verify and preserve those rules when adding the
initializer; do not put actual credential examples in `.env.example`.

The existing vault is private. A human's `az login` authenticates the operator
but does not make the vault endpoint reachable from the workstation. API clients
can read their selected plane's already-injected demo key through authenticated
Kubernetes access; that is distinct from granting a data API Pod Secret access.

Local currently downloads Terraform in `LocalProvider.TERRAFORM_INIT`, and child
installation obtains Radius charts/images and Terraform providers. Eliminating
Azure calls alone would not prove that a prepared local deployment can run with
external access blocked. Dependency preparation and consumption are specified
separately below.

## Decision Log


**Decision, user, September 14:** Azure application credentials use the shared
Key Vault. Redis's service-owned access key and Radius's own internal state
retain their existing owners; do not create another independently mutable copy.
Gateway certificates and ACME account material already use Key Vault.

**Decision, user, September 14:** Endpoint and kubeconfig discovery must query
APIs. Do not save discovery results in the database, `.env`, or an exported
inventory file. This applies to management, control, and data API access and
to manual and automated scenario commands.

**Interpretation used by this plan:** “Nothing should go to db” refers to endpoint,
kubeconfig, and infrastructure discovery metadata. It does not remove tenant
records, desired configuration, counters, operation status, or reports, because
the user explicitly retained database-owned markers/evidence. It also does not
change child-initiated PostgreSQL polling into HTTP configuration propagation.

**Decision, user, September 14:** A small initialization command collects the
starting configuration and creates or replaces a git-ignored root `.env`.
Every operator command reads this same configuration. It may contain explicitly
provided credentials when needed; it must not become a dump of discovered
resources or a second Azure credential store.

**Interpretation used by this plan:** “Always query APIs” means fresh discovery
at each independent operator action or explicit endpoint query. It does not
make each data request query Azure, management, or Key Vault. A running process
still needs its injected database/Redis connection settings; those are derived
runtime bindings, not database inventory. The data request path remains local.

**Decision, user, September 14:** Local must have no cloud dependency. Scripts
use kind and Kubernetes to initialize and inspect local resources. Kubernetes
Secrets/PVCs and kind node storage are normal runtime state, not copies in the
checkout. No host-user-specific paths, checkout bind mounts, file-hosted Recipe
URLs, or cloud credentials are allowed.

**Preserved architecture contract:** Bootstrap alone directly creates the
management cluster. Child clusters are still created by management Radius
through its kind Recipe; child applications remain owned by child Radius.
Do not disguise direct Python or host-script child creation as Radius ownership.
A standard Docker connection for the local kind executor remains necessary.
A requirement to remove even that connection would require a different local
execution architecture and is not claimed solved by this plan.

**Decision, September 14:** Keep the approved singleton and explicit interrupted
operation behavior. Move markers to their real owners, but do not interpret
rediscovery as permission to replay a partially completed operation automatically.

## Outcomes & Retrospective


The prior implementation proved the full scenario and was torn down. Those
results establish a behavioral baseline, not proof of this simplification.
Implementation is in progress. Transaction-owned database initialization is
committed and pushed after real PostgreSQL verification and both review passes.
The `.env` configuration primitives have 25 passing focused tests; their Make
entrypoint and script relocation are still being integrated. Credential-store
primitives are being implemented separately. No revised demo environment has
been deployed, and the later integration/live milestones remain open.

The intended outcome is fewer authorities, not a different directory name.
The final implementation must function without `.state`, with an empty
provisioner workspace, and with no endpoint inventory in PostgreSQL.

## Context and Orientation


The repository is `macromania/radius-todolist-app`. The baseline for this plan
is commit `f65ed11`. `README.md` links to the self-paced
`RUN_AZURE_SCENARIOS.md` and `RUN_LOCAL_SCENARIOS.md`. The untracked
`HOW_PROVISIONING_WORKS.md` is unrelated user work and must not be edited,
staged, or removed by this task.

The management API in `src/plane_demo/management/api.py` validates tenant
requests. Its non-public worker in `management/provisioner.py` uses
`management/provisioning.py` and `management/providers/` to provision missing
control/data pairs. Control and data reconcilers pull PostgreSQL records from
their parents. The data API uses only its own key, local ConfigMaps, and Redis.

`sql/management.sql` defines tenant placement, a singleton operation handoff,
and management events. `sql/control.sql` defines desired configuration and
data reports. Existing login roles and row-level access rules remain in place.
This work removes discovery columns and file markers, not those access rules.

`infra/bootstrap/azure.bicep` creates Azure management and the shared foundation.
`infra/radius/apps/` contains the three environment-independent applications.
Types define Radius APIs; Recipes implement Azure or local resources.
`scripts/operations/local/` currently adds Docker/kind execution, persistent host
records, and image inspection. `scripts/harness/` consumes exported state for API calls,
faults, and acceptance. All these operator surfaces must use the new loader
and live discovery, not only a new front-end command.

Two terms matter. “Starting configuration” is the small set of values selected
by the operator to identify a deployment. “Discovery metadata” is information
returned by an existing resource, such as a gateway URL or kubeconfig. Only
the former belongs in `.env`.

## Target Contracts


### One starting configuration, not a hidden deployment inventory


Use root Make targets backed by small scripts under `scripts/operations/`;
do not add another root shell-script frontdoor. Prefer Bash plus native CLIs
for initialization and straightforward operator sequences, keeping a narrow
typed Python configuration helper only where shared runtime use justifies it.
The first target is `init`. These are proposed commands, not commands
available at the baseline:

    make init ENV=azure
    make init ENV=local

Interactive use prompts for missing starting values. Explicit flags support
repeatable use without a terminal. At minimum, collect environment, project
name, deployment name, and, for Azure, subscription and location. Azure can
suggest the current `az account show` subscription; selection is written to
`.env` and supplied explicitly to later Azure calls, never applied as a global
CLI default. Local initialization must not invoke Azure even to suggest defaults.

A non-secret Azure example is:

    DEMO_ENV=azure
    DEMO_PROJECT=radplanes
    DEMO_DEPLOYMENT=learning
    AZURE_SUBSCRIPTION_ID=<subscription-uuid>
    AZURE_LOCATION=centralus

A local example is:

    DEMO_ENV=local
    DEMO_PROJECT=radplanes
    DEMO_DEPLOYMENT=learning

Use `ports.env` for the reserved local port block. Tool, image-base, and provider
versions stay in source. An explicitly selected existing Key Vault name or
application/Recipe revision may be an optional configuration input; discovering
the resulting URI or resource ID must not append it to `.env`.

`init` atomically replaces the canonical `.env`; it never appends another
environment's settings. Validate everything before replacement. Show a masked
summary of which environment/project/deployment will be selected. Re-running
with the same values leaves resource identities unchanged. Switching to local
removes Azure-only values from the newly generated file and makes no cloud calls.
Replacing `.env` changes the selected target, not the resources themselves.

Implement a small typed loader in `scripts/operations/config.py` with a strict,
documented key/value format. Do not execute `.env` with `source`, `eval`, shell
substitution, or interpolation. Support quoted literal values so punctuation in
credentials round-trips. Reject duplicate/unknown keys and malformed identifiers
with explicit errors. In particular, the strings `$(...)` and backticks in a
value must remain literal or be rejected, never executed.

Use owner-only file permissions. Keep secrets out of arguments, command traces,
logs, API responses, and examples. Read optional credentials through a masked
prompt or a supplied environment variable, then write only requested values.
The loader may accept per-plane demo-key overrides for setup or client access;
it must not automatically export all Key Vault secrets. When a deployed secret
already exists, setup must reuse it or report a supplied-value conflict, not
silently rotate it because `.env` changed.

Every operator entrypoint loads configuration from the same repository root
`.env`. Do not silently choose a different target from conflicting process
variables. A running workload receives only its needed settings in existing
Kubernetes ConfigMaps/Secrets; do not mount or copy the complete operator `.env`
into containers. Local execution must also work when Azure SDKs and `az` are
unavailable, so provider imports must be lazy or otherwise environment-specific.

### Live discovery, with no database fallback


Use a concrete deployment identity everywhere. `DEMO_PROJECT` and
`DEMO_DEPLOYMENT` are lowercase alphanumeric/hyphen identifiers with a combined
length that leaves room for the longest slot and Kubernetes namespace suffix.
Validate the derived names against each provider's length/character limits;
do not silently truncate. A canonical stem
`<project>-<deployment>-<environment>` distinguishes physical resources.
Azure resource groups, kind cluster names, Kubernetes namespaces, and ownership
labels derive from this stem. Azure globally unique registry/vault names use a
deterministic hash of subscription plus project/deployment/environment, not a
saved random salt. Logical plane names, pair IDs, database login roles, Radius
resource types, and status values remain unchanged.

All resource selection uses that identity. `.env` replacement with another
deployment selects disjoint resource names, but local port allocation still
comes from the one reserved block in `ports.env`: local deployments must run
sequentially. Recreating a deleted retention-protected vault with identical
inputs can still fail while Azure reserves its name. Report that provider
condition; choose another deployment identity or wait, never silently purge
or adopt another vault.

Introduce a small provider-specific discovery interface in
`src/plane_demo/management/providers/discovery.py`. It resolves a logical slot
such as `shared-control` from the selected deployment. Azure uses Azure resource
APIs, Radius resource outputs, and Kubernetes; local uses kind/Docker and
Kubernetes plus Radius resource APIs. Use explicit resource scope, the selected
deployment identity, and known logical names. Do not scan by a broad name prefix
and adopt the first matching resource.

Azure cluster access uses ordinary `az aks get-credentials`, not `--admin`.
The command can return YAML to stdout for in-memory parsing or an owner-only
temporary file. Keep CA verification and the existing authentication model.
If Radius requires HOME-based discovery, generate a temporary scoped HOME and
kubeconfig for that operation. Do not change global kubeconfig or Radius state.

The management gateway is discovered from its Radius/Azure owner. Tenant
placement comes from the management API; the child's gateway, datastore
endpoint, and Kubernetes access come from the child/resource owner APIs.
No management database URL column, `endpoints.json`, `acceptance.json`, or
previous run output may substitute for a failed discovery call.

`make endpoints` and `make api ARGS='management|control:PAIR|data:PAIR ...'` resolve
the target at command time. `make kube ARGS='SLOT ...'` obtains fresh scoped access.
Scenario tools use the same interface and actual plane APIs; they do not query
SQL for endpoint inventory. A discovery outage is reported as such rather than
an empty inventory, stale URL, or successful health result.

Within one bounded command, an in-memory snapshot may hold a resolved identity
to keep all its actions on the same object. Revalidate the UID/resource identity
before a destructive mutation. Do not retain that snapshot after the command
as another mandatory file or database inventory.

Remove the four discovery columns from fresh management schemas:
`control_cluster_id`, `data_cluster_id`, `control_url`, and `data_url`.
Retain logical pair assignment, desired configuration, and operation status.
Update `OperationStore.complete()`, `provision_pair()`, API response models,
exporters, tests, and manual guides together. Shared reuse must query the live
Radius pair rather than trust stored cluster IDs.

The management tenant response should describe tenant/provisioning/report
state without saved endpoint fields. Expose URLs through the explicit live
operator discovery command instead. This intentional response-shape change must
be documented and tested, with no compatibility fallback to database URLs.
If a future API endpoint exposes connection metadata, it must use live discovery.
Do not give all public APIs administrative cloud permissions just to preserve
redundant URL fields.

Do not change the steady-state traffic model: control pulls management
PostgreSQL; data pulls control PostgreSQL. Required DSNs and Redis settings
are resolved and injected during bootstrap/startup and kept only as local
runtime bindings. Data requests must not start depending on management,
control, Key Vault, or Azure discovery availability.

### Credentials have one durable owner per environment


Reuse the project's shared Azure Key Vault for per-plane API keys and runtime
PostgreSQL passwords. Generate once and store before installing the matching
database login. Redis credentials remain supplied by the service/Radius
connection. Certificates and ACME accounts retain their existing vault storage.
The provisioner reads/writes its required application-secret names using its
workload identity; public APIs receive their own injected credentials, not
vault-wide access.

Local stores the equivalent once-generated values in Kubernetes Secrets in
the relevant namespace. Keep source labels and namespace ownership, and use
the operator/bootstrap or existing provisioner authority, not public API Secret
access. No local call to Key Vault or another cloud secret service is permitted.

Replace the filesystem `Credentials` implementation with a narrow
`CredentialStore` interface that can get or create an owned value and detect
conflicts. Avoid a generic secret synchronization framework. Azure's store is
Key Vault; local's store is Kubernetes. Reading existing values must not create
new versions or rotate passwords. A missing secret for an existing initialized
database is an explicit incomplete state, not permission to generate a new login
password and claim recovery.

Remove `PROVISIONING_CREDENTIALS_JSON` as a replay seed and the durable
`credentials.json` files after the two stores are fully wired. Remove the
`provisioner-state` and Azure operator working-data PVC requirements once all
their consumers have moved. Normal temporary files and an `emptyDir` workspace
are sufficient; public-runtime image boundaries remain unchanged.

Replace the worker's full `PROVISIONING_CONFIG` inventory with a small
`provisioning-settings` ConfigMap containing selected environment, project,
deployment, Azure subscription/location when relevant, and an explicitly
selected existing vault name if configured. The worker reads it as environment
settings into a typed `BootstrapIdentity`; it discovers actual foundation,
allocation resources, Recipe references, and cluster access at startup and
before their use. Its own `MANAGEMENT_DSN` remains a role-specific Secret-backed
connection binding. Child DSNs and Redis settings are similarly injected only
into their actual consumers. Neither the full `.env` nor a copied infrastructure
inventory belongs in a workload ConfigMap.

An existing externally selected shared vault is not owned by demo teardown.
Remove only the demo-owned secret/certificate objects when explicitly selected
for deletion, and do not delete the vault or unrelated objects. A vault created
by the demo foundation follows the foundation's normal owner lifecycle.

### Progress and measurements are not infrastructure discovery


Keep `management.operations` and `management.events` as the source for accepted,
running, succeeded, failed, and interrupted work. Record a stage before a remote
action and its observed result afterward using the current singleton session.
Use logical slots and operation IDs, not copied kubeconfigs/endpoints or a full
provider inventory. Query resource owners to establish what actually exists.
The DB transaction and a cloud action are not one atomic transaction.

Remove `*-intent.json`, `*-complete.json`, `*-cluster.json`, and similar
filesystem authorities from live run paths. Before management PostgreSQL exists,
bootstrap queries Azure deployment/Kubernetes initialization state. Database
initialization uses its actual schema/initialization marker and a stable secret
owner. It cannot require the database to already contain its own bootstrap marker.

Add an owned `demo_metadata.schema_version` record in each PostgreSQL database,
written in the same transaction as the application schema, role configuration,
and grants. Record schema kind, version, logical pair when applicable, and the
initialization operation ID, not endpoint metadata or passwords. Grant the
matching administrative/runtime role just enough read access to verify it.
Update `src/plane_demo/setup/bootstrap.py` and
`management/providers/workloads.py` to inspect this fact before initialization.

If SQL commits but Job acknowledgement is lost, discovery reads the version
record and verifies the expected schema/security definitions against it. An
exact match is reported as initialized without replaying DDL or changing
passwords. A version conflict or partial schema is reported explicitly. If the
database cannot be reached, its outcome is unknown, not absent. Store stable
runtime credentials before the transaction so this check does not require a
lost setup password. Kubernetes Job status and the existing initialization
ConfigMap can remain observations, but are not authorities over a committed
schema.

Keep application reports in their current parent databases. Manual scenarios
query the plane APIs and resource APIs. Optional automated scenario measurements
may be stored in a small clearly test-owned database record or emitted to stdout;
they must not be a prerequisite for API use, deploy, or cleanup. Do not copy
the entire evidence directory into SQL.

Active fault restoration needs a recoverable description even if the host
command exits. Store the fault's non-secret ID, target UID, rule/policy reference,
and restoration status in an owned Kubernetes ConfigMap alongside its target,
before mutation. Azure's actual Cilium policy and local's actual Pod network
rule remain the mechanisms. A later `make fault ARGS='restore ID'` discovers and
restores the exact owned fault without a local journal pathname. Do not store
access credentials or treat a journal's claim as proof that the rule was removed.

The journal must contain the complete restoration identity: environment,
project/deployment, logical slot/component, cluster and namespace UIDs, Pod UID,
the exact target address/port and rule identifier, and before/after status.
For local, include Docker node ID, container sandbox ID, network namespace
identity, the exact inserted rule, and the original rule-set fingerprint.
For Azure, include the exact Cilium policy name/spec/UID and the original scoped
policy inventory needed to verify restoration. These are observations for one
fault, not reusable endpoint discovery or access credentials.

If interruption happens between journal creation and mutation, or between
physical restoration and the final status write, the next command examines the
actual rule/policy and identity to determine the result. Never remove another
rule or adopt a replacement Pod because the old target is unavailable.

Full teardown can remove the database that held reports. Keeping a historical
measurement report after teardown is an optional explicit export, never a file
needed to operate the next run. Azure Activity Log is useful for resource
operations, not a substitute for measuring successful application requests
during a fault.

### Local means cloud-free, not without a container runtime


Keep the five-cluster local topology and the same application declarations.
Local bootstrap and observation are script-driven through kind and Kubernetes.
Recipes must run through Radius, not `file://` paths or hidden cloud registries.
Use locally built and inspected images, loaded through the existing local
container runtime. Public dependency downloads during tool/image preparation
are distinct from an Azure runtime dependency; once those dependencies are
available, local deployment and scenarios must work without cloud access.

Make `make build` prepare the complete local dependency set before bootstrap:
the pinned kind node and workload images, Radius chart and all rendered Radius
images, Terraform binary and provider packages, and the existing administrative
CLI binaries. Store reusable dependencies in the local Docker image store and
standard tool caches, not as authoritative deployment records. Package chart,
Terraform/provider mirror, and required administrative files in the appropriate
operator/executor image; do not mount a host dependency folder into workloads.
Import the required images into management and each new child before starting
their Kubernetes workloads. The Radius-owned child Recipe remains responsible
for child image import.

Replace the network download in `LocalProvider.TERRAFORM_INIT` with copy/use
of the pinned packaged binary. Configure Terraform to consume the packaged
provider mirror and child Radius installation to use the packaged chart.
Internal Recipe HTTP services may serve immutable archives from in-cluster
images/ConfigMaps; they must not reference the checkout. Test the first
child creation and deployment with external access blocked after preparation,
not only a previously warmed cluster. This is artifact preparation, not another
long-lived host service or cloud dependency.

Remove the host encryption-file bind mount while retaining management Secret
encryption. First create the owned kind management node without a reference
to a nonexistent encryption file. Generate/install an owner-only key inside
that node's persistent filesystem, update the API server's static Pod
configuration using node-local paths, and wait for it to restart successfully.
Only then create Radius and demo Secrets. Rewrite any preexisting Kubernetes
Secrets if claiming all Secrets are encrypted; inspect actual ciphertext.
Do not retrieve the key from the encrypted store it is needed to open.

Prove management node stop/start with the same node container and no operator
scratch files. This proves that node's persistence, not recovery after deleting
the node. Run this as a bounded bootstrap experiment before replacing the
working path. Do not keep the checkout mount as an undocumented fallback.

Standard Kubernetes Secret, ConfigMap, and PVC mounts, kind port mappings, and
the management Radius executor's Docker connection are normal infrastructure
primitives that remain. Remove custom mounts of checkout paths and personal
CLI directories. Limit the executor image/setup to the tools actually required
to create kind children through Radius. No Docker access in API or runtime
provisioner containers, no new host agent, and no fake Azure IDs.

### Temporary files are not a recovery protocol


Source templates and intentional configuration remain durable in Git and `.env`.
Compile Bicep and build parameter/Job manifests in memory or a command-scoped
temporary directory. Some CLIs require a pathname; that requirement does not
make the file authoritative or reusable after the command.

Use private temporary directories with deterministic cleanup in normal and
error paths. A killed process may leave scratch files, but future commands
must not need or trust them. Keep generated Bicep extensions buildable from
source. `.state/check` may remain an optional development scratch convention;
no deployed workload or manual scenario may require it.

## Plan of Work


### Milestone 1: establish the `.env` entrypoint


Add root Make targets, a small initialization script, shared configuration
parsing, `.env.example`, and focused tests under `tests/operations/`. Keep the
entrypoints as thin calls to existing tools, not a replacement orchestration
framework. Every command
must receive the same typed starting configuration. Replace hard-coded operator
subscription/project/deployment inputs at the boundary; retain valid existing
logical role names, component labels, and five slot names.

This is not only a command-line substitution. Update `OperatorConfig.from_dict`
in `management/provisioning.py`, `LocalConfig.from_dict` in
`management/providers/local_config.py`, `scripts/operations/project.py`,
`infra/bootstrap/*.bicep`, the local cluster Recipe naming locals, and the
export/fault/cleanup target validators that currently require fixed project,
region, or resource names. They must share the deterministic naming contract
above, without broadening selectors to arbitrary resources. Source constants
for database login roles, component names, and fixed logical slots remain
separate from the selected physical deployment identity.

Derive owned resource naming consistently from the selected project/deployment,
without a hidden `name-salt.json`. An initialized `.env` is sufficient to
identify the same deployment from another checkout. Verify prompted/noninteractive
input, same-value reruns, atomic replacement failure, local/Azure switching,
literal special-character credentials, ignored secrets, and no accidental
cloud lookup for local initialization.

### Milestone 2: implement live discovery and disposable workspaces


Add the discovery interface and provider-specific implementation. Wire Azure
access methods in `management/providers/azure.py`, kind/Kubernetes access in
`management/providers/local.py`, and temporary CLI homes in
`management/providers/commands.py`. Refactor `scripts/operations/project.py` bootstrap
output handling and `scripts/operations/run-management-job.py` so a fresh operator
discovers existing outputs instead of rebuilding a saved configuration bundle.

Complete the currently manual Azure artifact handoffs as part of this wiring:
build/publish returns exact inspected image references and resolves the selected
immutable Recipe revision. Do not require a human to merge `images.json`,
`recipes.json`, and `bootstrap.outputs.json` into `provisioning.json`.
Actual image filesystem inspection remains a technical verification step,
not a durable approval-file requirement.

Update the explicit copied-source lists in `images/provisioner/Dockerfile`
and `images/local-provisioner/Dockerfile` for the new operator configuration
and discovery helpers. Update `scripts/operations/local/runtime-images.py`,
`scripts/operations/build-images.py`, and `scripts/harness/test-e2e.py` source inspection
manifests at the same time. Verify the built administrative images contain the
actual new entrypoints; keep those helpers and secret-store libraries out of
the public API image unless they are genuinely needed by its allowed runtime.

Verify a second checkout with the same `.env` and login can resolve management
and both child pairs without copying any generated file. Mock and live
discovery failures must remain visible; no stale-data fallback is permitted.

### Milestone 3: replace filesystem credential persistence


Implement the two credential stores and update
`management/providers/credentials.py`, `management/providers/workloads.py`,
`management/provisioner.py`, `scripts/operations/deploy-plane.py`, and both provider
prerequisite paths. Add required secret-object access under the existing
bootstrap identity model; do not grant runtime role delegation.

Generate a new plane's keys/passwords once, record them with the appropriate
owner, initialize the database, and inject runtime Secrets. An empty worker
workspace must read those same values. Verify existing passwords and counters
survive worker replacement; a mismatching operator-supplied value must not
silently change database credentials.

Only after that proof remove state-volume mounts from
`infra/radius/apps/management.bicep`, provider prerequisites, and operator Jobs.
Keep `fsGroupChangePolicy: OnRootMismatch` where volumes still exist, public
image allowlists, Azure verified PostgreSQL TLS, and Redis's lowercase
connection and existing password encoding.

### Milestone 4: use DB progress and live owners consistently


Update `sql/management.sql`, `src/plane_demo/shared/db.py`, `management/provisioning.py`,
`management/api.py`, and both providers. Remove discovery columns and marker
file reads/writes. Prefer the existing operation/event schema rather than new
step/lease/workflow tables. Preserve singleton locking, duplicate behavior,
child-initiated reporting, and explicit interrupted status.

Use fresh POC databases for this schema change. Do not perform a speculative
in-place migration on retained/live databases. Provide a clear version mismatch
error if an old schema is selected. Test the response-shape change and shared
reuse from live Radius observations instead of saved URLs or IDs.

Update `management/provisioner.py::read_pair()` explicitly: it currently selects
all four columns being removed. Update `src/plane_demo/setup/bootstrap.py`
and the management/control schema setup to write and verify the transaction-owned
schema version described above. Test SQL-commit/acknowledgement-loss and
password reuse from the actual initialization entrypoint, not only a helper.

### Milestone 5: finish the cloud-free local path


Prove the node-owned encryption bootstrap experiment. Then update
`scripts/operations/local/bootstrap.py`, `scripts/operations/local/setup-demo.py`,
`scripts/operations/local/deploy-demo.py`, local Recipe publication and image access,
and the management executor setup. Preserve child ownership through Radius
while eliminating checkout mounts and durable host records.

Do not force local through an Azure-shaped configuration schema. Start and
operate local with no Azure CLI installed, no cloud credentials in the
environment, and cloud service access blocked after dependencies are prepared.
Verify every constructed subprocess environment and provider import path,
not merely the top-level CLI branch.

### Milestone 6: wire the guides, APIs, fault controls, and cleanup


Replace required `scripts/harness/api.py` endpoint-file lookup with live discovery.
Refactor `scripts/harness/export-state.py`, `scripts/harness/local/export-state.py`,
`scripts/harness/test-e2e.py`, and both fault implementations. Any retained export command
is an optional report command; it must not prepare hidden prerequisites for the
next action.

Add `make endpoints`, `make api`, `make kube`, and fault/status/cleanup
subcommands as thin uses of the shared resolver. Every plane target and
scenario, including reused pairs, must follow the same API-only discovery path.
Do not maintain an old `.state` path as a silent fallback.

Cleanup discovers actual owner graphs through Azure/Radius/Kubernetes even
when the management API/database is stopped. Restore active faults from owned
resource records, quiesce management, delete child apps through child Radius,
delete child clusters through management Radius, and remove management/foundation
last. Use live resource scope and ownership, not blanket prefix deletion.
Confirm absence with fresh owner API queries; report failed calls explicitly.

Update both `RUN_*_SCENARIOS.md` guides with self-contained `.env` initialization,
short commands, explanations, checkpoints, and no required export-state stage.
Keep README a short Azure-first/local-second index. Update `docs/contracts.md`,
`docs/architecture.md`, provisioning/cleanup docs, `AGENTS.md`, `DECISIONS.md`,
and `FINDINGS.md` to distinguish the new behavior from historical evidence.

### Milestone 7: prove the result and remove dead paths


Run offline checks, then real Azure and local scenarios using fresh deployments.
Build and inspect changed images before deploying. Test actual empty-worker
and fresh-checkout behavior in addition to unit tests. Run rubber-duck/security
reviews for implementation phases and fixes, recording actual findings and
results, not merely source expectations.

Remove obsolete credential seeds, saved endpoint inventories, generated-config
assemblers, persistent working PVCs, and file marker helpers only after all
their callers use the new owners. Do not bulk-delete historical `.state`
evidence or the unrelated user file. Retain optional scratch/report export as
clearly disposable output. Commit reviewed, verified changes in coherent phases.

The user explicitly requested a lean final `docs/` folder. Once the implementation
and its verification are complete, consolidate useful current material, delete
obsolete or duplicate documents under `docs/`, and delete this ExecPlan itself.
Keep the root Azure/local scenario guides as the manual walkthroughs and fix
README, AGENTS, and reference links. Do not delete the active plan early or
silently remove unrelated user-authored root design documents.

## Concrete Steps


All commands run from the repository root. The following are existing offline
checks to use during implementation:

    uv sync --locked
    make check

The proposed command stages have distinct boundaries. `init` writes `.env`
only. Azure `bootstrap` performs the scoped foundation deployment and management
Radius installation, using the existing project/install operations; it returns
only after actual completion. Azure `build` then uses the discovered ACR to
publish Recipes and build/inspect application images. Local `build` comes before
bootstrap because it prepares the executor, chart, binaries, provider mirror,
and images needed to start management without external downloads. Local
`bootstrap` creates management, installs node-owned encryption and Radius, and
loads the prepared dependencies.

In either environment, `deploy-management` discovers the selected artifacts,
registers the types/environment/Recipes, initializes PostgreSQL and its roles,
injects scoped ConfigMaps/Secrets, deploys management's API/provisioner/gateway,
and waits for the actual completion/health/readiness checks. Azure keeps its
certificate issuance path; local does not invoke it. Reuse
`scripts/operations/deploy-plane.py`, `run-management-job.py`, and local setup/deploy
functions behind these commands rather than maintaining two independent
implementations. A submitted asynchronous Job is reported as submitted until
its completion is observed.

The following is the intended command surface to implement. Do not claim these
commands exist until their milestone is complete:

    make init ENV=azure
    make bootstrap
    make build
    make deploy-management
    make endpoints
    printf '%s' '{"tenant_id":"shared-a","isolation":"shared","initial_message":"alpha"}' | make api ARGS='management POST /tenants'
    make api ARGS='management GET /tenants/shared-a'
    make api ARGS='control:shared GET /tenants/shared-a'
    make api ARGS='data:shared GET /tenants/shared-a'

The API response still distinguishes accepted provisioning from ready control
records and applied data configuration. Repeat manual scenarios for `shared-b`
and `isolated-c`. Fault commands must target logical slots and return a
discoverable fault ID rather than a local file dependency:

    make fault ARGS='start --slot shared-control --component control-reconciler --duration 60'
    make fault ARGS='status'
    make fault ARGS='restore <fault-id>'
    make clean
    make verify-clean

For local, initialize the same file for local and follow the same logical
commands. Initialization itself does not delete or change an Azure deployment:

    make init ENV=local
    make build
    make bootstrap
    make deploy-management
    make endpoints

Normal operation must not require manual `source .env`, `export_state`,
`acceptance.json`, a particular working directory under `.state`, or copying
a prior run's credentials. Explicit source compilation/build tools may produce
scratch files, but operations must create their required scratch themselves.

## Validation and Acceptance


Configuration acceptance requires `.env` replacement to leave one complete,
validated target configuration. A local initializer runs without Azure installed
or logged in. Git and Docker ignore probes must confirm actual `.env` values
cannot enter commits or images; tests use synthetic secret strings and verify
they do not appear in logs. Every public command uses the same loader.

Fresh-client acceptance starts from a new checkout containing source plus the
same `.env`, with no `.state`, `.rad`, exported kubeconfigs, or saved images/
Recipes/endpoint manifests. Ordinary user tool installations and Azure's own
login cache are allowed. Query existing tenants, resolve all plane endpoints,
update a configuration, increment a counter, inspect progress, and perform
owner cleanup without copying prior generated files.

Metadata acceptance removes endpoint/cluster inventory columns from the schema,
not just their writers. Query construction and response tests prove no hidden
database fallback. Change a discovered endpoint in a controlled test and require
the next independent client command to query the owner API and use the new
value. A failed discovery request must not return a stale value or silently
use another scope.

For `make api`, first discover the selected plane's cluster and namespace.
An authenticated operator reads only `DEMO_KEY` from that plane's existing
`<role>-api-runtime` Kubernetes Secret, in memory, and sends it to the discovered
API. This is a read by the operator, not a new permission on the public API.
It avoids assuming the workstation can reach the private vault. The Azure
vault remains the durable source; the injected Secret is its runtime copy.
An explicitly provided matching demo key in `.env` can be used for HTTP access
without that Secret read. Never print either source's value. Distinguish
operator authorization failure, missing runtime Secret, and an unreachable
API instead of falling back to another credential or broadening network access.

Credential acceptance replaces the provisioner Pod with an empty writable
workspace after shared and isolated provisioning. It must reconnect with the
same stored passwords, preserve API keys and tenant data, and correctly reuse
the shared pair. Missing credentials for an initialized database must be a
clear error. Public data Pods must still use `data-api-runtime` with ConfigMap
get only and authenticated Secret GET/list denial.

Azure operator-client acceptance must pass from the normal permitted AKS
administrative network even when the workstation cannot directly reach the
private vault. Read the API key through its injected Kubernetes Secret as
specified; keep the vault private. An operator without the required cluster
read permission must get a clear authorization error, not a request to reveal
or export every vault credential.

Local acceptance proves bootstrap, two shared tenants, one isolated tenant,
configuration/counters, both real parent-link faults, API replacement while
the parent is blocked, and datastore persistence without a cloud service call.
Inspect actual kind/container mounts to show no path inside the repository,
`.env`, or `.state` is mounted. Inspect the API/provisioner to confirm neither
has the Docker socket. Stop/start only the owned management kind node after
bootstrap and prove the API server still decrypts its Secrets without the
operator's temporary files.

Behavioral acceptance preserves five distinct clusters, shared pair reuse,
isolated pair separation, control-owned updates, unchanged-poll idempotency,
ordered timelines, both at-least-60-second parent outages, and latest-only
catch-up. The existing 30-second restoration/recovery targets remain technical
measurements, not cost constraints. A data API request and restart during a
parent outage must not call an upstream discovery or secret service.

Fault acceptance covers both normal interruption and actual loss of the client.
In a controlled test against an explicitly owned disposable deployment, record
the exact helper PID, wait for a verified applied fault, then terminate only
that process without running its cleanup handler. Verify the fault remains
active. A new command with only `.env` must discover the resource journal,
restore the exact rule/policy, and verify actual connectivity and original-rule
fingerprints. Do not kill a reconciler or node. This forced-loss test is not
the normal manual-demo interruption procedure. Database metadata or a ConfigMap
flag alone does not prove the network was restored.

Cleanup acceptance works with all generated local files absent and the
management application unavailable. Query actual Radius owners and provider
resources; remove only the selected deployment in owner order. Independently
verify absent project resources and preserved unrelated resources. Do not claim
to purge a retention-protected vault or an externally supplied shared vault.

No past deployment result counts as new acceptance. Record the source revision,
actual inspected image content, command/run timestamps, scope, and observed
outcomes in the new measurements or optional report. Keep failed and interrupted
results visible rather than relabeling them.

## Idempotence and Recovery


Recreating `.env` with the same selected values is configuration setup, not a
resource reset. It must discover the same deployment. Changing target values
selects another deployment and must not adopt or delete the previous one.

Safe reads, endpoint lookup, credential retrieval, and generation of temporary
manifests can repeat. Creation must consult its actual owner and operation state.
Do not generate replacement credentials or replay non-idempotent initialization
because an operator file disappeared.

The current DB singleton remains the operation coordinator. A failed/uncertain
remote action is recorded as failed or interrupted and reconciled with resource
observations for operator diagnosis/cleanup. Automatically finishing that action
is not part of this simplification. No required local marker file remains.

Historical state directories and old deployments must not be migrated by
unscoped deletion. Implement and prove fresh environments, then document any
separately approved migration path if one is later needed.

## Interfaces and Dependencies


Keep new interfaces small and typed. `DemoConfig` in `scripts/operations/config.py`
represents user-selected environment, project, deployment, Azure fields when
applicable, and optional secret inputs with redacted representations.
`load_config(path)` has no resource mutations; `initialize_config(...)` performs
the validated atomic `.env` replacement.

`Discovery` resolves slots and returns transient typed resource identity,
endpoint, and access objects. Its adapters use existing Azure/Radius/Kubernetes
clients and the local Docker Desktop resolver, not a new service. A context
manager owns temporary CLI HOME/kubeconfig/manifests and removes them after use.
Do not persist resolved objects in SQL or serialize them as mandatory exports.

`CredentialStore` supports owned get/create/conflict checks for exact logical
plane/role names. Use the existing Azure Identity/Key Vault packages where
available and the existing Kubernetes client for local. Install dependencies
only through the project manifest/lockfile if the chosen implementation needs
them. Do not add Azure dependencies to the public API runtime or require them
on the local-only execution path.

Use Python 3.13, Radius 0.60.2 with bundled Bicep 0.42.1, Docker Desktop/kind,
existing Kubernetes libraries, and the pinned Terraform providers. Preserve
service-account separation, PostgreSQL role semantics, explicit TLS settings,
Redis password decoding, and parent/child reporting semantics.

## Artifacts and Notes


The `.env` file is the only new durable operator configuration. It is not
runtime history, a kubeconfig archive, a list of URLs, or an automatically
populated bag of every credential. Durable application and infrastructure state
remains in its owning services.

The initial security review found no vulnerabilities. The rubber-duck review
identified six missing execution contracts: private-vault access from operator
clients, deterministic deployment identity, command/runtime boundaries,
transaction-owned initialization, prepared local artifacts, and recovery of
an active fault after actual client loss. It also clarified the node-owned
encryption bootstrap sequence.

Those corrections are incorporated in the corresponding sections and acceptance
cases. Follow-up rubber-duck review reported the plan ready with no remaining
blockers; the correction security review reported no vulnerabilities. This is
plan-level readiness, not implementation or deployment proof.

Revision note, September 14, 2026: replaced the former filesystem-centered
implementation plan with the user's approved `.env` initialization, shared
Azure Key Vault, API-only discovery, cloud-free local operation, disposable
artifacts, and database/resource-owned progress. Explicitly separated discovery
metadata from tenant data and from runtime connection injection. Preserved
Radius child ownership and identified the necessary local Docker execution
connection rather than claiming that kind can create containers without it.

Revision note, review corrections: made operator key access work without direct
workstation connectivity to the private vault; defined naming and command-stage
contracts; replaced vague initialization markers with a transactional schema
version; specified local dependency preparation and actual orphaned-fault
recovery tests. Implementation status is recorded in `Progress`; uncompleted milestones must
not be described as verified.
