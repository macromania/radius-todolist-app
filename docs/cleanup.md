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

Before execution, export the current operator kubeconfigs and relevant Radius
workspace configurations from the management provisioner's protected state.
Child files on its PVC must be exported **before** management is removed.
Create a non-secret `.state/azure/cleanup-targets.json`:

```json
{
  "version": 1,
  "targets": {
    "management": {
      "clusterId": "COPY managementCluster.id FROM BOOTSTRAP OUTPUT",
      "clusterUid": "COPY VERIFIED kube-system NAMESPACE UID",
      "context": "radplanes-management",
      "workspace": "radplanes-management",
      "group": "radplanes",
      "kubeconfig": "kubeconfig",
      "radiusConfig": "radius.yaml"
    },
    "shared-control": {
      "clusterId": "COPY THE ALLOCATED CHILD AKS RESOURCE ID",
      "clusterUid": "COPY VERIFIED CHILD kube-system NAMESPACE UID",
      "context": "radplanes-shared-control",
      "kubeconfig": "shared-control.kubeconfig"
    }
  }
}
```

The uppercase strings are placeholders, not working defaults. Include an entry
for **every existing cluster**. Allocated slots whose AKS was never created do
not need an entry. `workspace` defaults to the explicit context; `group`
defaults to `radplanes`. Per-target `radiusConfig` overrides
`--radius-config radius.yaml`.

Obtain only the non-secret UID from an already verified target:

```sh
kubectl --kubeconfig .state/azure/shared-control.kubeconfig \
  --context radplanes-shared-control get namespace kube-system \
  -o jsonpath='{.metadata.uid}'
```

These are trusted operator configuration files: kubeconfig exec plugins can run
local commands. Use exports from the approved clusters, not another project's
or the operator's global kubeconfig. Keep state directories mode `0700` and
credential files mode `0600`. Source files and evidence must not contain tokens,
DSNs, passwords, PFX data, or complete Secrets.

`--targets NAME` selects a different target metadata file within `.state/azure`.
All supplied kubeconfig/Radius files must stay inside project state and must
not be symlinks. Context names must be `radplanes-<slot>`.

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
  be selected through the manifest.
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
lookup, so they do not require Microsoft Graph.

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
  --credential-file kubeconfig \
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
  uv run --no-sync pytest tests/operations/test_cleanup.py -q
uv run --no-sync ruff check operations/clean-azure.py operations/verify-clean.py \
  tests/operations/test_cleanup.py
```

The test command runner is an in-memory model; real subprocess creation is
blocked. Test scratch stays under the project. These tests cover ownership
tampering, explicit contexts, deletion order, failed/partial operations,
provider-only intent, read-only verification, and post-verification credential
removal. No real Azure/Radius mutation is part of source validation.

Validated on **2026-09-09T13:26:37Z**: **34 tests and 12 subtests passed**;
Ruff checks, formatting checks, and both CLI help entrypoints passed. Actual
cleanup and post-cleanup cloud verification remain parent-owned live actions.
