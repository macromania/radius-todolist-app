# Radius three-plane tenant demo

Management, control, and data run in separate Kubernetes clusters. Radius
provisions the child clusters and their applications. **Azure onboarding and
functional behavior are proven in separately scoped runs:** two shared tenants,
one isolated tenant, configuration/counter isolation, and both parent outages.
The corrected Redis create/tag/delete lifecycle and final Azure teardown are
also verified. Local Radius-owned cluster creation, access, and deletion are
proven. The complete five-cluster local scenario passed with shared reuse,
isolation, both parent outages, and the corrected data-API credential boundary.
Datastore Pod-replacement persistence was verified separately. Both demonstrated
environments have been removed; protected state is historical, not a live demo.

The runtime, platform operations, and acceptance harness are grouped separately.
Deeper SQL, authentication, provisioning, and reconciliation simplification is
deferred until the agreed scenario works end to end in both environments.
The obsolete todo example remains in Git history, not as a second deployment path.

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
| [`infra/radius/environments/`](infra/radius/environments/) | Environment contracts; local operations register equivalent Recipe maps through Radius |
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
extensions, Terraform mock-provider tests, and ShellCheck. Terraform 1.14-1.15
is required for the local Recipe checks; CI uses pinned 1.15.8. It does not
build/push images, create resources, or use deployed databases. Real
PostgreSQL/Redis tests require explicitly disposable
dependencies; see [test inputs](docs/contracts.md#validation).

The demonstrated Azure environment has been removed. Its protected
`.state/azure/` records are historical evidence, not active endpoints or reusable
cluster identities. For a fresh Azure deployment, follow the
[provisioning prerequisites](docs/provisioning.md). These commands are
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
Local build/setup/deployment/acceptance/cleanup targets are implemented and
verified. There is no automatic
certificate-renewal target. The [Azure contract](docs/azure-infrastructure.md) describes the
integration gates; [FINDINGS.md](FINDINGS.md) records what actually ran.
The [local executor gate](docs/local.md) passed real cluster creation, encrypted
state, child TLS, PostgreSQL/Envoy connectivity, and owner-driven deletion.
The final local full run used source `aac195e`; its API identity checks also ran
after a data API restart during the control-database outage.
See [local provider stages](docs/local-provider.md),
[local Recipes](docs/local-recipes.md), and
[local ownership-ordered cleanup](docs/local-cleanup.md) for their exact contracts.
The fresh admission run stopped on harness endpoint discovery after all three
Azure operations succeeded. A separate Azure `verify-existing` run passed the remaining
functional and outage checks; it does not claim another fresh admission.
That historical Azure proof predates the final shared data-API identity fix,
which was compiled for both environments and proved live on local Radius.

For a fresh local deployment, use the ordered prerequisites and commands in
[local provider stages](docs/local-provider.md#fresh-local-run) from a new
clone/worktree on the verified host. Docker access is deliberately scoped to
the recorded user's socket; this is not a portable-host setup script. Existing
attempt files deliberately block automatic replay; do not reuse deleted cluster
identities or bulk-remove state to bypass that guard.

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

`.state/azure/` and `.state/local/` retain ownership records, protected
credentials/kubeconfigs, operator configuration, and evidence from the removed
deployments. Do not use their endpoints as live access or bulk-delete them.
Local Docker images/cache and the shared `kind` network are intentionally retained.
Azure's protected soft-deleted vault is scheduled for purge on September 17;
it is not claimed purged. `operator-state` and `provisioner-state` are distinct cluster
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
[architecture](docs/architecture.md), [accepted limitations](docs/limitations.md),
[application contracts](docs/contracts.md), and
[provisioning sequence](docs/provisioning.md).
