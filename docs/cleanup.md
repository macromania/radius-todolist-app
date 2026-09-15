# Azure cleanup and verification

These commands remove the selected demonstration and destroy its application
data. They read the checkout's `.env` and live Azure, Radius and Kubernetes
owners. They do not require saved manifests, exported targets, database endpoint
inventory, a healthy management API, or a prior cleanup report.

```sh
# Require DEMO_ENV=azure in the selected configuration:
make show-config

# Read-only ownership and deletion plan:
make clean-plan

# Execute the same normal owner-ordered operation:
make clean-azure CONFIRM_AZURE=yes

# Independent verification of the selected deployment:
make verify-clean
```

The underlying entrypoints are `scripts/operations/clean-azure.py` and
`scripts/operations/verify-clean.py`. Omit `--execute` for a plan. The command's
Azure selection must match `.env`; every Azure operation names that subscription.
Only initialization uses `ENV`; it is not an override for an existing `.env`.
Nothing changes global CLI defaults or broadens network access.

The revised implementation has offline review and test coverage. Historical
teardown results in [FINDINGS.md](../FINDINGS.md) are not proof that the revised
flow has run live.

## Ownership and prerequisites

`STEM` is `PROJECT-DEPLOYMENT-azure`. Discovery checks the selected foundation,
exact group/resource IDs, ownership tags, managed identities and role scopes.
Expected slot groups are `rg-STEM-SLOT-cluster`, `rg-STEM-SLOT-app`, and
`rg-STEM-SLOT-nodes`; the shared foundation is `rg-STEM-platform`.
Matching a prefix alone never authorizes deletion.

Cluster access is rediscovered into private temporary files. It must match
the actual owned AKS and verified transport. Application namespace and HTTP API
availability are not prerequisites for discovering a cluster to clean up.
Radius applications, environments and custom cluster resources must still have
their expected ownership and terminal status.

Restore parent-link faults and finish or stop active provisioning before
cleanup. The operator checks resource-owned fault state and refuses incomplete
or changed fault ownership. Cleanup does not repair faults, rewrite their
history, or interpret a missing report as restoration.

Discovery failures, missing ownership records, unexpected resources and
incomplete deletion stop the operation. A zero CLI exit code is not an absence
check. Legacy `rg-todolist-*`, unrelated groups, unrelated assignments and
unrelated vault objects are outside this operation.

## Normal deletion order

1. Quiesce the selected management API and provisioner and observe Pod termination.
2. Delete data applications, then control applications, through each child's Radius.
3. Verify their backend resources are absent before deleting each child cluster
   through its management-Radius owner.
4. Delete remaining management applications and verify management app-group
   resources are absent.
5. Remove only the verified bootstrap-owned Azure resources and eligible
   project role assignments/definitions.
6. Query Azure independently to verify the resulting scope and report leftovers.

A Radius error never triggers direct child-cluster deletion. The normal
entrypoint does not expose the old provider-only ownership-path bypass.
If an owner is unavailable or a failed Recipe left an orphan, stop and inspect
the exact resources. Any exceptional recovery needs its own explicit reviewed
ownership operation and must not be called normal Radius deletion proof.

## Keep the foundation

To remove Radius-owned applications and children while retaining the foundation:

```sh
uv run python scripts/operations/clean-azure.py --radius-only
CONFIRM_AZURE=yes uv run python scripts/operations/clean-azure.py --radius-only --execute
```

This still checks actual child/backend absence and an empty management app
group. It does not report whole-environment cleanup. The management cluster,
registry, network, identities and other retained foundation resources remain.
Orphaned PostgreSQL/gateway resources block success even if Radius records
have disappeared.

## Retained resources and verification

An externally selected Key Vault is not owned by this demo. Cleanup does not
delete or reconfigure it, unrelated objects, or external role assignments.
Custom role definitions still required by retained external assignments remain
and are reported rather than forced away.

An owned vault's soft-deleted recovery record is reported separately. Purge
protection is not bypassed, and nothing claims that name reservation or recovery
records have been purged. Deployment history can also remain without being a
live application deployment.

Verification uses current Azure APIs, not a saved cleanup result. Reports
distinguish removed scope, retained foundation/external resources, recovery
records and blockers. Preserve stdout explicitly when evidence is needed.
Reports are evidence, not authorization for a later deletion.

The commands do not move or remove historical `.state`, `.env`, source files,
or local credentials. Keep those archives separate from live-deployment claims.

## Offline checks

```sh
uv run --no-sync pytest -q tests/operations/test_cleanup.py \
  tests/operations/test_live_cleanup.py
```

These tests use command/API doubles and do not delete Azure resources.
