# Full local cleanup

`operations/local/cleanup.py` implements bounded, owner-ordered teardown of the
five-cluster local demonstration. **It destroys the demo databases, Redis data,
and node-local PVC storage.** Run acceptance and preserve its evidence first.
Offline tests are not proof that live cleanup or the full local scenario passed.
The earlier one-child gate has its own cleanup command and historical evidence;
this operator does not adopt that gate's resource or Terraform state.

```sh
# Read-only Docker/Kubernetes/Radius checks and an ordered preview:
uv run python operations/local/cleanup.py

# Explicit destructive opt-in, only after reviewing the preview:
uv run python operations/local/cleanup.py --execute

# Independent read-only verification using the exact retained cleanup record:
uv run python operations/local/cleanup.py \
  --verify .state/local/evidence/cleanup-<run-id>.json
```

The default command never scales or deletes resources. It may prepare a private
cleanup-only Radius configuration, project HOME directories, and their exact
kubeconfig links. An existing, different cleanup configuration is refused.
`--verify` performs
only Docker inventory/inspection; it never connects to deleted Kubernetes
clusters or writes a new cleanup result. Neither mode builds images, provisions
clusters, or touches Azure.

## Required ownership evidence

Export state while **all five clusters remain reachable**:

```sh
uv run python harness/local/export-state.py --once
```

Cleanup requires the protected `.state/local/provisioning.json`,
`runtime-images.json`, `management-created.json`, and `acceptance.json`.
It admits only these allocation keys and contexts:

| Slot | Cluster/context |
|---|---|
| `management` | `radplanes-local-management` |
| `shared-control` | `radplanes-local-shared-control` |
| `shared-data` | `radplanes-local-shared-data` |
| `isolated-1-control` | `radplanes-local-isolated-1-control` |
| `isolated-1-data` | `radplanes-local-isolated-1-data` |

The acceptance export's `targets` must include every slot. Cleanup uses its
existing `cluster_id`, `cluster_uid`, `namespace_uid`, `context`, `namespace`,
and `kubeconfig` fields, plus `local.node.id`, `local.node.address`,
`local.access_secret`, and `local.access_secret_uid`. It compares the export's
`local_images` with the protected runtime image review. No additional cleanup
export format is required. Paths remain within private `.state/local`;
symlinked state and credential paths are refused.

During preflight, cleanup reads each completed, ownership-verified Terraform
state in memory and records only its backend Secret name/UID, lineage, and
serial. The keys in that state must match the actual owned child's protected
access and live cluster identity. It rechecks the recorded state identity
before requesting deletion; there is no partial-state adoption or resume.

Management must use the original bootstrap kubeconfig at
`.state/local/home/.kube/config`, not the operator's global context or a
provisioner service-account profile. The bootstrap record must contain the
actual Docker ID and `secretEncryptionVerified: true`. The provisioning,
export, and inspected runtime image/source identities must agree. The current
deployed source files must match the inspected image source hashes; the cleanup
operator itself is hashed separately so a cleanup-only fix does not require
rebuilding running application images. Both management
Deployment images and the corresponding Docker image IDs are rechecked.

Before the first mutation, the operator checks:

* All five full Docker IDs, exact node names, and kind ownership labels. Extra
  project names, missing nodes, foreign labels, and partial topology stop cleanup.
* Every live `kube-system` UID and application namespace UID against the export.
  Namespace names are `radplanes-local-<slot>-<management|control|data>`.
* Certificate-only kubeconfigs, the reserved loopback API ports 35495–35499,
  child certificate names, and CA/client credentials matching the management
  access Secret. The protected child endpoint must use its actual kind node IP.
  There is no TLS bypass, exec credential plugin, or proxy fallback.
* Radius workspace connections and group `radplanes-local`; exactly the
  management/control/data application in each cluster, plus management's
  `cluster-<slot>` wrappers. Each custom cluster belongs to that wrapper and the
  `provision-<slot>` environment.
* Terminal Radius resource states, exact resource/application/environment
  references, and the complete Terraform backend inventory. Backend names are
  `tfstate-default-` followed by the first 40 hex characters of
  SHA-256(`lower(environmentName + "-" + applicationName + "-" + resourceId)`),
  using the actual bound resource references. Radius 0.60.2
  [`secretSuffixInput`](https://github.com/radius-project/radius/blob/v0.60.2/pkg/recipes/terraform/config/backends/kubernetes.go)
  includes the application name when present: full cluster state uses
  `provision-<slot>-cluster-<slot>-<resourceId>`, while application dependencies
  use `<slot>-<role>-<resourceId>`. The application-free gate formula must not
  be used for these resources. Legacy SHA-1 names and unexpected state are
  refused, never adopted.
* Application-scoped discovery cross-checked against the native resource-group
  inventory, not the CLI's default-environment resource list. Core containers
  may omit an explicit environment only under a validated parent application;
  custom resources still require their exact environment. Native inventory
  rejects foreign/duplicate IDs, incomplete pages, and request failures. Empty
  environment definitions and retained ARM deployment-history records are not
  workload owners.
* Each child's Terraform state Secret UID, lineage, serial, completed kind
  resource, pinned node image, and tracked access Secret. Full cleanup requires
  exactly three managed resources, including
  `module.default.terraform_data.images[0]` with the built-in Terraform provider,
  null input/output, and replacement triggers matching the child kind ID and
  inspected API/provisioner image references in order. Other managed resources
  or changed image inputs are refused. The two-resource, no-image gate variant
  remains exclusive to the separate gate cleanup. An access Secret UID,
  ownership label, or Radius-reference annotation mismatch stops deletion.
* The exact management API/provisioner Deployment UIDs and labels.
* Restored local fault journals and live reconciler Pod network namespaces.
  Journal checks require recorded physical restoration and matching rule
  hashes. Read-only `iptables -S OUTPUT` checks reject remaining
  `plane-demo-fault-` rules. Cleanup never injects, removes, or repairs faults.

Restore faults through the existing harness procedure before cleanup. A stale
or incomplete fault journal is a blocker, not permission to modify live rules.
Recognized fault filenames with missing strategy or identity are refused.
Attempted faults require a successful restoration probe and the same live
reconciler Pod/node/network sandbox. Historical failed outcomes or old
restoration errors do not override a later, verified restoration.
Finish in-flight provisioning first. This operator is not cancellation,
adoption, or partial-state recovery machinery.

## Deletion order and proof

1. Quiesce management API and provisioner with atomic Deployment UID/label
   preconditions, then wait for their Pods to terminate.
2. Delete the two data applications, then the two control applications,
   through each child's Radius. Requery applications and the native resource-group
   inventory after every deletion: CLI application-scoped resource listing fails
   once that application is gone. A zero exit code with a remaining application
   is failure. Previously removed resource IDs remain excluded during later
   checks; an old ID reappearing is also failure.
3. Delete each custom cluster resource through **management Radius**, data
   before control. The native DELETE uses `2025-08-01-preview`, explicit JSON
   media types, and the verified management CA/client certificate. It disables
   environment proxies and redirects and does not retry transport failures.
   The expected resource ID is
   `/planes/radius/local/resourceGroups/radplanes-local/providers/Demo.Platform/clusters/<slot>`.
4. Wait for actual Radius resource, Docker node, Terraform state Secret, and
   management access Secret absence. Only then delete the empty
   `cluster-<slot>` Radius application wrapper.
5. Delete management's Radius application/resources. Refuse management
   deletion while any child, Terraform backend state, or access Secret remains.
6. Recheck management's Docker identity and cluster UID. Delete this
   **operator-owned bootstrap cluster only** with `kind delete`, its exact name,
   and the original project kubeconfig.
7. Check all five original Docker IDs and all project cluster names/labels are
   absent. Verify that unrelated container IDs captured before cleanup still
   exist. Legitimate new unrelated containers are reported, not removed or
   treated as failure.

The child Recipe's normal kind deletion releases node-local PostgreSQL/Redis
PVC storage. Management node removal also removes bootstrap-owned local
workload/state PVC storage inside that node. The tool does **not** directly
delete a child kind cluster, remove Docker containers or arbitrary volumes,
delete Kubernetes Secrets, force Radius resources, or edit Radius backing state.

Immediately before each child deletion, its complete native workload inventory,
application list, and all `tfstate=true` Secrets must be empty. Final management
deletion likewise requires an empty native workload inventory, not merely an
empty environment-filtered CLI result. Native reads use the verified cluster
CA/client identity and expected child TLS name, without proxies or redirects.

Individual commands and the overall reporting window are bounded. A timeout
does not prove Radius or Terraform stopped; preserve the record and inspect the
reported owner. There is no automatic retry, resume, rollback, or alternate
direct-provider deletion path.

## Retained results and limitations

Execution writes private, timestamped
`.state/local/evidence/cleanup-<run-id>.json` with non-secret exact identities,
intent/completed steps, provenance, and the outcome. Failure preserves that
record and all earlier evidence. Successful output is
`{"scope":"cleanup","result":"resources_removed",...}`—never application
acceptance. Verification requires the retained proof of each child's Radius,
Terraform, and access Secret deletion and management application removal.

The shared Docker `kind` network, global images/cache, unrelated containers,
arbitrary host volumes, global kubecontexts, and global credentials are never
deleted. Protected `.state/local` configuration, exports, credentials, image
reviews, gate runs, and acceptance evidence remain as a **historical archive**.
The tool does not remove local credentials or rewrite prior failed results.
Kind may remove the management entry from the project bootstrap kubeconfig;
cleanup first requires the matching, separate exported `management.kubeconfig`
to remain as the protected historical copy.

This is a full, completed-topology cleanup command. Missing export entries,
partial creation, foreign state, a replaced cluster, or an unavailable owner
requires a separate reviewed operator decision; there is no `--force` or
direct-child deletion escape hatch.
