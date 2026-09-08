# radius-todolist-app

One application definition, two environments. `infra/radius/app.bicep` describes
a todo app that needs a Redis cache. On a local kind cluster that cache is a pod;
on Azure Kubernetes Service it is Azure Managed Redis behind a private endpoint.
The application file is byte-identical in both cases — the difference lives
entirely in `infra/radius/environments/`.

The application itself is the public Radius sample, `ghcr.io/radius-project/samples/demo`,
pinned by digest.

## Quick start

Local, against a kind cluster with Radius already installed:

    make up-local     # create the environment and deploy
    make test-local   # prove a todo survives deleting the pod

Azure, from nothing:

    make registry-azure   # resource groups and the Recipe registry
    make publish-recipe   # publish the custom Recipe, lock the tag
    make infra-azure      # network, private DNS zone, AKS cluster
    make radius-azure     # Radius control plane and its Azure identity
    make env-azure        # the Azure Radius environment
    make up-azure         # deploy the same app.bicep
    make test-azure       # read a todo back out of Azure Managed Redis

    make clean-azure      # delete everything (do this; it is not cheap)

`make help` lists every target.

### Publishing the Recipe again

`make publish-recipe` can be rerun. If the tag already exists and matches
`RECIPE_EXPECTED_DIGEST`, it skips publishing and ensures the tag is locked.
It refuses an existing tag with a different digest. Registry lookup failures
stop the command rather than being treated as a missing tag.

Reusing a tag does not publish local source changes. To release a changed Recipe,
use a new version, for example `make publish-recipe RECIPE_TAG=0.1.1`. Then update
`RECIPE_TAG` and `RECIPE_EXPECTED_DIGEST` in the Makefile using the printed digest
before running `make env-azure`. Do not unlock an old tag to overwrite it.

The registry returns HTTP 405 if a push tries to overwrite a locked tag. A
successful `az acr login` does not bypass that lock.

## Layout

    infra/
      main.bicep                 network, private DNS, Log Analytics, AKS
      main.bicepparam            Azure platform parameters
      registry.bicep             the registry that holds the Recipe
      radius/
        bicepconfig.json         Radius Bicep extension configuration
        app.bicep                the application; identical for both environments
        environments/
          local.bicep            maps redisCaches to an in-cluster Redis pod
          azure.bicep            maps redisCaches to the custom Azure Recipe
        recipes/
          azure/
            managed-redis.bicep  Azure Managed Redis + private endpoint
    scripts/                     setup, publishing and tests
    ports.env                    reserved local port block, 35490-35499

Only custom Recipes live in this repository. Local uses Radius's published
`local-dev/rediscaches` Recipe, so there is no local Recipe source to maintain.
The environment files select published Recipes explicitly; the directory names
are for organization, not automatic discovery. Run the existing Make targets
from the repository root.

## Four things that will bite you

Each of these produces a deployment that looks healthy and fails later, so each
is guarded by a check in `.github/workflows/validate.yml`.

**The Recipe must emit `tls: true`.** Radius infers TLS from `port == 6380`.
Azure Managed Redis uses port 10000, so without an explicit value Radius builds
a plaintext `redis://` URL against a TLS endpoint. Measured against the real
service: `rediss://` gives `/healthz` 200, `redis://` gives 500.

**The Recipe must percent-encode the access key.** Radius concatenates the
password into a URL without encoding it. Azure keys are 44 characters of base64,
which contains `/` roughly half the time, and an unencoded `/` terminates the
URL authority. The Redis client then throws `TypeError: Invalid URL` — so the
deployment works or fails depending on which key Azure happened to generate.
`uriComponent()` fixes it, and the client decodes it back exactly.

A consequence worth knowing: `CONNECTION_REDIS_PASSWORD` and
`CONNECTION_REDIS_CONNECTIONSTRING` therefore arrive percent-encoded. Nothing
here reads them, but a future consumer would need to decode.

**The connection must stay named `redis`, lowercase.** Radius uppercases the
connection name to build `CONNECTION_REDIS_*`, which is what the app reads.
Rename it to `cache` and you get `CONNECTION_CACHE_*`, and the app silently
stores todos in process memory instead.

**`rad workspace create` validates that the Radius resource group and the
environment already exist.** So the workspace has to be created twice: once with
only a Kubernetes context, and again once both exist. The Makefile does this.

## Security posture

Azure Managed Redis has no public endpoint. It is created with
`publicNetworkAccess: 'Disabled'` and reached through a private endpoint in
`snet-privatelink`, resolved by the `privatelink.redis.azure.net` private DNS
zone. From a laptop the hostname resolves but the connection times out; from
inside the cluster it resolves to a private address.

Radius holds Contributor on `rg-todolist-app` only, never on
`rg-todolist-platform` which holds the cluster, so a compromise of the Radius
control plane cannot reconfigure or delete the cluster it runs on. It also holds
Network Contributor on one subnet and Private DNS Zone Contributor on one zone.

The AKS cluster uses Entra ID with Azure RBAC and has local accounts disabled,
so `kubelogin` and an explicit role assignment are required to reach it.

Radius v0.60 rejects digest references for Recipes and requires a tag, even
though `rad bicep publish` prints a digest URL and calls it the way to pin the
artifact immutably. Immutability is enforced two other ways instead: the tag is
locked in the registry (`writeEnabled=false`, `deleteEnabled=false`), and
`scripts/setup-env-azure.sh` refuses to deploy unless the tag still resolves to
the digest recorded in the Makefile.

**There is deliberately no public URL for the application.** The sample image
serves `GET /api/container-info`, which returns its entire process environment —
including the Redis password in three forms — and its landing page renders it.
Exposing it would publish the credential. Adding a gateway requires first
building a patched image with that endpoint removed.

## Costs

The AKS cluster dominates: four `Standard_D2s_v5` nodes, a Standard load
balancer and Log Analytics ingestion, roughly $280-350 a month. Azure Managed
Redis `Balanced_B0` is about $12. The registry is about $20. This is a spike;
run `make clean-azure` when you are done.

Note that `make clean-azure` does not delete a fallback service principal, if
one was created.
