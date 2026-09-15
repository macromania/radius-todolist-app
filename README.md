# Radius three-plane demo

This POC demonstrates tenant provisioning and configuration across three
independent planes. The focus is Radius, the boundaries between planes, and
their behavior during configuration changes and outages.

| Plane | Responsibility |
|---|---|
| Management | Accept tenant requests and provision control/data clusters through Radius |
| Control | Own tenant configuration in PostgreSQL and report control-record creation |
| Data | Apply local ConfigMaps and serve Redis-backed requests without querying parent databases |

Shared tenants reuse a control/data pair. An isolated tenant receives a
separate pair. Children pull configuration from their parent and report their
own progress; readiness is not transitive dependency health.

## Run the scenarios

Each guide is a standalone, self-paced walkthrough with setup, short explanations,
manual API calls, checkpoints, outage/recovery experiments, and cleanup.

1. [RUN_AZURE_SCENARIOS.md](RUN_AZURE_SCENARIOS.md): Azure foundation, management
   deployment, shared/isolated tenants, configuration, parent outages, and teardown.
2. [RUN_LOCAL_SCENARIOS.md](RUN_LOCAL_SCENARIOS.md): Docker Desktop and kind setup,
   the same manual scenarios, datastore Pod-replacement persistence, and cleanup.

Use individual operations and harness utilities as shown in the guides rather
than running the all-in-one acceptance runner alongside your manual requests.
The normal commands inspect image contents and assemble deployment inputs from
current APIs. No saved endpoint inventory or workstation credential bundle is
required. The state-removal refactor still needs fresh live proof; see
[FINDINGS.md](FINDINGS.md) for the verified revision scopes.

Run `make` or `make help` for commands grouped by workflow, usage examples, and
safety requirements. Use `make help GROUP=azure`, `GROUP=local`, `GROUP=checks`,
or `GROUP=setup` to focus on one area. Help and stage headings use separators,
generous section spacing, single-spaced command rows, bold text, and blue accents.
Supporting guidance and document references appear in separate `[info]` blocks.
Styling is automatic on supported terminals. Use
`COLOR=always` to force styling, or `COLOR=never` or `NO_COLOR=1` to disable it.
Redirected output stays plain by default. Command runs preserve tool diagnostics
and machine-readable stdout.

## Repository map

| Location | Purpose |
|---|---|
| `src/plane_demo/` and `sql/` | APIs, provisioner, reconcilers, and database contracts |
| `infra/radius/apps/` | Management, control, and data application declarations |
| `infra/radius/types/`, `recipes/`, `environments/` | Resource APIs, provider implementations, and Recipe selection |
| `scripts/operations/` | Infrastructure and deployment administration |
| `scripts/harness/` and `tests/` | Individual demo utilities and automated checks |

For the design, read [architecture](docs/architecture.md) and
[application contracts](docs/contracts.md). [DECISIONS.md](DECISIONS.md) records
choices; [FINDINGS.md](FINDINGS.md) records results and their source revisions.
[Limitations](docs/limitations.md) describe the POC's scope.

The [provisioning walkthrough](HOW_PROVISIONING_WORKS.md) explains current Radius
and Azure deployment ownership. [Provisioning durability](PROVISIONING_DURABILITY.md)
records the proposed management-local and Azure-managed scheduler direction,
including the unverified local backend requirements.

The [recovery design direction](RECOVERY.md) captures the external Radius
PostgreSQL preference, availability-first recovery model, and unresolved
execution and recovery questions. It is design exploration, not implemented
recovery behavior.

The [state-removal ExecPlan](docs/plans/remove-state-dependency.md) describes the
`.env` configuration, live discovery, service-owned credentials and progress,
and the remaining verification work.

Use synthetic data and keep credentials separate from source.
