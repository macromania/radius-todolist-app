# AGENTS.md

A Radius application deployed to two targets: a local kind cluster and Azure
Kubernetes Service. `infra/radius/app.bicep` is identical for both.
Read `README.md` first.

## The rule that shapes everything

`infra/radius/app.bicep` must never contain a conditional on the environment.
If it needs one, the design has failed. Environment differences belong in
`infra/radius/environments/local.bicep` and
`infra/radius/environments/azure.bicep`, which map the
`Applications.Datastores/redisCaches` resource type to different Recipes.

## Where things are

- `infra/radius/app.bicep` — the application. Environment-agnostic.
- `infra/radius/bicepconfig.json` — extension configuration for the Radius subtree.
- `infra/radius/environments/` — local and Azure Recipe maps. Local uses a
  published Radius Recipe; it has no custom Recipe source in this repository.
- `infra/radius/recipes/azure/managed-redis.bicep` — the custom Recipe. Its header
  comment explains three non-obvious requirements; read it before editing.
- `infra/main.bicep` — network, private DNS, Log Analytics, AKS.
- `infra/registry.bicep` — the registry holding the Recipe. Deployed separately
  and first, because the Recipe must exist before
  `infra/radius/environments/azure.bicep` can reference it.
- `scripts/` — setup and the two acceptance tests.
- `.github/workflows/validate.yml` — compiles every Bicep file and asserts the
  invariants listed below.

## Commands

Use the Makefile; it encodes ordering that is not obvious. `make help` lists
targets. `make check` compiles everything.

Use the Bicep compiler bundled with `rad` (`~/.rad/bin/bicep`), not `az bicep`.
The `az` one is older: it lacks the `redisEnterprise` types and rejects
`@secure()` outputs with BCP129. Radius files additionally need the `radius`
extension, which only `rad bicep generate-kubernetes-manifest` resolves.

## Invariants

These four are enforced in CI because each produces a deployment that looks
healthy and fails later.

1. The Recipe emits `tls: true`. Radius otherwise infers TLS from `port == 6380`
   and would build a plaintext URL for a port-10000 service.
2. The Recipe wraps the access key in `uriComponent()`. Radius embeds the
   password in a URL unencoded, and base64 keys contain `/` about half the time,
   which throws `TypeError: Invalid URL` in the client.
3. The connection in `infra/radius/app.bicep` is named `redis`, lowercase.
   Radius uppercases it into `CONNECTION_REDIS_*`. Any other name and the app
   silently falls back to in-memory storage.
4. The Recipe sets `publicNetworkAccess: 'Disabled'`.

## Things that cost time if you do not know them

`rad workspace create` validates that the Radius resource group and environment
already exist, so bootstrapping creates the workspace twice.

`environment` and `application` are injected by the CLI but must still be
declared as parameters, or Bicep fails with BCP063.

The application namespace is `<environment namespace>-<application name>`, for
example `todolist-azure-todolist`.

Radius rejects digest references for Recipes and requires a tag. Immutability
comes from locking the tag in the registry plus the digest assertion in
`scripts/setup-env-azure.sh`.

`az aks show --query oidcIssuerProfile.issuerUrl` — note the casing. The ARM API
and the Radius docs both say `issuerURL`, which returns an empty string with a
zero exit status.

Availability zones are immutable after node pool creation, and AKS supports only
zones 2 and 3 for this subscription in eastus2 even though the VM SKU lists all
three.

## Do not

Do not put the sample image behind a public gateway. `GET /api/container-info`
returns the whole process environment, including the Redis password. A public
endpoint requires a patched image first.

Do not put real data in the local environment. The local Recipe creates an
unauthenticated, non-TLS Redis with a `redis-cli MONITOR` sidecar that writes
every command to pod logs.

Do not leave the Azure resources running. `make clean-azure`.
