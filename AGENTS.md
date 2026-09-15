# Working on the three-plane demo

Read [README.md](README.md) first. The public instructions are the standalone
[Azure guide](RUN_AZURE_SCENARIOS.md) and [local guide](RUN_LOCAL_SCENARIOS.md).
Keep these guides current rather than adding histories, plans, or reference docs.
Git retains the historical material.

## Scope and verification

This is a trusted-operator demonstration of Radius and three application planes.
Use the current source to resolve behavior; do not infer live proof from tests.
The current command path still needs a fresh live end-to-end verification run.
Do not redesign authentication, reconciliation, or SQL during documentation work.

## Runtime map

- `src/plane_demo/management/api.py` accepts validated tenant requests.
- `src/plane_demo/management/provisioner.py` owns the non-public singleton loop.
- `src/plane_demo/management/provisioning.py` defines the administrative sequence.
- `src/plane_demo/management/providers/` owns provider access and scoped commands.
- `src/plane_demo/control/` owns configuration and polls management PostgreSQL.
- `src/plane_demo/data/` applies ConfigMaps and serves Redis-backed requests.
- `src/plane_demo/shared/` contains settings, models, auth, and database helpers.
- `src/plane_demo/setup/` contains SQL initialization and the ACME responder.
- `sql/management.sql` and `sql/control.sql` define database roles and access rules.

Preserve logical settings roles, service accounts, labels, resource names and
database status values when changing module paths.

## Infrastructure map

- `infra/bootstrap/azure.bicep` creates management AKS and its Azure foundation.
- `infra/radius/apps/` declares only management, control, and data.
- `infra/radius/modules/` contains shared workload and dependency declarations.
- `infra/radius/types/*.yaml` defines custom types; adjacent `.tgz` files are generated.
- `infra/radius/recipes/azure/` and `local/` implement provider-specific resources.
- `infra/radius/environments/azure.bicep` maps types to Azure Recipes.
- `infra/radius/bicepconfig.json` applies to the Radius subtree.

Application declarations must not branch on environment names. Bootstrap alone
may create management directly. Management Radius creates child clusters; each
child Radius owns its apps. Do not replace this with direct Python cluster creation.

## Operator entrypoints

- `Makefile` and `scripts/operations/stage.sh` route commands from the selected `.env`.
- `scripts/operations/init.sh` writes starting configuration, not discovered inventory.
- `scripts/operations/{api,endpoints,kube}.sh` discover current access per invocation.
- `make report` prints live observations; `fault-status.sh` reads a selected fault journal.
- `scripts/operations/azure/` and `local/` contain native bootstrap/build operations.
- `scripts/operations/run-management-job.py` and `deploy-plane.py` own the Azure Job path.
- `scripts/operations/local/deploy-demo.py` uses the guarded local operator factory.
- `scripts/harness/export-state.py` and its local counterpart print optional reports.
- `scripts/harness/test-e2e.py` and `fault-parent-link.py` drive acceptance and owned faults.

Operations are administration, not a fourth application plane. Stable credentials
belong in Key Vault on Azure and Kubernetes locally. Worker startup uses public
identity and current APIs, without a file credential seed or working-state PVC.
Keep `images/api/Dockerfile`'s public source allowlist separate from provisioner
images. Never copy provider tools or deployment credentials into an API image.

## Checks and output

Follow [README checks](README.md#checks). Install dependencies with `uv sync --locked`
when needed. `make check` compiles Bicep and runs Ruff, offline tests, Terraform
mocks and ShellCheck; it does not create clusters, build images or use live databases.
Integration tests require explicit disposable dependencies.
Use project Python 3.13 and Radius 0.60.2 with Bicep 0.42.1, not `az bicep`.
Test real entrypoints, command arguments and observable results.
After rebuilding an image, inspect its code. A changed digest is not proof.
Keep build/push diagnostics visible and compare results with their source revision.
Keep Make output sectioned, with separators, spacing and terminal-aware color.
Preserve plain machine-readable stdout and tool diagnostics; do not hide failures.

## Contracts to preserve

- Management readiness means control-record creation, not data/Redis health.
- Control reports ConfigMap application, not downstream dependency health.
- Children pull from parent databases; APIs do not push configuration downstream.
- Repeated control polls preserve control-owned updates.
- Data requests use only local ConfigMaps, Redis, and their own API key.
- Keep singleton locking and explicit interrupted-operation behavior.
- Keep Azure PostgreSQL verified TLS and Redis `tls: true`; local non-TLS is explicit/internal.
- Keep the lowercase `redis` connection and existing password encoding.
- Data API uses `data-api-runtime` with ConfigMap get only, never namespace Secret access.
- Preserve `fsGroupChangePolicy: OnRootMismatch`, private credential modes and separate identities.

## Safety and cleanup

Read the [Azure cleanup](RUN_AZURE_SCENARIOS.md#4-clean-up-azure) or
[local cleanup](RUN_LOCAL_SCENARIOS.md#5-clean-up-local) step before deletion.
Verify exact owners. Never bulk-clean `.state/azure/`, credentials or live PVCs.
Delete disposable check files or generated extensions only after their commands end.
Use explicit Radius configuration, kubeconfig, subscription and context.
Do not change global CLI defaults, expose databases or grant runtime role delegation.
Operational safety checks must be unconditional guards, not Python `assert`.
Use Docker Desktop and `ports.env`; do not bind default host ports.
Live operations are opt-in. Local is not a fallback for a failed Azure deployment.
Do not touch `rg-todolist-*` or unrelated resources; report leftovers.
Use synthetic data. Shared demo keys are not hostile-tenant or cost-abuse protection.
