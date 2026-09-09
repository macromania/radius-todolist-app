# Azure cleanup and verification

These tools implement the approved **whole-demo Azure teardown**. They destroy
application data. Local cleanup is not implemented. Authoring and mocked tests
do not mean a live cleanup has run.

```sh
# Read-only cloud/Radius inventory and ordered plan:
uv run python operations/clean-azure.py

# Destructive execution requires both switches of intent:
CONFIRM_AZURE=yes uv run python operations/clean-azure.py --execute

# Independent, read-only Azure verification:
uv run python operations/verify-clean.py
```

`--environment` accepts only `azure`. No tool changes the selected Azure
subscription, global kubeconfig, global Radius configuration, authentication
defaults, firewall rules, or Azure Policy. Every Azure command names subscription
`a3ed6c04-563f-4855-ac84-bdf1e5fbc3fc`.

## Inputs

The default ownership manifest is `.state/azure/bootstrap.outputs.json`.
`--manifest NAME` selects another file within `.state/azure`; absolute paths
inside that directory are also accepted. Both the unwrapped bootstrap output
and ARM `{type, value}` output envelopes are supported.

The tool uses `foundation`, the allocation **array**, and `managementCluster`.
It does not read application passwords, call management HTTP APIs, or connect to
PostgreSQL. The mutable credential store described in
[provisioning.md](provisioning.md) is not an infrastructure ownership authority.
Do not substitute `credentials.json` or the provisioning configuration's
dictionary-form allocation map for the bootstrap output.

Before execution, run the harness exporter while the clusters remain reachable:

```sh
uv run python harness/export-state.py --once
# Or keep exporting as onboarding creates children:
uv run python harness/export-state.py --watch --timeout 7200
```

No provisioner PVC files, credentials store, or manually authored JSON are
needed. The exporter reads `.state/azure/provisioning.json`, verifies Azure
ownership and operator Entra access, and checks the live `kube-system` namespace
UID. It automatically publishes:

* `cleanup-targets.json`: version 1, with a `targets` mapping for known, verified
  clusters. Each target records the exact `clusterId`, `clusterUid`, `context`,
  `workspace`, `group`, `kubeconfig`, and `radiusConfig`.
* `<slot>.kubeconfig`: the same private operator kubeconfig used by the harness,
  including `management.kubeconfig` for management.
* `cleanup-radius.yaml`: non-secret, cleanup-only workspace connections.
  Workspace and context names are `radplanes-<slot>`; each workspace's scope is
  `/planes/radius/local/resourceGroups/radplanes`. The active `radius.yaml` is
  never overwritten. Generated targets explicitly select this cleanup config.

The standard bootstrap's existing `management.kubeconfig` needs no exporter
marker. Before reusing it in place, the exporter checks its private permissions,
current context, and selected server/CA/exec profile against a fresh kubeconfig
from the verified, owned AKS, then reads the live cluster UID. A mismatching
existing file is neither used nor overwritten.

`--config` can select a provisioning file nested under `.state/azure`, such as
`.state/azure/operator-export/provisioning.json`. Acceptance paths stay relative
to that export directory; cleanup's kubeconfig and Radius references are always
relative to `.state/azure`. For that example, select
`--targets operator-export/cleanup-targets.json` when planning or executing
cleanup. Existing generated nested metadata with the old basename-only
references is repaired on export; manual files are not adopted.

Cleanup access is published before application/HTTPS gateway readiness checks.
Existing cluster IDs in management inventory are included even without a
showcase tenant assignment. A management app that is not ready can still yield
its own verified cleanup access, but cannot supply new child inventory.
`--once` still exits **3**, not 0, when the full demo export is incomplete;
cleanup metadata is not evidence of application readiness.

Rerunning the exporter creates or repairs missing cleanup metadata even when
the acceptance generation is unchanged. Files carry generated ownership
markers and project/subscription metadata; foreign, manual, symlinked, or
scope-changed cleanup files are refused, not overwritten. Review and preserve
conflicting operator files before rerunning; do not add a generated marker to
adopt them. Writes are atomic `0600` replacements under a required `0700` state
directory, with kubeconfig/workspace dependencies written before target metadata.

Normal cleanup requires an entry for **every existing cluster**. Allocated slots
whose AKS was never created do not need one. Previous exports are retained when
a cluster is temporarily unavailable; they are not cached authorization.
Each exporter watch pass refreshes live trust checks, and cleanup independently
rechecks ownership, UID, FQDN, and Radius scope before any mutation. A missing
target is an explicit blocker, never permission to skip a cluster.

These are trusted operator configuration files: kubeconfig exec plugins can run
local commands. Use exports from the approved clusters, not another project's
or the operator's global kubeconfig. Keep state directories mode `0700` and
credential files mode `0600`. Source files and evidence must not contain tokens,
DSNs, passwords, PFX data, or complete Secrets.

`--targets NAME` selects a different target metadata file within `.state/azure`.
All supplied kubeconfig/Radius files must stay inside `.state/azure` and must
not be symlinks. Context names must be `radplanes-<slot>`. For explicitly supplied
targets, `workspace` defaults to the context and `group` to `radplanes`.
Per-target `radiusConfig` overrides `--radius-config radius.yaml`.

The tool applies the existing project `HOME` workaround from the command
runner: each context gets `.state/azure/homes/<context>`, whose `.kube/config`
and bundled Bicep links target the exact project kubeconfig/compiler. Unexpected
links or redirected home directories fail. The caller's Azure CLI cache is
preserved through `AZURE_CONFIG_DIR`; no login/logout or global workspace
mutation is performed. A preview may prepare these local home links but never
executes deletion or scaling commands.

## Checks before deletion

The tool requires:

* Exact project/subscription IDs and all three ownership tags:
  `project=radplanes`, `managedBy=radius-todolist-app`,
  `SecurityControl=Ignore`.
* Exact allocation-derived app, cluster, and managed-node resource group names
  and full IDs. A matching name prefix alone never authorizes deletion.
* The four deterministic bootstrap role-definition GUIDs, matching custom role
  names, and exact project assignable scopes. Built-in role definitions cannot
  be selected through the manifest. The broad custom-role inventory is augmented
  with an exact GUID lookup for every manifest role; matching entries are
  deduplicated only after validation. An exact lookup returning another role
  ID, or any authorization/command failure, stops cleanup.
* Live group/resource/AKS tags and IDs. The saved vault, registry, and VNet must
  match the corresponding live platform resources. Recognized non-taggable
  child types inherit the checked group boundary only when their tags are
  absent; explicit foreign tags always fail.
* No unmanifested project-tagged group/resource. Such discoveries are reported,
  not deleted by prefix.
* For normal cleanup, the AKS FQDN, TLS verification, recorded cluster UID, and
  Radius workspace connection/group must match the supplied target. Radius
  application environments and referenced Azure IDs must stay in the owned
  scopes.

All cloud ownership and available Radius inventories are checked before
mutations. Each group is checked again before its provider deletion. If a group
contains an untagged resource type that cannot be verified, inspect it rather
than adding a blanket ownership bypass.

## Normal deletion order

First restore all parent-link test faults and finish or stop in-flight
onboarding using the existing operational procedures. The tool rejects
non-terminal AKS/Radius operations; it is not an operation-cancellation engine.

The normal path then:

1. Scales management API/provisioner Deployments to zero using their exact
   Radius labels and names, and waits for their Pods to disappear.
2. Deletes each child's Radius applications, data before control. This includes
   the Radius-owned PostgreSQL, Redis, and gateway resources. Remaining Azure
   app-group resources cause a visible failure before removing their cluster.
3. Deletes the discovered `Demo.Platform/clusters` records through management
   Radius under their per-slot provisioning applications. Each child AKS
   must actually be absent before management application deletion proceeds.
4. Deletes management's Radius applications, including empty cluster-application
   wrappers and explicitly scoped integration-gate applications.
5. Removes the exact bootstrap-owned groups. Child groups precede management;
   app groups precede their identities; management AKS is deleted explicitly
   before its identity group; the platform network, registry, DNS, and vault
   remain until dependents are gone.
6. Removes residual role assignments by **exact assignment ID** at owned
   scopes, then deletes the four custom role definitions by GUID.
7. Runs independent Azure verification before reporting success.

Radius deletion failures never fall through to provider deletion. Failed
terminal Radius resources are reported and their owner deletion is attempted;
they are not silently ignored or forcibly reset. Missing cluster ownership
records, duplicate records, orphaned app resources, and incomplete deletions
stop normal cleanup clearly.

This is not a transaction or automatic rollback. A failure may follow successful
deletion of earlier resources. Preserve evidence, inspect the reported phase,
and use an explicit reviewed recovery path.

### Reset only Radius-owned demo resources

After a terminal failed onboarding, a fresh demonstration can keep the
bootstrap foundation instead of rebuilding the registry, network, identities,
and management cluster:

```sh
uv run python operations/clean-azure.py --radius-only
CONFIRM_AZURE=yes uv run python operations/clean-azure.py --radius-only --execute
```

This follows normal deletion steps 1-4 above: child applications and their
managed datastores/gateways, child AKS through management Radius, then management
applications and PostgreSQL. It still verifies live ownership, cluster UIDs,
Radius scopes, terminal resources, empty app groups, and absent child AKS.
It cannot be combined with `--provider-only`; failures never bypass Radius.
The result is `radius_resources_removed` with `foundationRetained: true`, **not
`clean`**. Management Radius, its cluster, Azure groups, identities, roles, the
registry, network, and certificate vault remain.

This mode makes no direct Azure mutations. It does not inspect custom role
assignments because it never modifies them; normal full cleanup retains those
checks. Resource-group ownership checks remain mandatory, including the managed
node-group contents of every live AKS cluster before any deletion. Node groups
without a live AKS are neither inspected nor deleted in this mode; they are
listed in `uninspectedManagedNodeGroups`, not asserted absent. Normal full
cleanup still checks every allocated group, including orphaned node groups.
This avoids requesting permissions on node groups not yet created by AKS.
A scoped in-cluster operator needs Reader on all app/cluster/platform groups
and live AKS node groups, plus AKS cluster access, without Azure delete or
role-delegation grants. It still needs exported targets for every live
cluster. The mode neither installs a transport nor copies human tokens into
Pods.

Local credentials, evidence, and bootstrap-created state PVCs are retained.
`--credential-file` is rejected before any action in this mode. Preserve failed
acceptance evidence before explicitly clearing named application-state volumes
for a fresh management deployment. Never change a failed operation back to
pending or claim this reset proves whole-environment teardown.

Keyboard interruption exits **130** and reports incomplete cleanup. An Azure
operation may still be running and resources may remain. If interruption occurs
after verification, optional local credential removal may already be partial;
the message does not promise that all credentials were retained. Inspect state
and run read-only verification before deciding how to resume.

## Emergency provider-only path

If Radius is unavailable, first inspect the ownership plan:

```sh
uv run python operations/clean-azure.py --provider-only
CONFIRM_AZURE=yes uv run python operations/clean-azure.py --provider-only --execute
```

This is an explicit ownership-path bypass, **not** an ownership-check bypass.
All manifest, live tag, resource ID, custom role, and foreign-scope checks still
apply. The tool reports this mode prominently.

Provider-only teardown first removes management's app group to disable its
public entrypoint and provisioning database, then removes child app resources,
the exact child AKS instances, and their groups. Management AKS/identities and
the shared foundation are last. It never guesses resource groups from a prefix.

For unreliable laptop Kubernetes access, use a proven operator configuration
from an approved network that can reach the existing AKS API. The tool does not
broaden authorized IP ranges, create a new transport, or automatically use
`az aks command invoke`. A normal-path access failure is a blocker, not proof
that resources are absent.

## Verification, credentials, and retained records

`verify-clean.py` is Azure-read-only and needs no Kubernetes connection. It
requires every manifest group to be actually gone, every owned-scope assignment
and assignment using a project custom role to be absent, all four custom role
definitions to be deleted, and no active project-tagged resources/groups.
An unrelated subscription-level role assignment is neither deleted nor
mistaken for a project leftover. Role assignment reads disable principal-name
lookup, so they do not require Microsoft Graph. Absence of custom roles requires
both the broad inventory and `az role definition list --name <GUID>` for each
manifest GUID; a role omitted from the broad list still blocks a clean result.

The matching soft-deleted Key Vault record is reported separately, including
its scheduled purge date when returned. It is **not purged**. Purge protection
and name reservation remain in effect. A `clean` result means no active owned
resources/roles, not that recovery records, subscription deployment history,
registry evidence, or local state were erased.

Optional credential removal happens only after execution **and successful
verification**, for individually named top-level files:

```sh
CONFIRM_AZURE=yes uv run python operations/clean-azure.py --execute \
  --credential-file credentials.json \
  --credential-file management.kubeconfig \
  --credential-file shared-control.kubeconfig \
  --credential-file shared-control.key
```

Allowed names are `credentials.json`, `kubeconfig`, `radius.yaml`, and allocated
`<slot>.kubeconfig`/`<slot>.key` files. Symlinks and other names are refused.
The tool never deletes the whole `.state`, the bootstrap manifest, endpoint
metadata, name salt, provisioning settings, or evidence. Nested exports and
other credential material need separate, explicit operator handling.

## Source validation

Run only mocked tests during ordinary development:

```sh
mkdir -p .state/check/tmp
TMPDIR="$PWD/.state/check/tmp" PYTHONDONTWRITEBYTECODE=1 \
  uv run --no-sync pytest tests/operations/test_cleanup.py tests/harness -q
uv run --no-sync ruff check operations/clean-azure.py operations/verify-clean.py \
  harness/export-state.py tests/operations/test_cleanup.py \
  tests/harness/test_export_state.py tests/harness/test_cleanup_export.py
```

The test command runner is an in-memory model; real subprocess creation is
blocked. Test scratch stays under the project. These tests cover ownership
tampering, explicit contexts, deletion order, failed/partial operations,
provider-only intent, read-only verification, and post-verification credential
removal. They also cover exact-role inventory gaps, early/late interruption,
automatic cleanup handoff, partial exports, ownership refusal, and interrupted
atomic publication. No real Azure/Radius mutation is part of source validation.

Validated on **2026-09-09T16:37:07Z**: **171 tests and 63 subtests passed**;
Ruff checks, formatting checks, and all three CLI help entrypoints passed.
Actual cleanup and post-cleanup cloud verification remain parent-owned live
actions.
