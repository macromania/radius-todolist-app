# Application and database contracts

## Processes and configuration

One API/reconciler image runs any of these commands:

| Command (`python -m plane_demo.…`) | Required configuration |
| --- | --- |
| `management.api` | `MANAGEMENT_DSN`, management-only `DEMO_KEY` |
| `control.api` | `CONTROL_DSN`, control-only `DEMO_KEY` |
| `control.reconciler` | `MANAGEMENT_DSN`, `CONTROL_DSN`, `PAIR_ID` |
| `data.reconciler` | `CONTROL_DSN`, `PAIR_ID`, `PROJECT_ID`, `KUBE_NAMESPACE` |
| `data.api` | `PAIR_ID`, `PROJECT_ID`, `KUBE_NAMESPACE`, data-only `DEMO_KEY`, Redis configuration |
| `setup.acme_responder` | mounted `CHALLENGE_DIRECTORY` (default `/challenges`) |
| `setup.bootstrap` | initialization inputs described below; never a steady-state runtime entrypoint |

These module paths do not change the logical `Settings.from_env()` role IDs
(`management_api`, `control_reconciler`, and so on), Kubernetes workload names,
or environment-variable contracts.

`DEMO_KEY` must have at least 32 characters; deployment generates independent,
high-entropy keys. Keys belong in Kubernetes Secrets. All tenant, configuration,
counter, and operation routes require `X-Demo-Key`. The only unauthenticated API
routes are `GET /healthz` and `GET /livez`; both are process liveness, **not**
database, Redis, child, or end-to-end readiness checks. API documentation is
disabled. The challenge responder does not host application routes.

Optional settings: `LISTEN_PORT=8088`, `POLL_INTERVAL_SECONDS=5`,
`TIMEOUT_SECONDS=5`, and `HTTP_BODY_LIMIT=8192`. API listeners are internal pod
listeners; do not publish them directly on the host. The gateway is responsible
for trusted HTTPS on Azure and reserved loopback-only HTTP ports locally.
Application code does not select behavior by environment name.

Use libpq DSNs, either URI or keyword form, with properly escaped passwords.
Azure DSNs must include `sslmode=verify-full` and the correct trusted root
certificate configuration; local synthetic-data connections explicitly use
`sslmode=disable`. Every operation opens and closes a connection. Connect,
statement, and lock timeouts are bounded; there are no indefinitely reusable
pooled parent connections that bypass a later network fault. Parent reads finish
before any write to another database or Kubernetes. Exceptions are logged by
stable category/SQLSTATE, not full exception text or DSN.

The data API has no parent database connection or parent module import. It reads
its local ConfigMap on every request, including after a process restart, and uses
its local Redis. Its API authentication and liveness do not contact a parent.

Redis accepts `REDIS_URL` (preferred) or `CONNECTION_REDIS_URL`; URLs must use
`redis://` or `rediss://` and are passed intact to Redis. Alternatively set all of
`CONNECTION_REDIS_HOST`, `CONNECTION_REDIS_PORT`, `CONNECTION_REDIS_TLS`
(`true|false`), and `CONNECTION_REDIS_PASSWORD`. The standalone Radius password
is percent-decoded **exactly once**. TLS is explicit, never inferred from port
6380; Azure Managed Redis uses TLS on port 10000. A missing connection or failed
operation is an error, never an in-memory fallback.

## Initializing a fresh database

The initialization command is intentionally not a migration/recovery system.
Run it once per **fresh PostgreSQL 16+ project database** in a short-lived
in-cluster Job before starting runtime workloads:

```text
python -m plane_demo.setup.bootstrap
```

Mount only that Job's setup credentials as `BOOTSTRAP_DSN`. The setup login needs
database ownership/creation rights in the selected database and PostgreSQL
`CREATEROLE`/role-administration privileges sufficient to create and temporarily
assume the three NOLOGIN roles. Azure's trusted database administrator supplies
this authority; ordinary API/reconciler users do not. The command reads
`SQL_DIRECTORY=/app/sql`, applies the matching schema in one transaction,
seeds the allocation/bindings, and restores temporary owner membership options
it changed. PostgreSQL 16 grants a CREATEROLE creator ADMIN without INHERIT or
SET by default; initialization temporarily enables these two options only for
the trusted setup login and restores their original values before committing.
It refuses existing schemas and incompatible or pre-privileged roles.
It never runs from a polling loop or HTTP request.

For management set:

```text
BOOTSTRAP_KIND=management
PAIR_SLOTS_JSON=[{"pair_id":"shared","reporting_role":"cp_shared"},{"pair_id":"isolated-1","reporting_role":"cp_isolated_1"}]
ROLE_PASSWORDS_JSON={"mgmt_api":"<generated>","mgmt_provisioner":"<generated>","cp_shared":"<generated>","cp_isolated_1":"<generated>"}
```

For each control instance set:

```text
BOOTSTRAP_KIND=control
PAIR_ID=shared
ROLE_PASSWORDS_JSON={"cp_api":"<generated>","cp_reconciler":"<generated>","dp_reconciler":"<generated>"}
```

Use that instance's actual pair ID. The role-password object must match the
required runtime roles exactly, and each password must contain at least 32
characters. These examples are placeholders, not usable credentials. Pass JSON
through Secret-backed environment or protected Job input, not shell arguments.
SQL is included in the API image. Remove completed initialization Jobs and setup
Secrets; do not copy `BOOTSTRAP_DSN` into any runtime Secret.

The role names are PostgreSQL cluster-wide. Each dedicated control database is
expected to have a separate PostgreSQL instance; do not initialize independent
control databases with different role passwords on one shared server.

| Runtime process | PostgreSQL login |
| --- | --- |
| Management API | `mgmt_api` in management |
| Non-public provisioner | `mgmt_provisioner` in management |
| Shared control reconciler's parent connection | `cp_shared` in management |
| Isolated control reconciler's parent connection | allocated reporting role, e.g. `cp_isolated_1` |
| Control API | `cp_api` in that control instance |
| Control reconciler's local connection | `cp_reconciler` in that control instance |
| Data reconciler's parent connection | `dp_reconciler` in that control instance |

`plane_owner` owns schemas/tables. `plane_writer` owns desired-write functions.
`plane_reporter` owns report functions with only SELECT, event INSERT/sequence
usage, and the minimal tenant-column UPDATE permission PostgreSQL requires for
row locking. All three are `NOLOGIN`; no runtime has membership in them.
Runtime roles are `NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS`.
Project schemas revoke `PUBLIC` access/default object privileges and public
function execution. Public schema creation is revoked.

The data API Pod uses Kubernetes account `data-api-runtime`, with only
namespace-scoped ConfigMap `get`. Radius's generated `data-api` account is not
the API's runtime identity: Radius 0.60.2 otherwise grants it namespace Secret
reads. Redis credentials are still injected by kubelet through the unchanged
`redis` connection. No parent DSN is present in the API environment, and its
mounted identity must receive authenticated 403 responses when getting
`data-reconciler-runtime` or listing Secrets. These are distinct checks.

The immutable `management.login_pairs` table binds database `session_user` to a
pair. FORCE RLS protects management tenants, events, and pairs. The control
schema similarly binds its three runtime logins to one pair. A child has
read-only desired access and EXECUTE on its one report function, not parent
desired UPDATE or event INSERT. Definer functions use fixed
`search_path=pg_catalog`, fully qualified project objects, and original
`session_user`, never a caller-set variable or definer `current_user`, for report
authorization.
Tenant-identity triggers reject actual mutation of onboarding identity even for
the provisioner's tenant-column privilege needed for `SELECT … FOR UPDATE`.

Bootstrap seeds `shared` and `isolated-1` for the default deployment. More
preallocated slots can be supplied initially. An operator may later extend
allocation by creating a nonprivileged reporting login, inserting its pair and
immutable binding as the trusted schema owner, granting schema USAGE and
SELECT on `management.tenants`, `management.pairs`, `management.login_pairs`,
and granting EXECUTE on `management.report_control` as `plane_reporter`.
Extend provider identities, network, and port allocation first. No runtime can
change a binding. There is no `MAX_TENANTS` or tenant product limit.

## Management and provisioner handoff

`POST /tenants` accepts exactly:

```json
{"tenant_id":"shared-a","isolation":"shared","initial_message":"alpha"}
```

The slug has 1–32 lowercase alphanumeric/hyphen characters and starts/ends with
an alphanumeric character. Messages contain at most 1,024 Unicode characters.
Unexpected fields, null characters, and invalid Unicode are rejected. Validation
errors do not reflect raw input. Body size is bounded independently of
Content-Length, including chunked requests.

Acceptance calls `management.accept_tenant(text,text,text) -> uuid`. It holds a
transaction-scoped advisory lock, selects/locks an allocated pair, and commits
tenant, onboarding UUID, operation, and `tenant_requested` event together.
Shared tenants use `shared`. Isolated tenants atomically receive the first
unassigned allocated pair; a partial unique index prevents double allocation.
An additional unique partial index permits only one pending/running operation.

The response is `202`, with `operation_id`, `status_url`, `operation_url`, and a
Location header. A duplicate is `409` with the original status URL, even while
busy. A different request while busy is `503` with `Retry-After: 5`, without
creating a row. Exhausted infrastructure allocation is `503
allocation_unavailable`, also without a row.

`management.operations` contains `operation_id`, `tenant_id`, `status`, `stage`,
`error_code`, `created_at`, and `updated_at`. Status values are `pending`,
`running`, `succeeded`, `failed`, and `interrupted`. Stage and error code must be
bounded stable identifiers, not subprocess output. The separate provisioner
must use its own singleton session advisory lock (reserved key `(35510,2)`),
mark old running operations interrupted at startup, and claim pending work
atomically. It must not replay interrupted work.

The provisioner has scoped SELECT, operation/pair UPDATE, tenant-row lock, and
event-append privileges. For **each** observation it must lock the tenant with
`SELECT … FOR UPDATE` before updating its operation/pair and inserting into
`management.events`, all in one short transaction. The event INSERT fields are
`tenant_id`, `onboarding_id`, `pair_id`, `source='provisioner'`, stable `type`,
`version=1`, optional `error_code`, and `stage`. `event_id` and `received_at` are
database-generated; never supply either. The parent coordinator implementation
owns this handoff, infrastructure orchestration, and crash policy.

The coordinator uses the database-only interface in `plane_demo.shared.db`.
Its loop and administrative sequence live separately in
`plane_demo.management.provisioner` and `plane_demo.management.provisioning`:

```python
with provisioner_session(management_dsn) as operations:
    operations.interrupt_running()  # once, immediately after acquiring the lock
    operation = operations.claim_pending()
    if operation is not None:
        # Provision from the immutable tenant/pair/message fields.
        operations.observe(operation.operation_id, "control-cluster")
        # After the actual child bootstrap and certificate steps:
        operations.complete(
            operation.operation_id,
            control_cluster_id=actual_control_id,
            data_cluster_id=actual_data_id,
            control_url=actual_control_url,
            data_url=actual_data_url,
        )
```

`claim_pending()` returns frozen `PendingOperation` fields: `operation_id`,
`tenant_id`, `onboarding_id`, `pair_id`, `isolation`, and `initial_message`, or
`None`. `observe(id, stage, status="running", error_code=None)` accepts running,
failed, and interrupted observations; failures require a stable error code.
Repeated identical observations append nothing. Terminal operations cannot be
resumed. `complete()` atomically stores non-secret pair identifiers/endpoints,
marks the pair available, marks the operation succeeded, and appends its event;
it never reports control-record creation. `interrupt_running()` returns the
number interrupted and never claims/replays them. Each method commits before
returning, with no transaction held across provider calls.

`provisioner_session()` raises `ProvisionerAlreadyRunning` if its session
advisory lock is held elsewhere. Keep this context open for the coordinator
lifetime. Do not create `OperationStore` directly, reconnect the session
implicitly, or continue infrastructure work after its connection fails: the
session is the singleton lock, not a lease or a recovery engine.

`management.pairs` inventory fields are `pair_id`, `isolation`, `reporting_role`,
`stage`, `control_cluster_id`, `data_cluster_id`, `control_url`, `data_url`, and
`created_at`. They must contain no credentials. Once child databases/apps are
initialized and accessible, set **`stage='available'`**. Only then does the
control reconciler pull that pair's assigned tenants. Infrastructure completion
must never fabricate a `control_record_created` event.

`GET /operations/{operation_id}` exposes the persisted operation.
`GET /tenants/{tenant_id}` exposes provisioning status separately from
`onboarding_status`. Management becomes ready only from a valid
`control_record_created` report for that immutable onboarding UUID and revision
1. It has no data-applied or Redis-health assertion.

## Control ownership, reconciliation, and reports

Control polling calls `control.ensure_tenant(tenant_id,onboarding_id,pair_id,
initial_message) -> bigint`. It creates version 1 and a creation event once.
Repeated reads preserve the current local message/version and reject a changed
onboarding UUID. A successful committed local insert/existing record is reported
directly to management PostgreSQL:

```text
management.report_control(tenant_id, onboarding_id, 1, transition, error_code=NULL)
```

Transitions: `control_record_created`, `control_record_failed`. A failed
transition requires a stable bounded error code.

`PUT /tenants/{tenant_id}/configuration` with `{"message":"new"}` calls
`control.update_configuration(tenant_id,message) -> bigint`: tenant locking,
message change, atomic version increment, and `configuration_updated` event
commit together. Unknown tenants are `404`, not implicit onboarding.

Data polling reads the latest control desired rows and writes its **own**
namespace's ConfigMaps, then calls:

```text
control.report_data(tenant_id, onboarding_id, version, transition, error_code=NULL)
```

Transitions: `config_applied`, `config_apply_failed`. The ConfigMap write result
must contain the intended message, UUID, and version before success is reported.
Report failures leave ConfigMaps intact. Each real `main()` invokes its tested
`run_once(settings) -> ReconcileResult` in the five-second loop.

Report functions return an event ID. Their stable key is source, onboarding UUID,
version, and transition. Identical replay returns the same ID; changed canonical
content is `PT409`. Wrong onboarding, pair target, future version, or malformed
content is `PT422`; unbound caller is `42501`. Failure then success is allowed.
Failure after success for that same version acknowledges the success ID without
appending a contradictory failure. Late historical successes do not reduce the
greatest applied version.

Reports and desired changes lock the **same tenant row before allocating event
IDs**. Each parent's one `events` table contains its desired and immediate-child
observations. APIs support `after_event_id >= 0` and `limit=1..500` (default 100).
Use `next_after_event_id` until null; event IDs may have gaps. Summary state is
computed independently of the selected page in one repeatable-read snapshot.
Control's `reported_at` is the received time of its greatest successful applied
version; `last_report` separately identifies a later failure/historical report.
None of these observations claims live application health.

## Local data API and Kubernetes permissions

ConfigMaps are named `tenant-<tenant_id>` and contain string fields `message`,
`version`, and `onboarding_id`. Required labels are `plane-demo/project`,
`plane-demo/pair`, and `plane-demo/onboarding`. The reader verifies their
ownership and parses all fields. Updates use Kubernetes resourceVersion
preconditions and reject same-version content conflicts; an older poll cannot
replace a newer version.

The data API service account needs **get ConfigMaps only** in its own namespace.
The data reconciler needs **get, create, patch ConfigMaps** in that namespace.
Neither gets a parent-cluster kubeconfig. Production uses the in-cluster
Kubernetes client with explicit connect/read timeouts. Do not give the API the
reconciler's write Role.

These are the application's required ConfigMap grants, not its exhaustive
effective permissions. Radius 0.60 also generates namespace Secret `get/list`
Roles and bindings. On token-bearing data workloads, a compromised container
could therefore read other runtime Secrets in that namespace. Separate service
accounts here are not a claim of isolation against container compromise.

`GET /tenants/{tenant_id}` returns `tenant_id`, `onboarding_id`, `message`,
`applied_version`, and `counter`. `POST /tenants/{tenant_id}/counter` first reads
the ConfigMap, then uses atomic Redis `INCR` on
`plane-demo:<onboarding_id>:<tenant_id>:counter`. A missing counter is zero; a
missing ConfigMap is `404 tenant_config_not_applied`. Invalid ConfigMaps,
unavailable Kubernetes, and unavailable/malformed Redis counters are explicit
503 errors. GET never increments.

ACME serves only `GET /.well-known/acme-challenge/<token>` from the mounted
directory. Tokens use URL-safe characters, response size is bounded, path
traversal is rejected, and everything else is 404. Tokens are intentionally
public and have no demo-key requirement. ConfigMap projected-volume symlinks
within the mount work; symlinks escaping it do not.

## Validation

`uv sync --locked` installs the pinned project dependencies. `make check` checks
runtime, operator, harness, and infrastructure source without live dependencies.
It generates the Radius type extensions before the compile-dependent tests and
uses normal pytest temporary directories under a project-local `TMPDIR`.
`make test-integration` runs only the explicitly configured dependency tests.

Real database tests require `TEST_POSTGRES_DSN` pointing to a **disposable**
PostgreSQL administrator connection and `TEST_ALLOW_DATABASE_CREATE=yes`.
They create uniquely named databases, initialize the real SQL, use separate
runtime logins, exercise actual API routes/reconciler functions, and remove the
test databases. Fixed runtime role names mean the server must not be a deployed
or shared project database. Set `TEST_REDIS_URL` for real atomic-counter and
URL-special-character password tests. Use a Redis password containing `/`, `+`,
`:`, or `%`.

The real Kubernetes test additionally requires `TEST_KUBECONFIG`,
`TEST_KUBE_CONTEXT`, and a pre-existing dedicated `TEST_KUBE_NAMESPACE`. It
creates/deletes only one randomly named test ConfigMap. Without these inputs,
that test is explicitly skipped; mocked-client tests do not prove Kubernetes
RBAC or cross-cluster networking. Tests never create Azure resources or clusters.
Real deployed isolation, outage, TLS, image, and cleanup acceptance remains the
deployment suite's responsibility.
