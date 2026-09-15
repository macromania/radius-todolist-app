# Local cleanup and verification

Normal local cleanup uses `.env` and live Docker, Kubernetes and Radius owners.
It destroys the selected demo's PostgreSQL, Redis and node-local volume data.
Preserve any required acceptance report before deletion.

```sh
# Read-only ownership and deletion plan:
make local-clean-plan

# Execute the normal Radius-owned deletion path:
CONFIRM_LOCAL=yes make local-clean

# Independent Docker verification; no saved cleanup record:
make local-verify
```

The underlying entrypoint is `scripts/operations/local/cleanup.py`. Its default
mode is a plan; `--execute` deletes, and `--verify` observes current absence.
There is no cloud connection, image build, dependency installation or fallback
to Azure. The revised cleanup still needs live proof; historical results in
[FINDINGS.md](../FINDINGS.md) describe their original source revisions.

## Selected ownership

`STEM` is `PROJECT-DEPLOYMENT-local`. The five logical slots remain management,
shared-control, shared-data, isolated-1-control and isolated-1-data. Their kind
clusters are `STEM-SLOT`, using the reserved Kubernetes API ports 35495-35499.
The Radius group is `STEM`; application namespaces are `STEM-SLOT-ROLE`.

Discovery verifies actual full Docker IDs, exact node names/labels, running
state, private addresses, cluster/namespace identities, and certificate-backed
access. Kubeconfigs and Radius HOME/configuration are private and temporary.
A saved `acceptance.json`, image report, bootstrap marker, exporter process or
management HTTP API is not required.

The selected management Radius must own each child through its custom cluster
resource and provisioning application/environment. Each child Radius owns its
application resources. Unexpected resources, owner mismatches, non-terminal
operations and unavailable observations fail explicitly.

## Terraform state is deletion authority

The operator validates the complete backend inventory and the actual Terraform
payload before asking Radius to destroy resources. It checks the expected
managed resource types/addresses, exact child identity, credentials/access
binding, image inputs, namespace and application/resource IDs.

Names and Secret UIDs alone are insufficient: a same-UID payload can change.
The state and its ownership are revalidated immediately before app and child
deletion. Foreign kind clusters, extra managed resources, old gate-only state,
changed images and changed bindings are not adopted. Reports include bounded
identifiers and hashes, not credentials or raw state payloads.

## Faults and active bootstrap work

Restore parent-link faults using the normal harness command before cleanup.
Resource-owned journals and current network-namespace/rule observations must
show that no relevant fault remains. Cleanup never flushes rules, edits
policies, repairs journals or treats a missing local report as restoration.

An extant `management-bootstrap` Lease is an active or interrupted bootstrap
condition. Cleanup checks it before destructive boundaries and refuses to race,
delete or take it over. There is no expiry heuristic or automatic replay.
The normal host operator factory releases its owned Lease on exit.

## Deletion order

1. Quiesce management API/provisioner and observe their termination.
2. Delete data applications, then control applications, through child Radius.
3. Verify child applications, backend state and associated resources are absent.
4. Delete custom child-cluster owners through management Radius.
5. Verify child Docker nodes, Terraform state and access Secrets are absent.
6. Delete management applications, then the verified bootstrap-owned management
   kind cluster.
7. Independently inspect Docker for current selected-cluster absence.

No child kind cluster is directly deleted to recover from a Radius failure.
A timeout is not proof that a remote operation stopped. Missing owners,
reappearing resources, leftover backend/access state or incomplete deletion
block later steps.

Execution compares unrelated containers observed before and after cleanup and
requires their preservation. Independent verification reports current selected
absence; without a prior record it does not invent historical observations.

## Retained state

The shared Docker kind network, global images/cache, unrelated containers,
arbitrary host volumes and global contexts/credentials remain untouched.
The operator does not bulk-delete `.state`, `.env` or source files. Historical
evidence is not a live deployment and is not cleanup authorization.

Reports go to stdout. Save a report explicitly if needed, but do not make its
path a prerequisite for the next command.

## Offline checks

```sh
uv run --no-sync pytest -q tests/operations/local/test_local_cleanup.py \
  tests/operations/test_live_cleanup.py
```

The normal entrypoints, payload revalidation, fault/Lease refusal and independent
verification are exercised with command/API doubles. Those results are not a
claim that the revised live teardown has run.
