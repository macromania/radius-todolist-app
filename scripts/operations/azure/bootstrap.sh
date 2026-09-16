#!/usr/bin/env bash
set -Eeuo pipefail
set +x
umask 077
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd -P)
# shellcheck source=azure.shlib
source "$ROOT/scripts/operations/azure/azure.shlib"
# shellcheck source=../../lib/discovery.sh
source "$ROOT/scripts/lib/discovery.sh"

if [[ "${1:-}" == --help ]]; then
  printf '%s\n' 'Usage: CONFIRM_AZURE=yes bash scripts/operations/azure/bootstrap.sh' \
    'Reads root .env. Completes the Azure foundation and management Radius installation before success.' \
    'Prints freshly queried ARM outputs. An external DEMO_KEY_VAULT remains externally owned and unmodified.'
  exit 0
fi
(( $# == 0 )) || { demo_error 'bootstrap takes no arguments'; exit 1; }
demo_status section 'Bootstrap: configuration and tools'
azure_init
demo_status section 'Bootstrap: account, vault and operator discovery'
azure_discover_vault

azure_json account show > "$AZURE_WORKSPACE/account.json"
jq -e --arg subscription "$AZURE_SUBSCRIPTION_ID" '
  (.id | ascii_downcase) == $subscription and .user.type == "user" and .state == "Enabled"
' "$AZURE_WORKSPACE/account.json" >/dev/null || {
  demo_error 'Expected an enabled selected subscription with an interactive operator login'; exit 1;
}

azure_json account get-access-token --resource-type ms-graph > "$AZURE_WORKSPACE/graph-token.json"
jq -er '
  .accessToken | select(type == "string" and test("^[A-Za-z0-9._~+/=-]+$")) |
  "header = " + (("Authorization: Bearer " + .) | @json)
' "$AZURE_WORKSPACE/graph-token.json" > "$AZURE_WORKSPACE/graph.curl"
curl --fail --silent --show-error --max-time 20 --proto '=https' \
  --config "$AZURE_WORKSPACE/graph.curl" "https://graph.microsoft.com/v1.0/me?\$select=id" \
  --output "$AZURE_WORKSPACE/operator.json"
OPERATOR=$(jq -er '.id | select(test("^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$"))' \
  "$AZURE_WORKSPACE/operator.json")
OPERATOR_IP=$(curl --fail --silent --show-error --max-time 20 --proto '=https' https://api.ipify.org)
printf '%s' "$OPERATOR_IP" | jq -Rse '
  if test("^(0|[1-9][0-9]{0,2})(\\.(0|[1-9][0-9]{0,2})){3}$") then
  (split(".") | map(tonumber)) as $a |
  ($a | all(.[]; . <= 255)) and
  ($a[0] > 0 and $a[0] < 224 and $a[0] != 10 and $a[0] != 127) and
  ([$a[0],$a[1]] != [169,254]) and ([$a[0],$a[1]] != [192,168]) and
  (($a[0] == 100 and $a[1] >= 64 and $a[1] <= 127) | not) and
  (($a[0] == 172 and $a[1] >= 16 and $a[1] <= 31) | not) and
  (($a[0] == 198 and ($a[1] == 18 or $a[1] == 19)) | not) and
  ([$a[0],$a[1],$a[2]] as $prefix |
    [[192,0,0],[192,0,2],[192,88,99],[198,51,100],[203,0,113]] |
    any(.[]; . == $prefix) | not)
  else false end
' >/dev/null || { demo_error 'Operator address must be a canonical public IPv4 address'; exit 1; }

demo_status section 'Bootstrap: existing resource ownership'
GROUPS_JSON=$(printf '%s\n' "rg-$STEM-platform" \
  "rg-$STEM-"{management,shared-control,shared-data,isolated-1-control,isolated-1-data}-{cluster,app,nodes} \
  | jq -Rsc 'split("\n") | map(select(length > 0))')
azure_json group list --query '[].{name:name,tags:tags}' > "$AZURE_WORKSPACE/group-candidates.json"
jq --arg prefix "rg-$STEM-" '[.[] | select((.name | ascii_downcase) | startswith($prefix))]' \
  "$AZURE_WORKSPACE/group-candidates.json" > "$AZURE_WORKSPACE/groups.json"
jq -e --argjson expected "$GROUPS_JSON" --arg project "$DEMO_PROJECT" \
  --arg deployment "$DEMO_DEPLOYMENT" '
  type == "array" and all(.[];
    ((.name | ascii_downcase) as $name | $expected | index($name) != null) and
    ((.name | ascii_downcase | endswith("-nodes")) or
      (.tags.project == $project and .tags.deployment == $deployment and
       .tags.environment == "azure" and .tags.managedBy == "radius-todolist-app")))
' "$AZURE_WORKSPACE/groups.json" >/dev/null || {
  demo_error 'An existing resource group has a different owner or unexpected name'; exit 1;
}
while IFS= read -r group; do
  lower_group=$(printf '%s' "$group" | tr '[:upper:]' '[:lower:]')
  if [[ "$lower_group" == *-nodes ]]; then
    slot="${lower_group#"rg-$STEM-"}"
    slot="${slot%-nodes}"
    azure_json aks show --resource-group "rg-$STEM-$slot-cluster" --name "aks-$STEM-$slot" \
      > "$AZURE_WORKSPACE/node-owner.json"
    azure_owned < "$AZURE_WORKSPACE/node-owner.json" || {
      demo_error 'Node resource group is not attached to an owned AKS cluster'; exit 1;
    }
    jq -e --arg group "$group" --arg id \
      "$SUBSCRIPTION_SCOPE/resourceGroups/rg-$STEM-$slot-cluster/providers/Microsoft.ContainerService/managedClusters/aks-$STEM-$slot" '
      (.nodeResourceGroup | ascii_downcase) == ($group | ascii_downcase) and
      (.id | ascii_downcase) == ($id | ascii_downcase)
    ' "$AZURE_WORKSPACE/node-owner.json" >/dev/null || {
      demo_error 'Node resource group differs from its actual AKS owner'; exit 1;
    }
    continue
  fi
  azure_json resource list --resource-group "$group" > "$AZURE_WORKSPACE/resources.json"
  jq -e --arg project "$DEMO_PROJECT" --arg deployment "$DEMO_DEPLOYMENT" '
    type == "array" and all(.[];
      if (.type | ascii_downcase | test(
        "^microsoft\\.(network/(virtualnetworks|publicipaddresses|natgateways|networksecuritygroups|privatednszones|privateendpoints|networkinterfaces)|managedidentity/userassignedidentities|containerregistry/registries|keyvault/vaults|containerservice/managedclusters)$"))
      then .tags.project == $project and .tags.deployment == $deployment and
        .tags.environment == "azure" and .tags.managedBy == "radius-todolist-app"
      else (.tags.project == null or .tags.project == $project) and
        (.tags.deployment == null or .tags.deployment == $deployment)
      end)
  ' "$AZURE_WORKSPACE/resources.json" >/dev/null || {
    demo_error 'An existing foundation resource has missing or foreign ownership'; exit 1;
  }
done < <(jq -r '.[].name' "$AZURE_WORKSPACE/groups.json")

demo_status section 'Bootstrap: subscription prerequisites'
[[ -x "$ROOT/.venv/bin/python" ]] || {
  demo_error 'Run uv sync --locked before bootstrap'; exit 1;
}
demo_run 'Azure subscription prerequisites' "$ROOT/.venv/bin/python" \
  "$ROOT/scripts/operations/azure/prerequisites.py" --subscription "$AZURE_SUBSCRIPTION_ID" \
  > "$AZURE_WORKSPACE/prerequisites.json"

REGISTRY_EXISTS=false
for kind in registry vault; do
  if [[ "$kind" == vault && "$VAULT_OWNED" == false ]]; then
    continue
  fi
  if [[ "$kind" == registry ]]; then
    name="$REGISTRY"; resource_type='Microsoft.ContainerRegistry/registries'
  else
    name="$VAULT"; resource_type='Microsoft.KeyVault/vaults'
  fi
  azure_json resource list --name "$name" --resource-type "$resource_type" \
    > "$AZURE_WORKSPACE/global-name.json"
  count=$(jq -er 'if type == "array" then length else error("Invalid resource list") end' \
    "$AZURE_WORKSPACE/global-name.json")
  if [[ "$count" == 0 ]]; then
    if [[ "$kind" == registry ]]; then
      azure_json acr check-name --name "$name" > "$AZURE_WORKSPACE/availability.json"
    else
      # az keyvault check-name targets managed HSMs, not ordinary vaults.
      jq -n --arg name "$name" '{name:$name,type:"Microsoft.KeyVault/vaults"}' \
        > "$AZURE_WORKSPACE/vault-name.json"
      azure_json rest --method post \
        --url "https://management.azure.com$SUBSCRIPTION_SCOPE/providers/Microsoft.KeyVault/checkNameAvailability?api-version=2024-11-01" \
        --body "@$AZURE_WORKSPACE/vault-name.json" > "$AZURE_WORKSPACE/availability.json"
    fi
    jq -e '.nameAvailable == true' "$AZURE_WORKSPACE/availability.json" >/dev/null || {
      demo_error 'Deterministic global name is unavailable or retention-protected; select another deployment'
      exit 1
    }
  elif [[ "$count" == 1 ]]; then
    if ! jq -e --arg id "$PLATFORM_SCOPE/providers/$resource_type/$name" '
      (.[0].id | ascii_downcase) == ($id | ascii_downcase)
    ' "$AZURE_WORKSPACE/global-name.json" >/dev/null; then
      demo_error 'A deterministic global name belongs to another resource'; exit 1
    fi
    jq '.[0]' "$AZURE_WORKSPACE/global-name.json" | azure_owned || {
      demo_error 'A deterministic global name belongs to another resource or owner'; exit 1;
    }
    if [[ "$kind" == registry ]]; then
      REGISTRY_EXISTS=true
      azure_registry
    fi
  else
    demo_error 'Ambiguous deterministic global resource identity'; exit 1
  fi
done

azure_json deployment sub list --query "[?name=='$STEM-bootstrap']" \
  > "$AZURE_WORKSPACE/prior-deployments.json"
jq -e --arg project "$DEMO_PROJECT" --arg deployment "$DEMO_DEPLOYMENT" '
  type == "array" and length <= 1 and all(.[];
    .properties.parameters.projectName.value == $project and
    .properties.parameters.deploymentName.value == $deployment and
    .properties.parameters.environment.value == "azure")
' "$AZURE_WORKSPACE/prior-deployments.json" >/dev/null || {
  demo_error 'The subscription deployment name already belongs to another identity'; exit 1;
}
jq -e 'all(.[]; .properties.provisioningState |
  . == "Succeeded" or . == "Failed" or . == "Canceled")' \
  "$AZURE_WORKSPACE/prior-deployments.json" >/dev/null || {
  demo_error 'Bootstrap deployment is still active or has an unknown state; inspect it before retrying'
  exit 1
}

CREDENTIAL_NAMES=$(azure_credential_names | jq -Rsc 'split("\n") | map(select(length > 0))')
EXTERNAL_VAULT_GROUP=''
[[ "$VAULT_OWNED" == true ]] || EXTERNAL_VAULT_GROUP="$VAULT_RESOURCE_GROUP"
jq -e --arg vault "$VAULT" --arg group "$EXTERNAL_VAULT_GROUP" '
  all(.[]; .properties.parameters.vaultName.value == $vault and
    (.properties.parameters.externalVaultResourceGroup.value // "") == $group)
' "$AZURE_WORKSPACE/prior-deployments.json" >/dev/null || {
  demo_error 'Existing deployment has a different vault binding; implicit migration is refused'; exit 1;
}

demo_status section 'Bootstrap: compile and validate the foundation'
jq -n --arg project "$DEMO_PROJECT" --arg deployment "$DEMO_DEPLOYMENT" \
  --arg location "$AZURE_LOCATION" --arg registry "$REGISTRY" --arg vault "$VAULT" \
  --arg operator "$OPERATOR" --arg ip "$OPERATOR_IP" --arg hash "$IDENTITY_HASH" \
  --arg externalVaultGroup "$EXTERNAL_VAULT_GROUP" --argjson credentialNames "$CREDENTIAL_NAMES" \
  --argjson registryExists "$REGISTRY_EXISTS" '{
    "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
    contentVersion: "1.0.0.0",
    parameters: ({
      projectName:$project, deploymentName:$deployment, environment:"azure", location:$location,
      registryName:$registry, vaultName:$vault, operatorObjectId:$operator, operatorIp:$ip,
      deploymentHash:$hash, externalVaultResourceGroup:$externalVaultGroup,
      applicationCredentialNames:$credentialNames, registryExists:$registryExists
    } | map_values({value:.}))
  }' > "$AZURE_WORKSPACE/parameters.json"
"$BICEP" build "$ROOT/infra/bootstrap/azure.bicep" --outfile "$AZURE_WORKSPACE/bootstrap.json"
azure_json deployment sub validate --location "$AZURE_LOCATION" --name "$STEM-bootstrap" \
  --template-file "$AZURE_WORKSPACE/bootstrap.json" --parameters "@$AZURE_WORKSPACE/parameters.json" \
  > "$AZURE_WORKSPACE/validated.json"
jq -e '.properties.provisioningState == "Succeeded"' "$AZURE_WORKSPACE/validated.json" >/dev/null || {
  demo_error 'Foundation validation did not reach Succeeded; no deployment was submitted'; exit 1;
}
demo_status section "Bootstrap: ARM deployment $STEM-bootstrap"
azure_json deployment sub create --location "$AZURE_LOCATION" --name "$STEM-bootstrap" \
  --template-file "$AZURE_WORKSPACE/bootstrap.json" --parameters "@$AZURE_WORKSPACE/parameters.json" \
  > "$AZURE_WORKSPACE/created.json" || {
    result=$?
    demo_status error "Foundation deployment failed; Azure resources may remain. Inspect failed operations:"
    printf '  az deployment operation sub list --subscription %s --name %s-bootstrap --output json\n' \
      "$AZURE_SUBSCRIPTION_ID" "$STEM" >&2
    demo_status warning 'An unexpected BYOIP requirement must be diagnosed from the failed request, not enabled automatically.'
    exit "$result"
  }
jq -e '.properties.provisioningState == "Succeeded"' "$AZURE_WORKSPACE/created.json" >/dev/null || {
  demo_error 'Foundation deployment did not reach Succeeded'; exit 1;
}
BOOTSTRAP_PHASE='live foundation verification'
demo_status section "Bootstrap: $BOOTSTRAP_PHASE"
trap 'printf "ERROR: ARM deployment %s succeeded, but bootstrap is incomplete at %s. Azure resources are retained; rerun normal bootstrap after resolving the failure.\n" "$STEM-bootstrap" "$BOOTSTRAP_PHASE" >&2' ERR
azure_foundation
BOOTSTRAP_PHASE='registry repository permission validation'
demo_status section "Bootstrap: $BOOTSTRAP_PHASE"
azure_registry
azure_recipe_policy
BOOTSTRAP_PHASE='management Radius identity validation'
demo_status section "Bootstrap: $BOOTSTRAP_PHASE"
jq -e --arg identity \
  "$SUBSCRIPTION_SCOPE/resourceGroups/rg-$STEM-management-cluster/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-$STEM-management-radius" \
  --slurpfile account "$AZURE_WORKSPACE/account.json" '
  [.allocations[] | select(.slot == "management")] as $management |
  ($management | length) == 1 and
  ($management[0].identities.radius.id | ascii_downcase) == ($identity | ascii_downcase) and
  (.foundation.tenantId | ascii_downcase) == ($account[0].tenantId | ascii_downcase) and
  ([$management[0].identities.radius.clientId, .foundation.tenantId] |
    all(.[]; type == "string" and
      test("^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")))
' "$AZURE_WORKSPACE/foundation.json" >/dev/null
RADIUS_CLIENT_ID=$(jq -er '.allocations[] | select(.slot == "management") | .identities.radius.clientId' \
  "$AZURE_WORKSPACE/foundation.json")
RADIUS_TENANT_ID=$(jq -er '.foundation.tenantId' "$AZURE_WORKSPACE/foundation.json")
BOOTSTRAP_PHASE='management cluster access'
demo_status section "Bootstrap: $BOOTSTRAP_PHASE"
TMPDIR="$AZURE_WORKSPACE" demo_workspace
demo_open_cluster management
BOOTSTRAP_PHASE='management Radius installation'
demo_status section "Bootstrap: $BOOTSTRAP_PHASE"
BICEP_BIN="$BICEP" bash "$ROOT/scripts/operations/install-radius.sh" \
  --workspace-root "$AZURE_WORKSPACE" \
  --context "$DEMO_CONTEXT" --kubeconfig "$DEMO_KUBECONFIG" \
  --config "$DEMO_WORKSPACE/management/radius.yaml" \
  --client-id "$RADIUS_CLIENT_ID" --tenant-id "$RADIUS_TENANT_ID" \
  > "$AZURE_WORKSPACE/radius-install.json"
BOOTSTRAP_PHASE='management Radius verification'
demo_status section "Bootstrap: $BOOTSTRAP_PHASE"
jq -e --arg context "$DEMO_CONTEXT" \
  '.context == $context and .workload_identity_verified == true' \
  "$AZURE_WORKSPACE/radius-install.json" >/dev/null
demo_status success 'Bootstrap completed: Azure foundation and management Radius workload identity are verified.'
cat "$AZURE_WORKSPACE/foundation.json"
