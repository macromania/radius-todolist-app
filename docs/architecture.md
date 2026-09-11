# Three-plane architecture

The demo separates tenant provisioning, configuration ownership, and application
requests. Each plane runs in its own Kubernetes cluster. Shared tenants reuse
one control/data pair; an isolated tenant receives a separate pair.

| Plane | Runtime | Owns |
|---|---|---|
| Management | Public API and separate singleton provisioner | Tenant requests, placement, provisioning operations, child-cluster creation through Radius |
| Control | API and management reconciler | Tenant configuration in PostgreSQL; reports control-record creation to management |
| Data | API and control reconciler | Local tenant ConfigMaps and Redis counters; reports ConfigMap application to control |

## Request and reconciliation paths

1. An operator posts a tenant request to management. Its API validates and stores
   the request; it has no provisioning tools or deployment credentials.
2. The non-public provisioner claims the request under a PostgreSQL singleton
   lock. For a missing pair, management Radius creates both child clusters.
   The provisioner installs child Radius and deploys each child's application.
3. The control reconciler pulls management's tenant record and creates its
   initial configuration once. It reports this result to management.
4. The data reconciler pulls control's latest configuration, writes its local
   ConfigMap, and reports that result to control.
5. Data API requests read only local ConfigMaps and Redis. They do not query
   parent databases. A control API update follows the same pull path.

Management `ready` means the control record exists, not that Redis or the data
application is healthy. Control `applied` means the local ConfigMap was written.
Later configuration updates belong to control; repeated management polls do
not overwrite them. After a parent outage, children catch up to the latest
version without replaying intermediate updates.

## Infrastructure and identity boundaries

Only bootstrap directly creates management's cluster. Management Radius creates
children; each child's Radius owns its workloads and dependencies. The same
three application declarations use Azure or local Recipes without branching
on environment names.

Runtime code is under `src/plane_demo/`. `operations/` administers infrastructure;
`harness/` exercises the APIs, records outcomes, and injects bounded faults.
Neither is another application plane.

API and provisioner images/identities remain separate. The data API's
`data-api-runtime` account can get ConfigMaps but cannot read namespace Secrets,
including the reconciler's parent credential. Redis connection injection is
performed by kubelet, not by granting the API Secret-reader permissions.

See [API and database contracts](contracts.md), [Azure operations](azure.md),
[local provider stages](local-provider.md), and [accepted limitations](limitations.md).
Actual deployment, outage, persistence, and deletion results are in
[FINDINGS.md](../FINDINGS.md), not inferred from this architecture.
