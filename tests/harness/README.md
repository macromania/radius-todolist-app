# Opt-in three-plane acceptance

Use [the Azure guide](../../RUN_AZURE_SCENARIOS.md) and
[the local guide](../../RUN_LOCAL_SCENARIOS.md) for individual manual steps.
This document describes the automated harness, not deployment preparation.
The revised deployment pipeline is still being integrated. Offline harness
tests and historical deployments are not proof of a new live run.

## Configuration and discovery

The harness reads the checkout's private `.env`, written by `make init`.
It treats values literally and does not execute that file. Configuration,
source reads, and Git commands resolve from the checkout, not the caller's
working directory. An optional `--config` can select only that same `.env`;
it cannot select an old acceptance inventory or another checkout.

Each command discovers endpoints, cluster access, and parent bindings through
Azure, Radius, and Kubernetes APIs. Temporary access files and HOME directories
are discarded afterward. Neither `acceptance.json`, `endpoints.json`, an
exporter process, nor database endpoint columns are a prerequisite.

`export-state.py --once` and its local counterpart are optional read-only
reports to stdout. They do not prepare files for later commands. Their `--watch`
mode repeats observation rather than maintaining a required inventory.

## Running the checks

First complete the normal build, bootstrap, setup, and management deployment
commands. Commit the source used for deployment, inspect actual image contents,
and use the same source revision for acceptance.

```sh
# Offline command-path and contract tests:
uv run --no-sync pytest -q tests/harness

# Azure, using the current .env selection:
CONFIRM_AZURE=yes make test-e2e
CONFIRM_AZURE=yes make test-outages

# Both scopes in one explicit run:
uv run python scripts/harness/test-e2e.py --environment azure --mode all --execute

# After explicitly configuring and deploying local:
CONFIRM_LOCAL=yes make local-test
```

`--execute` authorizes the selected live scenario. A supplied `--environment`
must match the loaded `.env` before live actions begin. The Make aliases pass
their expected environment; a generic direct command can omit that argument
and use `.env` alone.

| Mode | Scope |
|---|---|
| `scenario` | Fresh admissions, shared reuse, isolation, configuration, counters, authentication and timelines |
| `outages` | Parent-link faults, continued data requests, API replacement and recovery |
| `all` | Both scopes |
| `verify-existing` | Existing-state verification; not fresh onboarding proof |

Reports go to stdout. They include source revision, UTC timing, the actual mode,
cluster and workload identities, source hashes, timelines, versions, counters,
and bounded datastore observations. They exclude API keys, passwords, DSNs,
complete environment dumps, and raw credential-bearing command output.
Preserve a report explicitly if needed; it is evidence, not discovery authority.

## Fault journals and recovery

Fault intent, active state, and restoration information belong to owned
Kubernetes ConfigMaps. Journals have UID bindings, version checks, and
fingerprints. Actual fault artifacts are bound to the same journal UID.
Restoration rechecks current owners and exact policy/rule state before mutation.
Changed or ambiguous ownership is an error, not permission to delete by name.

The standalone fault command uses the same implementation as acceptance:

```sh
uv run python scripts/harness/fault-parent-link.py \
  --slot shared-data --component data-reconciler --duration 60 --execute

# A fresh command can discover and restore the active journal:
uv run python scripts/harness/fault-parent-link.py \
  --slot shared-data --component data-reconciler --restore --execute
```

Do not pass an old local report as restoration authority. Do not delete journal
objects, flush rules, or reset a cluster to turn a failed restoration into a
passing result. Standalone restoration success is not full outage acceptance.

First-admission continuation remains limited to its original bounded scope.
Use the resource journal handle reported by that run:

```sh
uv run python scripts/harness/test-e2e.py --mode all \
  --continue-first-from NAME@UID@RUN_ID --execute
```

The handle selects an owned journal; it is not a path to a workstation file.
Continuation still verifies its original source, request and deployment
identities and does not silently become a new admission run.

## Boundaries checked during acceptance

Shared tenants must reuse one pair, while an isolated tenant gets a distinct
control/data pair. The second shared admission includes a paused data
reconciler so management readiness cannot be mistaken for data readiness.
Both real parent outages must leave data requests independent of the parent,
and recovery includes the original absolute convergence deadline.

Azure parent transport retains verified PostgreSQL TLS and negotiated Redis
TLS. Local PostgreSQL is explicitly internal, non-TLS, and uses the verified
node address on port 31543. Local Redis remains explicitly non-TLS. A successful
connection option or environment value is not a TLS result; the probes inspect
the actual connection and perform authenticated operations.

Local faults use the existing kind node tools to enter the verified
reconciler's network namespace. Only the exact parent TCP drop rule is inserted
or removed. Node/global networking and unrelated processes are not touched.
Runtime API and provisioner containers receive no Docker socket.

Acceptance checks actual running source bytes, not only image tags or changed
digests. Azure workloads use the selected project registry and digest-pinned
references. Local image identity is resolved through the actual container
runtime and checked against the expected configuration/content identity.
Replacement data API Pods are checked again.

The data API's `data-api-runtime` identity is limited to ConfigMap get. Secret
get/list and token minting remain denied, including after API replacement.
The API and privileged provisioner image/identity boundaries remain separate.

Current results and unresolved integration work are recorded in
[FINDINGS.md](../../FINDINGS.md), not inferred from passing mocked tests.
