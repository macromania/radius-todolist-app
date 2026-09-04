#!/usr/bin/env bash
# Create the Azure Radius environment.
#
# The bootstrap order is not arbitrary. `rad workspace create` validates that
# the Radius resource group and the environment already exist, so the workspace
# is created twice: once with only a context, and again once both exist.
set -euo pipefail

cd "$(dirname "$0")/.."

AKS_CONTEXT="${AKS_CONTEXT:-aks-todolist}"
PLATFORM_RG="${PLATFORM_RG:-rg-todolist-platform}"
APP_RG="${APP_RG:-rg-todolist-app}"
RAD_GROUP="${RAD_GROUP:-todolist}"
SUBSCRIPTION="${SUBSCRIPTION:-a3ed6c04-563f-4855-ac84-bdf1e5fbc3fc}"
VNET_NAME="${VNET_NAME:-vnet-todolist}"
PE_SUBNET_NAME="${PE_SUBNET_NAME:-snet-privatelink}"
DNS_ZONE="${DNS_ZONE:-privatelink.redis.azure.net}"

RECIPE_REF="${RECIPE_REF:?set RECIPE_REF to the Recipe reference}"
RECIPE_EXPECTED_DIGEST="${RECIPE_EXPECTED_DIGEST:?set RECIPE_EXPECTED_DIGEST to the digest the tag must resolve to}"
ACR_NAME="${ACR_NAME:-acrtodolistjts7g6kk6ua66}"

# Radius v0.60 refuses a digest reference outright:
#
#   RecipeDownloadFailed: invalid path "...@sha256:..." does not include a tag;
#   a tagged reference such as repository:tag is required (digest references
#   are not supported)
#
# which is a little galling, because `rad bicep publish` prints the digest URL
# and calls it "the Recipe url to immutably pin the artifact". So immutability
# has to be enforced elsewhere. Two things do it:
#
#   1. The tag is locked in the registry (writeEnabled=false, deleteEnabled=false)
#      so it cannot be repointed or deleted.
#   2. The check below resolves the tag and refuses to deploy unless it still
#      points at the digest this repository expects.
#
# Together these give what digest pinning would have: the Radius identity cannot
# be made to execute a template nobody reviewed.
case "$RECIPE_REF" in
  *@sha256:*) echo "FAIL: Radius 0.60 rejects digest references; use the tag" >&2; exit 1;;
  *:*) ;;
  *) echo "FAIL: RECIPE_REF must carry a tag, got: $RECIPE_REF" >&2; exit 1;;
esac

RECIPE_IMAGE="${RECIPE_REF#*.azurecr.io/}"
echo "==> verifying the Recipe tag still points at the reviewed digest"
ACTUAL_DIGEST=$(az acr manifest show-metadata -r "$ACR_NAME" -n "$RECIPE_IMAGE" --query digest -o tsv 2>/dev/null)
if [ "$ACTUAL_DIGEST" != "$RECIPE_EXPECTED_DIGEST" ]; then
  echo "FAIL: Recipe tag $RECIPE_REF resolves to $ACTUAL_DIGEST" >&2
  echo "      but this repository expects  $RECIPE_EXPECTED_DIGEST" >&2
  echo "      Refusing to deploy: the tag was repointed, or the Recipe was" >&2
  echo "      republished without updating RECIPE_EXPECTED_DIGEST." >&2
  exit 1
fi
echo "    $ACTUAL_DIGEST (matches)"

PE_SUBNET_ID=$(az network vnet subnet show -g "$PLATFORM_RG" \
  --vnet-name "$VNET_NAME" -n "$PE_SUBNET_NAME" --query id -o tsv)
DNS_ZONE_ID=$(az network private-dns zone show -g "$PLATFORM_RG" -n "$DNS_ZONE" --query id -o tsv)

for v in PE_SUBNET_ID DNS_ZONE_ID; do
  case "${!v}" in
    /subscriptions/*) ;;
    *) echo "FAIL: $v is not a resource ID: ${!v}" >&2; exit 1;;
  esac
done

echo "==> bootstrapping the azure workspace and Radius resource group"
rad workspace create kubernetes azure --context "$AKS_CONTEXT" --force
rad group show "$RAD_GROUP" --workspace azure >/dev/null 2>&1 || \
  rad group create "$RAD_GROUP" --workspace azure

echo "==> deploying the azure environment"
rad deploy environments/azure.bicep --workspace azure --group "$RAD_GROUP" \
  -p azureSubscriptionId="$SUBSCRIPTION" \
  -p azureResourceGroup="$APP_RG" \
  -p redisRecipeRef="$RECIPE_REF" \
  -p privateEndpointSubnetId="$PE_SUBNET_ID" \
  -p privateDnsZoneId="$DNS_ZONE_ID"

echo "==> binding the workspace to the environment"
rad workspace create kubernetes azure --context "$AKS_CONTEXT" \
  --group "$RAD_GROUP" --environment azure --force

rad recipe list --workspace azure
