#!/usr/bin/env bash
# Install Radius on the AKS cluster and give it a narrowly scoped Azure identity.
#
# Scope matters here. Radius gets Contributor on the application resource group
# only, never on the platform group that holds the cluster. Anyone who reaches a
# pod in the radius-system namespace can use its projected service account token
# to obtain this identity, so it must not be able to reconfigure or delete the
# cluster it runs on, nor re-enable local admin accounts.
set -euo pipefail

cd "$(dirname "$0")/.."

AKS_CONTEXT="${AKS_CONTEXT:-aks-todolist}"
AKS_NAME="${AKS_NAME:-aks-todolist}"
PLATFORM_RG="${PLATFORM_RG:-rg-todolist-platform}"
APP_RG="${APP_RG:-rg-todolist-app}"
TENANT="${TENANT:-16b3c013-d300-468d-ac64-7eda0820b6d3}"
VNET_NAME="${VNET_NAME:-vnet-todolist}"
PE_SUBNET_NAME="${PE_SUBNET_NAME:-snet-privatelink}"
DNS_ZONE="${DNS_ZONE:-privatelink.redis.azure.net}"
IDENTITY_NAME="${IDENTITY_NAME:-id-radius}"

echo "==> installing the Radius control plane into ${AKS_CONTEXT}"
# --kubecontext is not optional on a machine with many contexts. Without it rad
# installs into whatever context happens to be current.
if rad install kubernetes --kubecontext "$AKS_CONTEXT" \
     --set global.azureWorkloadIdentity.enabled=true; then
  echo "    installed"
else
  echo "    install reported an error; checking whether the pods are up anyway" >&2
fi

kubectl --context "$AKS_CONTEXT" -n radius-system rollout status deploy/ucp --timeout=300s
kubectl --context "$AKS_CONTEXT" -n radius-system get pods

echo "==> creating the azure workspace"
# Created immediately, with only a context. Every later rad command must be told
# which cluster it means; a credential registered without --workspace would be
# written to whichever workspace happens to be current, which is the kind one.
rad workspace create kubernetes azure --context "$AKS_CONTEXT" --force

echo "==> creating the managed identity"
az identity create -g "$PLATFORM_RG" -n "$IDENTITY_NAME" -o none
CLIENT_ID=$(az identity show -g "$PLATFORM_RG" -n "$IDENTITY_NAME" --query clientId -o tsv)
PRINCIPAL_ID=$(az identity show -g "$PLATFORM_RG" -n "$IDENTITY_NAME" --query principalId -o tsv)
OIDC=$(az aks show -g "$PLATFORM_RG" -n "$AKS_NAME" --query oidcIssuerProfile.issuerUrl -o tsv)
# Note the casing: the Azure CLI returns `issuerUrl`, while the ARM API and the
# Radius documentation both spell it `issuerURL`. Querying the documented name
# yields an empty string with a zero exit status, which would silently federate
# every service account against an empty issuer.

# Guard against az writing an error to stdout and this script capturing the
# error prose as a value.
for v in CLIENT_ID PRINCIPAL_ID OIDC; do
  case "${!v}" in
    ''|*ERROR*|*error*) echo "FAIL: $v looks wrong: ${!v}" >&2; exit 1;;
  esac
done
case "$OIDC" in https://*) ;; *) echo "FAIL: OIDC issuer is not a URL: $OIDC" >&2; exit 1;; esac

echo "==> federating the Radius service accounts"
# Four, not three. The Radius documentation says three and then creates four;
# dynamic-rp was added later.
for SA in applications-rp bicep-de ucp dynamic-rp; do
  az identity federated-credential create \
    --identity-name "$IDENTITY_NAME" -g "$PLATFORM_RG" \
    --name "radius-$SA" \
    --issuer "$OIDC" \
    --subject "system:serviceaccount:radius-system:$SA" \
    --audiences api://AzureADTokenExchange -o none
  echo "    federated system:serviceaccount:radius-system:$SA"
done

echo "==> granting Azure permissions (narrow by design)"
APP_RG_ID=$(az group show -n "$APP_RG" --query id -o tsv)
PE_SUBNET_ID=$(az network vnet subnet show -g "$PLATFORM_RG" \
  --vnet-name "$VNET_NAME" -n "$PE_SUBNET_NAME" --query id -o tsv)
DNS_ZONE_ID=$(az network private-dns zone show -g "$PLATFORM_RG" -n "$DNS_ZONE" --query id -o tsv)

# Contributor on the application group only, so Recipes can create Redis there.
az role assignment create --assignee-object-id "$PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal --role Contributor \
  --scope "$APP_RG_ID" -o none 2>/dev/null || echo "    Contributor already assigned"

# Network Contributor on the private endpoint subnet only, so the Recipe can
# create a private endpoint in it.
az role assignment create --assignee-object-id "$PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal --role "Network Contributor" \
  --scope "$PE_SUBNET_ID" -o none 2>/dev/null || echo "    Network Contributor already assigned"

# Private DNS Zone Contributor on that one zone only, so the Recipe can register
# the private endpoint's A record.
az role assignment create --assignee-object-id "$PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal --role "Private DNS Zone Contributor" \
  --scope "$DNS_ZONE_ID" -o none 2>/dev/null || echo "    Private DNS Zone Contributor already assigned"

echo "==> registering the Azure credential against the azure workspace"
rad credential register azure wi --workspace azure \
  --client-id "$CLIENT_ID" --tenant-id "$TENANT"

kubectl --context "$AKS_CONTEXT" -n radius-system \
  get secret azure-azurecloud-default -o jsonpath='{.metadata.name}{"\n"}'

cat <<EOF

Done. Values the next step needs:

  PE_SUBNET_ID=$PE_SUBNET_ID
  DNS_ZONE_ID=$DNS_ZONE_ID

If 'make env-azure' or 'make up-azure' fails with a message saying '' is not a
valid subscription identifier, that is the open workload-identity defect
radius-project/radius#12278, not a mistake here. Fall back to a service
principal, keeping the secret out of shell history:

  IFS=\$'\t' read -r APP_ID SP_SECRET SP_TENANT < <(
    az ad sp create-for-rbac --name sp-radius-todolist --role Contributor \\
      --scopes "\$(az group show -n $APP_RG --query id -o tsv)" \\
      --query '[appId,password,tenant]' -o tsv
  )
  rad credential register azure sp --workspace azure \\
    --client-id "\$APP_ID" --client-secret "\$SP_SECRET" --tenant-id "\$SP_TENANT"
  unset SP_SECRET

Remember a service principal is not deleted by deleting the resource groups.
EOF
