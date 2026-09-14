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
Azure image inspection and provisioning-configuration assembly are documented
as manual preparation handoffs.

## Repository map

| Location | Purpose |
|---|---|
| `src/plane_demo/` and `sql/` | APIs, provisioner, reconcilers, and database contracts |
| `infra/radius/apps/` | Management, control, and data application declarations |
| `infra/radius/types/`, `recipes/`, `environments/` | Resource APIs, provider implementations, and Recipe selection |
| `operations/` | Infrastructure and deployment administration |
| `harness/` and `tests/` | Individual demo utilities and automated checks |

For the design, read [architecture](docs/architecture.md) and
[application contracts](docs/contracts.md). [DECISIONS.md](DECISIONS.md) records
choices; [FINDINGS.md](FINDINGS.md) records results and their source revisions.
[Limitations](docs/limitations.md) describe the POC's scope.

Use synthetic data and keep credentials separate from source.
