# Working on the three-plane demo

Read [README.md](README.md) first. This repository now contains the three-plane
tenant demonstration, not the old todo example. Git history preserves that
example; its scripts and deployment targets are intentionally removed.
Manual walkthroughs are in [RUN_AZURE_SCENARIOS.md](RUN_AZURE_SCENARIOS.md)
and [RUN_LOCAL_SCENARIOS.md](RUN_LOCAL_SCENARIOS.md).

## Scope and current status

- Azure and full local functional/outage scenarios are proven; see the recorded run scopes.
- Azure onboarding, functional/outage behavior, and fresh Redis lifecycle are proven.
- Azure and local teardown are verified; retained state is not a live deployment.
- Use [FINDINGS.md](FINDINGS.md) for actual results, not assumptions from code.
- [DECISIONS.md](DECISIONS.md) records approved choices and accepted limitations.
- [docs/architecture.md](docs/architecture.md) and [docs/limitations.md](docs/limitations.md) summarize the system and its boundaries.
- The user deferred deeper SQL/code simplification until end-to-end proof.
- A directory move is not permission to redesign authentication or reconciliation.

## Runtime map

- `src/plane_demo/management/api.py` accepts validated tenant requests.
- `management/provisioner.py` owns the non-public singleton run loop.
- `management/provisioning.py` defines the short administrative sequence.
- `management/providers/` supplies provider access and scoped commands.
- `src/plane_demo/control/` owns configuration and polls management.
- `src/plane_demo/data/` applies local ConfigMaps and serves Redis-backed requests.
- `src/plane_demo/shared/` holds small common helpers.
- `src/plane_demo/setup/` holds initialization and ACME responder support.
- `sql/management.sql` and `sql/control.sql` retain the existing access rules.
- [docs/contracts.md](docs/contracts.md) defines APIs, roles, reports, and settings.

Do not rename logical settings roles, service accounts, labels, resource names,
or database status values when changing Python module paths.

## Infrastructure map

- `infra/bootstrap/azure.bicep` creates management AKS and its Azure foundation.
- `infra/radius/apps/` contains only management, control, and data declarations.
- Application definitions must never branch on the environment name.
- `infra/radius/modules/` contains their workload and dependency helpers.
- `infra/radius/types/*.yaml` defines custom Radius API contracts.
- The adjacent `.tgz` files are ignored generated Bicep extensions.
- `infra/radius/recipes/azure/` implements clusters, PostgreSQL, Redis, and gateways.
- `infra/radius/recipes/local/` implements kind and persistent local dependencies.
- `infra/radius/environments/azure.bicep` maps types to Recipes.
- `infra/radius/bicepconfig.json` applies to the Radius subtree.
- [docs/azure-infrastructure.md](docs/azure-infrastructure.md) defines the contracts.

Bootstrap is the only direct management-cluster creation exception.
Management Radius creates child clusters; each child Radius owns its apps.
Do not replace those abstractions with direct cluster creation in Python.

## Operations, harness, and images

- `scripts/operations/` is platform administration, not a fourth application plane.
- `scripts/operations/project.py` scopes bootstrap commands and project configuration.
- `scripts/operations/deploy-plane.py` and `run-management-job.py` coordinate deployment.
- [docs/provisioning.md](docs/provisioning.md) explains prerequisites and ownership.
- [docs/local-provider.md](docs/local-provider.md) and [docs/local-cleanup.md](docs/local-cleanup.md) define local operations.
- `scripts/harness/api.sh` is the operator's thin API client.
- `scripts/harness/export-state.py` exports protected local access/acceptance state.
- `scripts/harness/test-e2e.py` and `fault-parent-link.py` drive acceptance and faults.
- [tests/harness/README.md](tests/harness/README.md) defines opt-in live testing.
- `images/api/Dockerfile` has an explicit public-runtime source allowlist.
- `images/provisioner/Dockerfile` adds privileged code and administrative tools.
- Never copy provider code, platform tools, or deployment credentials into API images.

## Validation

Keep Make output sectioned, tidy, and actionable, never a flat wall of text.
Use visible separators, generous spacing, bold text, and terminal-aware color.
Follow the [operator output contract](docs/contracts.md#operator-output).
Use `make help` and `make check`; neither claims a deployment succeeded.
Install project dependencies with `uv sync --locked` when required.
`make check` generates extensions, compiles all Bicep, runs Ruff, offline tests,
Terraform mock-provider validation, and ShellCheck. It never creates clusters,
builds/pushes images, or contacts deployed databases.
`make test-integration` requires explicit disposable dependencies; see contracts.
Use Python 3.13 from the project, not an arbitrary system interpreter.
Use Radius 0.60.2 and its bundled Bicep 0.42.1, not the older `az bicep`.
Test real entrypoints and command construction, not only isolated functions.
After rebuilding an image, inspect its actual code; a new digest is not proof.
Do not suppress image build/push output or reuse pre-change results as new proof.

## Contracts that must survive changes

- Management readiness is control-record creation, not data/Redis health.
- Control reports data ConfigMap application, not transitive health.
- Children pull from parent databases; APIs do not push configuration downstream.
- Repeated control polls preserve control-owned updates.
- Data API requests depend only on local ConfigMaps, Redis, and their own key.
- Keep singleton locking and explicit interrupted-operation behavior.
- Keep Azure PostgreSQL verified TLS and Redis `tls: true`; local non-TLS stays explicit and internal.
- Keep the lowercase `redis` connection and existing password encoding.
- Keep API/provisioner identities and images separate.
- Data API uses `data-api-runtime` with ConfigMap get only, never namespace Secret access.
- Preserve `fsGroupChangePolicy: OnRootMismatch` and private credential modes.

## State, safety, and cleanup

Do not move or bulk-clean `.state/azure/`, credentials, or live PVC state.
`.state/check/` and generated extensions are disposable only after commands end.
Historical finding paths describe the original files; do not rewrite evidence.
Use explicit project Radius configurations, kubeconfigs, subscription, and context.
Never change global CLI defaults, open public databases, or grant runtime role delegation.
Operational safety checks use unconditional guards, never Python `assert`.
Use Docker Desktop and the reservations in `ports.env`; no default host ports.
Live tests and cloud mutations are opt-in. Local is not a fallback deployment path.
Read [docs/cleanup.md](docs/cleanup.md) before deletion; verify exact ownership.
Do not touch legacy `rg-todolist-*` or unrelated resources. Report leftovers.
Synthetic data only. Shared demo keys are not hostile-tenant or cost-abuse protection.
