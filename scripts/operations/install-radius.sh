#!/usr/bin/env bash
set -euo pipefail
set +x
umask 077
# shellcheck source=../lib/output.sh
source "$(dirname "${BASH_SOURCE[0]}")/../lib/output.sh"

fail() { demo_status error "Radius installation failed: $*"; exit 1; }
context='' kubeconfig='' config='' client_id='' tenant_id='' workspace=''
while (($#)); do
  (($# >= 2)) || fail "Missing value for $1"
  case "$1" in
    --context) context=$2 ;;
    --kubeconfig) kubeconfig=$2 ;;
    --config) config=$2 ;;
    --client-id) client_id=$2 ;;
    --tenant-id) tenant_id=$2 ;;
    --workspace-root) workspace=$2 ;;
    *) fail "Unknown argument: $1" ;;
  esac
  shift 2
done
[[ "$context" =~ ^[a-z][a-z0-9-]{0,62}$ ]] || fail 'Invalid context'
[[ -n "$kubeconfig" && -n "$config" && -n "$workspace" ]] || fail 'Explicit access paths and workspace are required'
uuid='^[[:xdigit:]]{8}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{12}$'
[[ "$client_id" =~ $uuid && "$tenant_id" =~ $uuid ]] || fail 'Invalid identity identifiers'
[[ -d "$workspace" && ! -L "$workspace" ]] || fail 'Workspace must be a real directory'
workspace=$(cd "$workspace" && pwd -P)
[[ -f "$kubeconfig" && ! -L "$kubeconfig" && ! -L "$config" ]] || fail 'Invalid access files'
kubeconfig="$(cd "$(dirname "$kubeconfig")" && pwd -P)/$(basename "$kubeconfig")"
config="$(cd "$(dirname "$config")" && pwd -P)/$(basename "$config")"
[[ "$kubeconfig" == "$workspace/"* && "$config" == "$workspace/"* ]] || fail 'Access files must stay in the explicit workspace'
private_path() {
  local metadata mode owner
  if [[ "$(uname -s)" == Darwin ]]; then
    metadata=$(stat -f '%Lp %u' "$1")
  else
    metadata=$(stat -c '%a %u' "$1")
  fi
  read -r mode owner <<<"$metadata"
  [[ "$mode" =~ ^[0-7]{3,4}$ && "$owner" == "$(id -u)" ]] || return 1
  (( (8#$mode & 077) == 0 ))
}
if ! private_path "$workspace" || ! private_path "$kubeconfig"; then
  fail 'Access must be owner-only'
fi
if [[ -e "$config" ]]; then
  if [[ ! -f "$config" ]] || ! private_path "$config"; then
    fail 'Radius config must be a private regular file'
  fi
fi
bicep=${BICEP_BIN:-"${HOME:?}/.rad/bin/bicep"}
[[ -x "$bicep" && "$bicep" == /* ]] || fail 'The project Bicep compiler is missing'
export AZURE_CONFIG_DIR="${AZURE_CONFIG_DIR:-${HOME:?}/.azure}"
actual=$(kubectl --kubeconfig "$kubeconfig" config current-context)
[[ "$actual" == "$context" ]] || fail 'Kubeconfig context differs from the selected target'
temporary_home=$(mktemp -d "$workspace/radius-install-XXXXXXXX")
cleanup() {
  [[ -n "$temporary_home" && "$temporary_home" == "$workspace/radius-install-"* ]] ||
    fail 'Temporary HOME ownership mismatch'
  rm -rf -- "$temporary_home"
}
trap cleanup EXIT
mkdir -p "$temporary_home/.kube" "$temporary_home/.rad/bin"
ln -s "$kubeconfig" "$temporary_home/.kube/config"
ln -s "$bicep" "$temporary_home/.rad/bin/bicep"
export HOME="$temporary_home" KUBECONFIG="$kubeconfig"
rad_command() { demo_run "Radius $1" rad --config "$config" "$@" >&2; }
kube() { kubectl --kubeconfig "$kubeconfig" --context "$context" "$@"; }
kube get nodes >&2
rad_command install kubernetes --kubecontext "$context" --skip-contour-install \
  --set dashboard.enabled=false --set global.azureWorkloadIdentity.enabled=true
for account in applications-rp bicep-de ucp dynamic-rp; do
  demo_status progress "Configure and verify Radius workload identity: $account"
  kube -n radius-system annotate serviceaccount "$account" --overwrite \
    "azure.workload.identity/client-id=$client_id" \
    "azure.workload.identity/tenant-id=$tenant_id" >&2
  kube -n radius-system patch deployment "$account" --type=merge \
    -p '{"spec":{"template":{"metadata":{"labels":{"azure.workload.identity/use":"true"}}}}}' >&2
  kube -n radius-system rollout restart "deployment/$account" >&2
  kube -n radius-system rollout status "deployment/$account" --timeout=300s >&2
done
rad_command workspace create kubernetes "$context" --context "$context" --force
rad_command credential register azure wi --workspace "$context" --client-id "$client_id" \
  --tenant-id "$tenant_id"
pods=$(kube -n radius-system get pods -o json)
printf '%s' "$pods" | jq -e --arg client "$client_id" --arg tenant "$tenant_id" '
  ["applications-rp","bicep-de","dynamic-rp","ucp"] as $expected |
  [.items[] | select(.metadata.deletionTimestamp == null) |
    select(any(.spec.containers[]?;
      ((.env // [] | map({key: .name, value: .value}) | from_entries) |
        .AZURE_CLIENT_ID == $client and .AZURE_TENANT_ID == $tenant and
        (.AZURE_FEDERATED_TOKEN_FILE | type == "string" and length > 0)))) |
    .spec.serviceAccountName | select(. as $account | $expected | index($account))] |
  unique == $expected
' >/dev/null || fail 'Workload identity projection did not match all four Radius accounts'
chmod 600 "$config" "$kubeconfig"
jq -n --arg context "$context" '{context:$context,workload_identity_verified:true}'
