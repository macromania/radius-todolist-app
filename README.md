# Radius three-plane demo

This demo uses [Radius](https://radapp.io/) to provision tenants across management,
control, and data planes. It demonstrates configuration polling, shared and
isolated placement, and continued data-plane operation during parent outages.
Choose Azure or Docker Desktop; each has a standalone walkthrough.

| Plane | Responsibility |
|---|---|
| Management | Accept tenant requests and assign prepared Azure capacity; provision local capacity on demand |
| Control | Own tenant configuration in PostgreSQL and report control-record creation |
| Data | Apply local ConfigMaps and serve Redis-backed requests without querying parent databases |

Each plane runs in its own cluster. Azure starts with three: management, shared
control, and shared data. An operator can add a named isolated control/data pair
later. Tenant onboarding never creates Azure infrastructure. Local scenarios
create the shared and isolated pairs on demand, reaching five clusters.

Azure uses one resource group per plane instance and one shared platform group.
AKS creates a separate node group for each provisioned cluster. Each Radius
identity has resource-type-specific permissions instead of Contributor on its
plane group. Use a fresh deployment name; existing layouts are not migrated.

Control pulls tenant records from management PostgreSQL. Data pulls configuration
from control PostgreSQL and writes local ConfigMaps. Data API requests use only
those ConfigMaps, Redis, and the data API's key, so they can continue during a
parent outage.

## Run the scenarios

Each guide is self-contained. Work through one step at a time; each step explains
what changes and what to check before continuing.

1. [Run Azure scenarios](RUN_AZURE_SCENARIOS.md): bootstrap Azure, deploy management,
   onboard tenants, change configuration, test outages, and clean up.
2. [Run local scenarios](RUN_LOCAL_SCENARIOS.md): prepare Docker Desktop and kind,
   run the same scenarios without Azure, check datastore persistence, and clean up.

Use either the manual scenarios or the automated harness for a run. They use the
same tenant names, so do not run both concurrently. Each guide includes the
automated alternative.

On Azure, `make bootstrap CONFIRM_AZURE=yes` prepares the default environment,
including verified images, databases, gateways and workloads. Later,
`make bootstrap CONFIRM_AZURE=yes ARGS='--isolated blue'` adds only that isolated
pair. Both commands use the same selected `.env` and preserve existing
environment identities. The Azure guide describes readiness and scoped cleanup.

Run `make help` for commands, or narrow it with `GROUP=azure`, `GROUP=local`,
`GROUP=setup`, or `GROUP=checks`. `COLOR=never` or `NO_COLOR=1` disables styling;
redirected output is plain.

Commands show numbered phases, explicit errors, and a transient spinner on
interactive terminals. Progress goes to stderr; JSON/API stdout stays
machine-readable. Native diagnostics and full build/push logs remain visible.
Sizing menus mark a recommendation: Enter accepts it, a number overrides it,
and `q` or EOF cancels. Existing choices and resource sizes are preserved.

Use `make report` for current endpoints and tenant status.
`make fault-status ARGS='SLOT COMPONENT'` reads a fault journal without changing
the fault. The guides use Make directly, without shell function setup.

## Configuration and state

`make init ENV=azure` or `make init ENV=local` writes the private, git-ignored
`.env`. Later commands read that selection. Changing `ENV` on another command
does not retarget the deployment.

Initialization replaces settings rather than appending or merging them. Repeating
the same inputs writes the same keys once; switching environments removes stale
settings and credentials. Invalid or duplicate credential inputs leave the prior
file unchanged.

| Information | Owner |
|---|---|
| Operator choices | `.env`: environment, project/deployment name, Azure selection, and chosen resource sizes and tiers |
| Stable application credentials | Key Vault on Azure; Kubernetes Secrets locally |
| Endpoints and cluster access | Current Azure, Radius, Kubernetes, or Docker APIs |
| Tenant configuration and provisioning progress | PostgreSQL |
| Infrastructure and fault progress | Resource owners and Kubernetes journals |

Commands create temporary access files and generated inputs when needed.
Deployment access and cleanup do not depend on workstation `.state` files.
Source checks may create disposable `.state/check` files and Bicep extensions.
The services still need their databases, Secrets, and persistent volumes.

Azure image builds and verification run in ACR Tasks. The workstation only
prepares trusted inputs and checks small, digest-bound reports; it does not pull
the application images or require Docker Desktop for the Azure workflow.
The local workflow still uses Docker Desktop.

## Checks

Install the tools listed in your chosen guide. On macOS, complete any Command
Line Tools or Xcode first-use prompts before running Make. Then run from the
repository root:

```bash
uv sync --locked
make check
```

This runs Ruff, Bicep compilation, offline tests, Terraform mock-provider
validation, and ShellCheck. It does not create clusters, build images, or contact
deployed databases. `make test-integration` is separate and requires explicitly
configured disposable dependencies.

**Verification status:** a fresh live end-to-end run remains outstanding.
Source and mock checks do not prove a deployment. The guides describe expected
checkpoints, not a recorded successful run.

## Contribute and reuse

Read [CONTRIBUTING.md](CONTRIBUTING.md) for development and pull-request guidance,
and [SECURITY.md](SECURITY.md) for private vulnerability reporting and publication
precautions. [AGENTS.md](AGENTS.md) maps the source and its contracts.

Original source is [MIT-licensed](LICENSE). Downloaded tools and images retain
their own terms, including Terraform and Redis; read
[Third-party software](THIRD_PARTY_NOTICES.md) before redistributing built images.

## Demo boundaries

Use synthetic data and a trusted operator. Shared demo keys are simple per-plane
API access, not production tenant authentication. Azure setup uses serialized,
owned administrative Jobs; local uses one provisioner. Neither path
automatically replays interrupted infrastructure work. Tenant migration/deletion
APIs are outside the demo.

Management readiness means control created a tenant record. Control's `applied`
report means data applied a ConfigMap. Neither report asserts that every
downstream dependency is healthy.

Azure creates billable resources, including at least three AKS clusters,
gateways, two PostgreSQL servers, and Redis. Optional isolated capacity adds
more. Local uses Docker Desktop resources and a fixed loopback port block.
Read the chosen guide's prerequisites and cleanup steps before starting.
