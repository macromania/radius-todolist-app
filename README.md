# Radius three-plane tenant demo

Management, control, and data run in separate Kubernetes clusters. Radius
provisions the child clusters and their applications. **Azure onboarding and
functional behavior are proven in separately scoped runs:** two shared tenants,
one isolated tenant, configuration/counter isolation, and both parent outages.
Redis cleanup tracking and final teardown remain open; local is not implemented.

This pass organizes the repository, not the application design. SQL,
authentication, provisioning, and reconciliation simplification are deferred
until the agreed scenario works end to end. The obsolete todo example is
preserved in Git history, not as a second deployment path.

## How the demo works

Management accepts a tenant request. Its separate, non-public provisioner asks
management Radius to create missing control/data clusters, installs child
Radius, and deploys their applications. Shared tenants reuse one pair; an
isolated tenant receives a dedicated pair.

The steady-state configuration path is child-initiated:

```text
Operator -> Management API -> management PostgreSQL
                                  ^
                                  | control reconciler pulls and reports
                                  |
Operator -> Control API    -> control PostgreSQL
                                  ^
                                  | data reconciler pulls and reports
                                  |
Operator -> Data API       -> local tenant ConfigMap + Redis counter
```

Management's `ready` means control created its tenant record. Control's `applied`
means data wrote its ConfigMap. Neither is a transitive application-health
assertion. Control owns subsequent configuration changes. Data must keep serving
its last applied configuration while its parent is unreachable, then catch up
to the latest version. Azure demonstrated both parent-link outages, including
a data API restart while control PostgreSQL was unreachable.

## Repository map

| Group | Purpose |
|---|---|
| [`src/plane_demo/management/`](src/plane_demo/management/) | Management API, singleton provisioner, provisioning sequence, provider helpers |
| [`src/plane_demo/control/`](src/plane_demo/control/) | Control API and management-to-control polling process |
| [`src/plane_demo/data/`](src/plane_demo/data/) | Data API and control-to-ConfigMap polling process |
| [`src/plane_demo/shared/`](src/plane_demo/shared/) | Authentication, database, HTTP, Kubernetes, models, and settings helpers |
| [`src/plane_demo/setup/`](src/plane_demo/setup/) | Database initialization and ACME challenge responder |
| [`sql/`](sql/) | Management/control desired records, reports, timelines, and existing access rules—not fixtures |
| [`infra/bootstrap/`](infra/bootstrap/) | Operator-owned management AKS, networking, identities, and Azure foundation |
| [`infra/radius/apps/`](infra/radius/apps/) | Exactly three environment-independent application declarations |
| [`infra/radius/modules/`](infra/radius/modules/) | Reusable workload, challenge, gateway, database, and child-cluster templates |
| [`infra/radius/types/`](infra/radius/types/) | Custom Radius resource API contracts; YAML source, ignored generated `.tgz` extensions |
| [`infra/radius/recipes/`](infra/radius/recipes/) | Infrastructure implementations of those contracts |
| [`infra/radius/environments/`](infra/radius/environments/) | Recipe selection and environment configuration; Azure only so far |
| [`images/`](images/) | API and privileged provisioner image packaging |
| [`operations/`](operations/) | Platform bootstrap, deployment, image/Recipe publication, certificates, and cleanup |
| [`harness/`](harness/) | Demo-driving API client, state export, acceptance runner, and fault injection |
| [`tests/`](tests/) | Unit, operator, harness, and opt-in dependency integration tests |
| [`docs/`](docs/) | Runtime, provisioning, infrastructure, and cleanup contracts |

**Demo code** is the runtime, SQL, and infrastructure implementing the planes.
**Operations** administer the platform. **Harness code** sends example requests
and checks the result; it is not another plane or a dependency of data requests.

The API image explicitly copies only APIs, reconcilers, shared helpers, setup
support, and SQL. The provisioner image adds administrative code and pinned
tools. Do not replace the API allowlist with a copy of the entire source tree.

Runtime commands use `python -m plane_demo.management.api`,
`plane_demo.management.provisioner`, `plane_demo.control.api`,
`plane_demo.control.reconciler`, `plane_demo.data.api`, and
`plane_demo.data.reconciler`. Logical role IDs, service accounts, and labels have
not changed. See [configuration contracts](docs/contracts.md).

## Commands that exist today

Install the pinned project dependencies and use the cloud-free checks:

```sh
uv sync --locked
make help
make check
```

`make check` runs Ruff, offline tests, all Bicep compiles, generated Radius
extensions, and ShellCheck. It does not build/push images, create resources, or
use deployed databases. Real PostgreSQL/Redis tests require explicitly disposable
dependencies; see [test inputs](docs/contracts.md#validation).

For Azure, use the existing protected `.state/azure/` configuration and follow
the [provisioning prerequisites](docs/provisioning.md). These commands are
separate stages, not a one-command fresh deployment:

```sh
make preflight ENV=azure
make bootstrap-preview ENV=azure
make validate-azure ENV=azure
make bootstrap ENV=azure CONFIRM_AZURE=yes
make install-radius ENV=azure CONFIRM_AZURE=yes
```

Reviewed Recipes, verified image contents, and the validated operator
configuration are required before `make deploy-management CONFIRM_AZURE=yes`.
That target **submits** the operator Job; verify its completion separately.
There is no implemented local deployment target or automatic certificate-renewal
target. The [Azure contract](docs/azure-infrastructure.md) describes the
integration gates; [FINDINGS.md](FINDINGS.md) records what actually ran.
The fresh admission run stopped on harness endpoint discovery after all three
operations succeeded. A separate `verify-existing` run passed the remaining
functional and outage checks; it does not claim another fresh admission.

The harness remains explicit:

```sh
make export-state
./harness/api.sh azure management POST /tenants \
  '{"tenant_id":"shared-a","isolation":"shared","initial_message":"alpha"}'
./harness/api.sh azure control:shared PUT /tenants/shared-a/configuration \
  '{"message":"alpha-updated"}'
./harness/api.sh azure data:shared POST /tenants/shared-a/counter
```

State export must be repeated as child endpoints become available. See the
[harness contract](tests/harness/README.md) before running acceptance or faults.
If the operator cannot reach the AKS API directly, the same harness can run in
a management-cluster Job using committed source and the inspected tool image:

```sh
CONFIRM_AZURE=yes uv run python harness/run-azure.py --images-inspected --mode all --execute
```

This submits a Job, not a success result. Its separate `harness-state` volume
keeps evidence and exported API access; it does not contain the provisioner's
database credentials or add another application plane.

Each isolated tenant creates another two clusters, two gateways, PostgreSQL,
and Redis. Synthetic data and trusted operators only: demo keys are not
production tenant authentication or spending controls.

## State and cleanup

`.state/azure/` is essential deployment state: ownership records, protected
credentials/kubeconfigs, operator configuration, and evidence. Do not move or
bulk-delete it. `operator-state` and `provisioner-state` are distinct cluster
volumes. Preserve their names and credential permission checks.

Generated `.tgz` files and `.state/check/` compiler/test scratch are disposable
after their commands finish. Pytest uses its normal per-run temporary
directories; `make` places those under project-local `TMPDIR`. `.azure/plan.md`
is short deployment-workflow metadata, not another architecture document.
Generated credentials, caches, and evidence are not source or image inputs.

The deployed images were inspected for the grouped entrypoints and helper paths.
Before a later runtime change, rebuild/inspect images and regenerate any explicit
`certificateCommand` override to use `/app/operations/run-certificate-job.py`.
Source changes alone do not update immutable deployment inputs or running images.

```sh
make clean-plan
make clean-azure CONFIRM_AZURE=yes
make verify-clean
```

Read [cleanup ownership and verification](docs/cleanup.md) first. Keep the new
`rg-radplanes-*` deployment separate from legacy `rg-todolist-*` resources.
Do not claim teardown until the actual resources are verified gone.

Further reading: [decisions](DECISIONS.md), [findings](FINDINGS.md),
[application contracts](docs/contracts.md), and
[provisioning sequence](docs/provisioning.md).
