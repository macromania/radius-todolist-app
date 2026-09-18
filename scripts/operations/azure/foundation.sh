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
  printf '%s\n' 'Usage: CONFIRM_AZURE=yes bash scripts/operations/azure/foundation.sh' \
    'Internal first-time stage: creates the default Azure foundation. Radius is installed in a separate guarded phase.' \
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
  "rg-$STEM-"{management,shared-control,shared-data}{,-nodes} \
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
    azure_json aks show --resource-group "rg-$STEM-$slot" --name "aks-$STEM-$slot" \
      > "$AZURE_WORKSPACE/node-owner.json"
    azure_owned < "$AZURE_WORKSPACE/node-owner.json" || {
      demo_error 'Node resource group is not attached to an owned AKS cluster'; exit 1;
    }
    jq -e --arg group "$group" --arg id \
      "$SUBSCRIPTION_SCOPE/resourceGroups/rg-$STEM-$slot/providers/Microsoft.ContainerService/managedClusters/aks-$STEM-$slot" '
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

azure_json deployment sub list --query "[?name=='$STEM-bootstrap']" \
  > "$AZURE_WORKSPACE/layout-deployments.json"
jq -e --arg project "$DEMO_PROJECT" --arg deployment "$DEMO_DEPLOYMENT" '
  type == "array" and length <= 1 and all(.[];
    .properties.outputs.foundation.value.resourceGroupLayout == "plane-v2" and
    .properties.outputs.foundation.value.environmentMode == "prepared-v1" and
    .properties.parameters.projectName.value == $project and
    .properties.parameters.deploymentName.value == $deployment and
    .properties.parameters.environment.value == "azure" and
    (.properties.provisioningState | . == "Succeeded" or . == "Failed" or . == "Canceled"))' \
  "$AZURE_WORKSPACE/layout-deployments.json" >/dev/null || {
  demo_status error "Old or incomplete resource-group layout for $STEM-bootstrap. Existing resources are retained."
  demo_status warning 'Azure can retain old subscription deployment records after their resource groups are deleted.'
  demo_status warning 'Run make init ENV=azure with a fresh --deployment name in ARGS, then retry bootstrap.'
  printf '\n%s\n%s\n' \
    'See RUN_AZURE_SCENARIOS.md under "Select deployment identity" for the full command.' \
    'Do not bypass the layout guard.' >&2
  exit 1
}
if jq -e 'length > 0' "$AZURE_WORKSPACE/groups.json" >/dev/null; then
  jq -e 'length == 1' "$AZURE_WORKSPACE/layout-deployments.json" >/dev/null || {
    demo_error 'Existing resources have no verified bootstrap layout; resources retained'; exit 1;
  }
  jq '.[0].properties.outputs | map_values(.value)' "$AZURE_WORKSPACE/layout-deployments.json" \
    > "$AZURE_WORKSPACE/existing-foundation.json"
  "$ROOT/.venv/bin/python" "$ROOT/scripts/operations/azure/plane_policy.py" \
    --foundation "$AZURE_WORKSPACE/existing-foundation.json" --allow-missing \
    > "$AZURE_WORKSPACE/existing-plane-policy.json"
elif jq -e 'length == 0' "$AZURE_WORKSPACE/layout-deployments.json" >/dev/null; then
  azure_json role definition list --custom-role-only true \
    --query "[?starts_with(roleName, '$STEM ')]" > "$AZURE_WORKSPACE/existing-roles.json"
  jq -e 'type == "array" and length == 0' "$AZURE_WORKSPACE/existing-roles.json" >/dev/null || {
    demo_error 'Custom roles remain without a verified foundation; use a fresh deployment name'
    exit 1
  }
fi

demo_status section 'Bootstrap: subscription prerequisites'
[[ -x "$ROOT/.venv/bin/python" ]] || {
  demo_error 'Run uv sync --locked before bootstrap'; exit 1;
}
demo_run 'Azure subscription prerequisites' "$ROOT/.venv/bin/python" \
  "$ROOT/scripts/operations/azure/prerequisites.py" --subscription "$AZURE_SUBSCRIPTION_ID" \
  > "$AZURE_WORKSPACE/prerequisites.json"

demo_status section 'Bootstrap: node capacity and size'
demo_run 'Bicep: compile foundation' "$BICEP" build "$ROOT/infra/bootstrap/azure.bicep" \
  --outfile "$AZURE_WORKSPACE/bootstrap.json"
node_arguments=(--config "$ROOT/.env" --template "$AZURE_WORKSPACE/bootstrap.json")
if [[ -f "$AZURE_WORKSPACE/existing-foundation.json" ]]; then
  node_arguments+=(--existing-foundation "$AZURE_WORKSPACE/existing-foundation.json")
fi
demo_run 'AKS node size selection' "$ROOT/.venv/bin/python" \
  "$ROOT/scripts/operations/azure/node_sizes.py" "${node_arguments[@]}" \
  > "$AZURE_WORKSPACE/node-size.json"
AZURE_NODE_VM_SIZE=$(jq -er '
  .nodeVmSize | select(type == "string" and test("^Standard_[A-Za-z0-9_]{1,64}$"))
' "$AZURE_WORKSPACE/node-size.json") || {
  demo_error 'Node-size selection returned an invalid VM size'; exit 1;
}
NODE_COUNT=$(jq -er '.nodeCount | select(type == "number" and . >= 2 and floor == .)' \
  "$AZURE_WORKSPACE/node-size.json") || {
  demo_error 'Node-size selection returned an invalid node count'; exit 1;
}
export AZURE_NODE_VM_SIZE

demo_status section 'Bootstrap: PostgreSQL compute'
postgres_arguments=(--config "$ROOT/.env")
if [[ -f "$AZURE_WORKSPACE/existing-foundation.json" ]]; then
  postgres_arguments+=(--existing-foundation "$AZURE_WORKSPACE/existing-foundation.json")
fi
demo_run 'PostgreSQL compute selection' "$ROOT/.venv/bin/python" \
  "$ROOT/scripts/operations/azure/postgres_sizes.py" "${postgres_arguments[@]}" \
  > "$AZURE_WORKSPACE/postgres-size.json"
AZURE_POSTGRES_SKU=$(jq -er '
  .postgresSkuName | select(type == "string" and test("^Standard_[A-Za-z0-9_]{1,64}$"))
' "$AZURE_WORKSPACE/postgres-size.json") || {
  demo_error 'PostgreSQL selection returned an invalid SKU'; exit 1;
}
AZURE_POSTGRES_TIER=$(jq -er '
  .postgresSkuTier | select(. == "GeneralPurpose")
' "$AZURE_WORKSPACE/postgres-size.json") || {
  demo_error 'PostgreSQL selection returned an invalid tier'; exit 1;
}
export AZURE_POSTGRES_SKU AZURE_POSTGRES_TIER

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

demo_status section 'Bootstrap: validate the foundation'
jq -n --arg project "$DEMO_PROJECT" --arg deployment "$DEMO_DEPLOYMENT" \
  --arg location "$AZURE_LOCATION" --arg registry "$REGISTRY" --arg vault "$VAULT" \
  --arg operator "$OPERATOR" --arg ip "$OPERATOR_IP" --arg hash "$IDENTITY_HASH" \
  --arg externalVaultGroup "$EXTERNAL_VAULT_GROUP" --argjson credentialNames "$CREDENTIAL_NAMES" \
  --arg nodeVmSize "$AZURE_NODE_VM_SIZE" --argjson nodeCount "$NODE_COUNT" \
  --arg postgresSkuName "$AZURE_POSTGRES_SKU" --arg postgresSkuTier "$AZURE_POSTGRES_TIER" \
  --argjson registryExists "$REGISTRY_EXISTS" '{
    "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
    contentVersion: "1.0.0.0",
    parameters: ({
      projectName:$project, deploymentName:$deployment, environment:"azure", location:$location,
      registryName:$registry, vaultName:$vault, operatorObjectId:$operator, operatorIp:$ip,
      deploymentHash:$hash, externalVaultResourceGroup:$externalVaultGroup,
      nodeVmSize:$nodeVmSize, nodeCount:$nodeCount,
      postgresSkuName:$postgresSkuName, postgresSkuTier:$postgresSkuTier,
      applicationCredentialNames:$credentialNames, registryExists:$registryExists
    } | map_values({value:.}))
  }' > "$AZURE_WORKSPACE/parameters.json"
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
    demo_status warning 'If Azure still reports a BYOIP requirement, inspect public-IP policy events and feature approval before retrying.'
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
  "$SUBSCRIPTION_SCOPE/resourceGroups/rg-$STEM-management/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-$STEM-management-radius" \
  --slurpfile account "$AZURE_WORKSPACE/account.json" '
  [.allocations[] | select(.slot == "management")] as $management |
  ($management | length) == 1 and
  ($management[0].identities.radius.id | ascii_downcase) == ($identity | ascii_downcase) and
  (.foundation.tenantId | ascii_downcase) == ($account[0].tenantId | ascii_downcase) and
  ([$management[0].identities.radius.clientId, .foundation.tenantId] |
    all(.[]; type == "string" and
      test("^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")))
' "$AZURE_WORKSPACE/foundation.json" >/dev/null
demo_status success 'Foundation completed: Azure resources are verified. Radius installation is a separate guarded phase.'
cat "$AZURE_WORKSPACE/foundation.json"
