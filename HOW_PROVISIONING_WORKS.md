# How Azure child-cluster provisioning works

The Azure cluster Recipe is a Bicep template executed by management Radius.
Python requests a logical Radius cluster resource. Radius translates that
request into an Azure AKS resource, using permissions granted during bootstrap.

The child cluster does not need to exist to run the Recipe. Everything that
creates it runs through **Radius in the management cluster**.

```text
Management AKS
  Management provisioner
    -> Requests Demo.Platform/clusters
      -> Management Radius selects the Azure cluster Recipe
        -> Radius's in-cluster deployment engine processes the template
          -> Azure APIs receive the native resource operations
            -> Azure creates child AKS
    -> Provisioner connects to child AKS
      -> Installs the child's Radius
      -> Deploys the child's application through that Radius
```

This walkthrough follows `shared-control` with the repository's pinned Radius
0.60.2. Code excerpts omit unrelated fields and checks; they are not standalone
deployment instructions. Use [README.md](README.md) for the operator sequence.
See [FINDINGS.md](FINDINGS.md#status) for recorded deployment and teardown results.

Radius deployment records and Azure resource-group deployment records are
different. The [deployment-scope explanation](#does-radius-create-an-arm-deployment-in-the-child-resource-group)
below shows where each operation happens.

## 1. Bootstrap prepares the resources and permissions

Before tenant onboarding, external bootstrap creates management AKS, shared
networking, and child allocations containing resources such as:

```text
Slot:                   shared-control
Cluster resource group: rg-radplanes-shared-control-cluster
Application group:      rg-radplanes-shared-control-app
Node subnet:            preallocated subnet ID
Identities:             controlPlane, kubelet, radius, gateway, certificateIssuer
```

The allocation is loaded from protected operator configuration. A tenant cannot
supply arbitrary resource groups, subnets, or identity IDs.

Bootstrap grants the **management Radius identity** permission to create AKS in
the allocated child cluster groups. It also grants narrowly scoped permissions
to use the precreated identities and create their federation bindings.

The **provisioner's identity is different**. It can obtain child-cluster access
and administer Kubernetes, but it cannot directly create AKS through Azure.
The public management API has neither identity's provisioning permissions.

Sources: [bootstrap](infra/bootstrap/azure.bicep) and
[identity boundaries](docs/azure-infrastructure.md#identity-boundaries-and-federation).

## 2. The provisioner receives an assigned control/data pair

The management API stores the tenant request in PostgreSQL. A separate
singleton provisioner claims the pending operation and calls `provision_pair()`.

For a new shared pair, its essential sequence is:

```python
slots = [f"{request.pair_id}-{role}" for role in ("control", "data")]

clusters = []
for role, slot in zip(("control", "data"), slots, strict=True):
    observe(f"{role}-cluster")
    clusters.append(provider.ensure_child_cluster(slot))

for role, cluster in zip(("control", "data"), clusters, strict=True):
    observe(f"{role}-radius")
    provider.bootstrap_child(cluster)

urls = [provider.deploy_plane(slot, observe) for slot in slots]
```

With `pair_id="shared"`, those slots are `shared-control` and `shared-data`.
The pair assignment comes from management's stored placement, not an arbitrary
slot supplied by the API caller. Both clusters are created before child Radius
installation begins.

If the pair is already available, a separate branch validates its inventory and
endpoints and reuses it. The run loop holds a PostgreSQL singleton lock.
Interrupted operations are marked explicitly; this is not an automatic
resume-and-replay workflow.

Sources: [management API](src/plane_demo/management/api.py),
[provisioner](src/plane_demo/management/provisioner.py), and
[`provision_pair()`](src/plane_demo/management/provisioning.py).

## 3. Management creates a slot-specific Radius environment

Management has already registered the custom resource type
`Demo.Platform/clusters` from [clusters.yaml](infra/radius/types/clusters.yaml).

`register_cluster_environment("shared-control")` maps that type to the published
Azure Recipe. Its configuration includes:

```python
"recipes": {
    "Demo.Platform/clusters": {
        "default": {
            "templateKind": "bicep",
            "templatePath": self.config.recipes["cluster"]["reference"],
            "parameters": {
                "allocations": {slot: plain(allocation)},
                "location": foundation["location"],
                "tenantId": foundation["tenantId"],
                # Also supplies node settings, authorized IP ranges, and tags.
            },
        }
    }
}
```

The `allocations` dictionary contains **only this slot**, not every available
allocation. The `templatePath` is the published Recipe reference in the project
ACR.

The environment separates the cluster where Radius runs from the Azure group
where it creates resources:

| Setting | Value for this example |
|---|---|
| Radius workspace | `radplanes-management` |
| Radius resource group | `radplanes`, a logical Radius group, not an Azure resource group |
| Radius environment | `provision-shared-control` |
| Radius application | `cluster-shared-control` |
| Kubernetes compute | `self`, meaning management AKS |
| Azure provider scope | `/subscriptions/<subscription-id>/resourceGroups/rg-radplanes-shared-control-cluster` |

The environment Bicep makes the separation explicit:

```bicep
compute: {
  kind: 'kubernetes'
  resourceId: 'self'
  namespace: namespace
}
providers: {
  azure: {
    scope: '/subscriptions/${azureSubscriptionId}/resourceGroups/${azureResourceGroup}'
  }
}
```

**Changing the Azure provider scope does not move Radius into the child
cluster.** It tells management Radius where to create native Azure resources.
Management's ordinary application environment continues to target its own
application resource group.

Sources: [`register_cluster_environment()`](src/plane_demo/management/providers/azure.py)
and [Azure environment declaration](infra/radius/environments/azure.bicep).

## 4. Python submits a Radius resource, not an Azure AKS command

`ensure_child_cluster()` deploys the small child-cluster declaration:

```python
environment = self.register_cluster_environment(slot)
application = f"cluster-{slot}"

self.deploy(
    "management",
    "child-cluster",
    application,
    {"slot": slot},
    environment=environment,
)
```

The first argument, `"management"`, selects management's Radius workspace and
kubeconfig. The helper constructs a `rad deploy` command with an explicit
project configuration, workspace, group, environment, application, and
parameter file.

The submitted Bicep resource is:

```bicep
resource cluster 'Demo.Platform/clusters@2025-08-01-preview' = {
  name: slot
  properties: {
    application: application
    environment: environment
    slot: slot
  }
}
```

This says: "Create a logical cluster for this allocation." It does not contain
AKS networking, node pools, or Azure identity configuration. Those details
belong to the default Recipe selected by the environment.

Sources: [`ensure_child_cluster()` and `deploy()`](src/plane_demo/management/providers/azure.py)
and [child-cluster declaration](infra/radius/modules/child-cluster.bicep).

## 5. Radius resolves the Recipe and creates AKS

Radius finds the environment's default Recipe for `Demo.Platform/clusters` and
downloads its published Bicep artifact from the private ACR.

The environment configures Recipe-download authentication separately using an
`azureWorkloadIdentity` SecretStore. It uses **management Radius's identity**,
not the future child's identity. Azure provider credential registration alone
does not configure Recipe downloads.

Before submission, the provisioner also verifies the published Recipe's expected
digest and that its tag is locked against writes and deletion.

Radius supplies the Recipe with two kinds of input:

| Input | Purpose |
|---|---|
| `context` | Describes the requested Radius resource, application, environment, and runtime |
| Recipe parameters | Supply the approved allocation, location, identities, and cluster settings |

The Recipe joins those inputs:

```bicep
param context object
param allocations object

var slot = context.resource.properties.slot
var allocation = allocations[slot]
```

It then declares the actual Azure resource. This shortened excerpt shows the
central mapping:

```bicep
resource cluster 'Microsoft.ContainerService/managedClusters@2025-05-01' = {
  name: allocation.clusterName
  location: location
  tags: requiredTags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${allocation.identities.controlPlane.id}': {}
    }
  }
  properties: {
    kubernetesVersion: kubernetesVersion
    nodeResourceGroup: allocation.nodeResourceGroup
    enableRBAC: true
    disableLocalAccounts: true
    oidcIssuerProfile: {
      enabled: true
    }
    securityProfile: {
      workloadIdentity: {
        enabled: true
      }
    }
    // Node pools, Entra access, networking, and other properties omitted.
  }
}
```

The full Recipe uses the allocated node subnet and kubelet identity, Azure CNI
Overlay with Cilium, and the existing NAT Gateway. Its default system pool has
two `Standard_D4s_v5` nodes.

**Radius's in-cluster deployment engine processes this template and applies
the native resource operations through Azure APIs.** Python does not run
`az aks create`. Processing a template here does not mean submitting an Azure
resource-group ARM deployment; see the deployment-scope explanation below.

The Recipe is deliberately flat. This repository encountered a deployment-scope
problem with cross-resource-group nested modules in Radius 0.60.2. The
implementation uses a per-slot Azure provider scope and native resources rather
than those nested modules. This does not mean all Radius Bicep modules are
unsupported.

Sources: [Azure cluster Recipe](infra/radius/recipes/azure/cluster.bicep) and
[documented scope constraint](docs/azure-infrastructure.md#per-slot-management-radius-provisioning-environment-f017).

## 6. The Recipe binds the new cluster to its precreated identities

After AKS exposes its OIDC issuer URL, the Recipe creates federated identity
credentials for the future child Radius service accounts:

```bicep
@batchSize(1)
resource radiusFederation 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2024-11-30' = [for account in radiusAccounts: {
  parent: radiusIdentity
  name: 'radius-${account}'
  properties: {
    issuer: cluster.properties.oidcIssuerProfile.issuerURL
    subject: 'system:serviceaccount:radius-system:${account}'
    audiences: [
      'api://AzureADTokenExchange'
    ]
  }
}]
```

The four accounts are `applications-rp`, `bicep-de`, `ucp`, and `dynamic-rp`.
A separate binding is created for the child's certificate issuer.

These bindings let matching Kubernetes service accounts exchange their
projected tokens for Azure access as the precreated managed identities. The
reference to the new cluster's issuer creates the dependency on AKS creation.
The child Radius pods do not need to exist when these bindings are created.

`@batchSize(1)` serializes the four writes because concurrent federation updates
on the same identity previously caused Azure HTTP 409 conflicts. **The Recipe
creates bindings, not managed identities or Azure role assignments.**

Source: [federation resources in the cluster Recipe](infra/radius/recipes/azure/cluster.bicep).

## 7. Radius returns resource ownership and connection metadata

The Recipe's output has two important parts:

```bicep
output result object = {
  resources: concat([cluster.id], radiusFederationIds, [issuerFederation.id])
  values: {
    clusterId: cluster.id
    clusterName: cluster.name
    resourceGroup: allocation.clusterResourceGroup
    fqdn: cluster.properties.fqdn
    oidcIssuer: cluster.properties.oidcIssuerProfile.issuerURL
    bootstrapAccessRef: cluster.id
    radiusIdentityId: allocation.identities.radius.id
    radiusClientId: allocation.identities.radius.clientId
  }
}
```

`resources` tells Radius which Azure resources belong to this logical cluster,
including the federation bindings. `values` exposes the non-secret results on
the Radius resource.

The provisioner reads that resource back and checks its Azure cluster ID, name,
resource group, and child Radius client ID against the approved allocation. A
mismatch raises `cluster_output_mismatch`.

There is **no kubeconfig or access token in the Recipe output**.

One naming detail: `cluster.id` in the **Recipe** is an Azure AKS resource ID.
In the **child-cluster declaration**, `cluster.id` is a Radius resource ID,
including its `output clusterId` expression. The provisioner reads the Azure
ID from the returned resource properties, not that declaration output.

Sources: [Recipe outputs](infra/radius/recipes/azure/cluster.bicep) and
[`ensure_child_cluster()`](src/plane_demo/management/providers/azure.py).

## 8. The provisioner installs Radius into the child

Once the returned resource matches the allocation, `get_access()` uses the
coordinator identity to obtain **AKS user credentials**, without `--admin`.
It keeps the kubeconfig private, converts it with `kubelogin`, and checks
Kubernetes access with `kubectl get nodes`.

Then `bootstrap_child()` runs
[operations/install-radius.py](operations/install-radius.py) against that
child. The installer constructs this command, shown with illustrative shell
variables:

```bash
rad --config "$PROJECT_CONFIG" install kubernetes \
  --kubecontext "$CHILD_CONTEXT" \
  --skip-contour-install \
  --set dashboard.enabled=false \
  --set global.azureWorkloadIdentity.enabled=true
```

The installer annotates the four Radius service accounts with the **child's
Radius identity**, enables workload-identity projection on their Deployments,
restarts them, and verifies the projected identity settings. The provider then
registers the child's resource types, Azure credentials, and application
environment.

That child's application environment targets
`rg-radplanes-shared-control-app`, not its cluster resource group. Its own Radius
can now create control PostgreSQL, the gateway, and application workloads.
The corresponding data cluster's Radius creates Redis, its gateway, and data
workloads.

The child AKS itself remains owned by **management Radius's
`cluster-shared-control` application**. Child application resources must be
removed through the child's Radius before management Radius removes the cluster.
See [cleanup](docs/cleanup.md) before any deletion.

Infrastructure completion is also separate from tenant readiness. Management
reports the tenant ready when control creates its record, not when AKS creation
returns or when the data API becomes healthy.

## Does Radius create an ARM deployment in the child resource group?

**Not for this flat cluster Recipe.** Radius creates a deployment operation
under its own control-plane scope. It does not submit the Recipe as a normal
Azure resource-group deployment equivalent to `az deployment group create`.

The word "deployment" refers to different things here:

| Object or operation | Location and owner |
|---|---|
| Kubernetes Deployment `bicep-de` | Runs Radius's Bicep deployment engine in management AKS |
| Recipe deployment record | Radius scope under `/planes/radius/local/resourceGroups/radplanes` |
| Native AKS and federation resources | Azure scope under the allocated child cluster resource group |
| Ordinary Azure ARM deployment-history record | Azure subscription or resource-group scope, such as the external bootstrap deployment |

### The two provider scopes

In Radius 0.60.2, the Bicep Recipe driver generates a deployment ID with this
shape:

```text
/planes/radius/local/resourceGroups/radplanes/providers/Microsoft.Resources/deployments/recipe<timestamp>
```

The `Microsoft.Resources/deployments` type name alone does not make this an
Azure deployment-history entry. Its scope begins with `/planes/radius/local`,
not `/subscriptions/...`.

The driver creates this deployment through its UCP deployment client and
configures native Azure resource placement separately. The relevant provider
configuration has this shape, with the other provider fields omitted:

```json
{
  "deployments": {
    "type": "Microsoft.Resources",
    "value": {
      "scope": "/planes/radius/local/resourceGroups/radplanes"
    }
  },
  "az": {
    "type": "AzureResourceManager",
    "value": {
      "scope": "/subscriptions/<subscription-id>/resourceGroups/rg-radplanes-shared-control-cluster"
    }
  }
}
```

The `deployments` provider controls the scope for deployment/module records.
The `az` provider controls the scope for the AKS and other native Azure resources.
The child AKS therefore receives an Azure ID with this shape:

```text
/subscriptions/<subscription-id>/resourceGroups/rg-radplanes-shared-control-cluster/providers/Microsoft.ContainerService/managedClusters/<allocated-cluster-name>
```

An ordinary Azure resource-group deployment would instead have an ID ending in
`/providers/Microsoft.Resources/deployments/<deployment-name>` under that
`/subscriptions/.../resourceGroups/...` scope. The current cluster Recipe
declares no such Azure deployment resource.

This separation explains the earlier nested-module failure: a reference
compiled with an Azure deployment scope did not match the deployment/module
scope used by Radius. The flat Recipe and per-slot environment avoid that
mismatch without moving child AKS ownership outside management Radius.

### What to expect in Azure

The child resource group receives the AKS resource and federation updates.
Use Azure resource state and Activity Log for the native resource operations.
Use management Radius resource status and deployment-engine logs for Recipe
execution. Do not use the Azure resource group's **Deployments** list as the
complete record of Radius provisioning.

External bootstrap is different. [operations/project.py](operations/project.py)
explicitly invokes `az deployment sub create` and deploys resource-group
modules. Those are real Azure ARM deployments. Bootstrap or Azure-managed
service activity can therefore produce Azure deployment-history records;
their presence does not mean the child cluster Recipe was submitted as one.

This explanation is based on the pinned implementation and this repository's
flat Recipe. It is not a fresh inspection of a live Azure deployment or a claim
about every possible Recipe implementation.

## Is this a design choice or a limitation?

**It is an intentional Radius architecture choice. It is also a limitation if
the required contract is Azure-native deployment history and recovery
independent of the Radius cluster.**

Bicep is the authoring language and compiler. The service executing its compiled
template determines where deployment operations and history live:

```text
Bicep + az deployment group create
  -> Azure Resource Manager executes the template
  -> Azure resource-group deployment history

Bicep + rad deploy / Radius Recipe execution
  -> Radius's in-cluster engine executes the template
  -> Radius deployment APIs and resource state
  -> Native Azure resources still exist in Azure Resource Manager
```

Radius's engine can process Radius, Kubernetes, Azure, and other provider
resources in one deployment. Separate provider scopes are part of that design,
not a workaround invented by this project. The earlier cross-resource-group
module failure is a separate, version-specific constraint; it did not cause
Radius to adopt an in-cluster deployment engine.

### Where state and audit information are visible

| Information | Where it lives or is exposed |
|---|---|
| Actual AKS, database, gateway, and other Azure resource configuration | Azure Resource Manager and the Azure portal |
| Logical Radius resources, provisioning status, and recorded output-resource ownership | Radius APIs, backed by Radius's configured resource store |
| Recipe deployment operations and progress | Radius's deployment-engine API and logs, not Azure deployment history |
| Tenant placement and provisioning-operation records | This application's management PostgreSQL database |
| Azure management-plane writes and their calling identity | Azure Activity Log |

The default Kubernetes installation uses the API-server resource store. The
pinned chart sets `database.enabled: false`; this repository's installer does
not override it. Its UCP configuration therefore selects:

```yaml
databaseProvider:
  provider: apiserver
  apiserver:
    context: ""
    namespace: radius-system
```

The store persists Radius resource data in Kubernetes custom resources of type
`ucp.dev/v1alpha1/Resource`, exposed as `resources.ucp.dev`. Sensitive values use
the configured Kubernetes Secret store, and the asynchronous queue also has an
API-server backend. The management application's PostgreSQL database is **not**
automatically Radius's own state database.

The `DeploymentTemplate` and `DeploymentResource` CRDs are separate
Kubernetes-controller entrypoints. Their existence does not mean that every
CLI or Recipe deployment has a corresponding object of those types. Prefer
Radius's APIs when inspecting its logical resources; backing objects are not a
supported manual recovery interface.

### What persistence does and does not guarantee

The configured Radius resource store is not just pod memory. Persisted Kubernetes
objects remain when a Radius pod is replaced. That is different from losing the
management cluster or deleting its state.

Losing only management AKS does not itself delete the separate child AKS
clusters or their managed Azure dependencies. However, without recovery of the
corresponding Radius state, the replacement management installation does not
automatically regain the original ownership metadata, configuration, and
provisioning workflow. Templates and Azure resource IDs alone are not evidence
of a safe adoption or recovery procedure.

This demo has not established control-plane backup/restore, disaster recovery,
or an audit-retention guarantee for Recipe deployment history. Its interrupted
provisioning behavior is explicit rather than automatic replay. See the
[accepted limitations](docs/limitations.md).

Azure Activity Log provides cloud-side evidence even though an ARM deployment
wrapper was not created. However, the caller for a Recipe's Azure writes is the
Radius managed identity. That identity alone does not identify the originating
tenant request or operator. An end-to-end audit trail needs correlation with
the management operation and Radius execution, plus deliberate log retention.
Operational logs and current state are not automatically an immutable audit
archive.

Ordinary Azure ARM deployment history would provide an Azure-managed record of
template execution, parameters, outputs, and operations. It would not replace
the application's tenant database, Radius's ownership model, or backups.
Likewise, changing Radius's resource-store backend would not make its deployment
records appear in Azure's deployment history.

If Azure-native deployment-history records are mandatory, the current Recipe
path does not satisfy that requirement. An explicit Azure ARM deployment
integration would need separate design and verification, including Radius
ownership, outputs, update/delete behavior, and recovery. Merely adding a
Bicep module or changing an environment scope is not a demonstrated solution.

### Pinned implementation references

| Radius 0.60.2 source | What it establishes |
|---|---|
| [Deployment-engine architecture](https://github.com/radius-project/radius/blob/v0.60.2/docs/architecture/deployment-engine.md) | The in-cluster engine executes templates, routes individual operations, and exposes deployment progress |
| [State-persistence architecture](https://github.com/radius-project/radius/blob/v0.60.2/docs/architecture/state-persistence.md) | Radius resource, secret, and queue stores are separate persistence subsystems |
| [Default UCP store and deployment routing](https://github.com/radius-project/radius/blob/v0.60.2/deploy/Chart/templates/ucp/configmaps.yaml) | Selects API-server storage by default and routes Radius-scoped `Microsoft.Resources` requests to `bicep-de` |
| [Bicep Recipe execution](https://github.com/radius-project/radius/blob/v0.60.2/pkg/recipes/driver/bicep/bicep.go#L147-L187) | Creates an incremental Recipe deployment and waits for its result |
| [Deployment ID and Azure provider scope](https://github.com/radius-project/radius/blob/v0.60.2/pkg/recipes/driver/bicep/bicep.go#L368-L399) | Builds the Radius-scoped deployment ID and separately sets the environment's Azure scope |
| [Default provider configuration](https://github.com/radius-project/radius/blob/v0.60.2/pkg/sdk/clients/providerconfig.go#L24-L55) | Gives the deployments provider a Radius scope, not the Azure resource-group scope |
| [Deployment client request](https://github.com/radius-project/radius/blob/v0.60.2/pkg/sdk/clients/resourcedeploymentsclient.go#L176-L191) | Sends the request to the client's configured deployment-engine endpoint |
| [In-cluster deployment engine](https://github.com/radius-project/radius/blob/v0.60.2/deploy/Chart/templates/de/deployment.yaml) | Runs `bicep-de` as a Kubernetes Deployment with UCP integration |
