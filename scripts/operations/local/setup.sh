#!/usr/bin/env bash
# Normal management setup. Inspect emits freshly discovered LocalConfig inputs for the factory.
# shellcheck disable=SC2016
set +x
set -euo pipefail
umask 077
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
# shellcheck source=../../lib/env.sh
source "$ROOT/scripts/lib/env.sh"
case "${1:-apply}" in
  apply|inspect) mode=${1:-apply} ;;
  --help) printf 'Usage: bash scripts/operations/local/setup.sh [apply|inspect]\n'; exit 0 ;;
  *) demo_error 'Expected apply or inspect'; exit 1 ;;
esac
(( $# <= 1 )) || { demo_error 'Unexpected setup arguments'; exit 1; }
demo_load_env "$ROOT/.env"
[[ "$DEMO_ENV" == local ]] || { demo_error 'Local setup requires DEMO_ENV=local'; exit 1; }
unset DEMO_KEY_MANAGEMENT DEMO_KEY_SHARED_CONTROL DEMO_KEY_SHARED_DATA \
  DEMO_KEY_ISOLATED_1_CONTROL DEMO_KEY_ISOLATED_1_DATA
prefix="$DEMO_PROJECT-$DEMO_DEPLOYMENT-local"
cluster="$prefix-management"
context="kind-$cluster"
access_namespace="$prefix-access"
work=$(mktemp -d "${TMPDIR:-/tmp}/plane-local-setup.XXXXXX")
container=''
cleanup() {
  local status=$?
  if [[ -n "$container" ]]; then docker_cli rm "$container" >/dev/null || status=1; fi
  case "$work" in */plane-local-setup.*) rm -rf -- "$work" ;; *) status=1 ;; esac
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
bash "$ROOT/scripts/operations/local/bootstrap.sh" inspect >"$work/bootstrap.json"
jq -e --arg cluster "$cluster" --arg group "$prefix" \
  '.cluster == $cluster and .radiusGroup == $group and .observedExisting == true' \
  "$work/bootstrap.json" >/dev/null
jq '.artifacts' "$work/bootstrap.json" >"$work/images.json"
host=$(env -i "HOME=$HOME" "PATH=$PATH" docker context inspect desktop-linux \
  --format '{{json .Endpoints.docker.Host}}' | jq -er .)
[[ "$host" =~ ^unix:///[-a-zA-Z0-9_./]+$ && "$host" != */../* ]] || exit 1
mkdir -m 700 "$work/home" "$work/prepared"
mkdir -p "$work/home/.kube"
kubeconfig="$work/home/.kube/config"
environment=(env -i "PATH=$PATH" "HOME=$work/home" "DOCKER_HOST=$host" \
  "KIND_EXPERIMENTAL_PROVIDER=docker" "KUBECONFIG=$kubeconfig" "LC_ALL=C" \
  "UV_PYTHON_DOWNLOADS=never" "UV_OFFLINE=1")
docker_cli() { "${environment[@]}" docker --host "$host" "$@"; }
kube() { "${environment[@]}" kubectl --kubeconfig "$kubeconfig" --context "$context" \
  --request-timeout=30s "$@"; }
assets() { "${environment[@]}" uv run --no-sync --project "$ROOT" python \
  "$ROOT/scripts/operations/local/assets.py" "$@"; }
radius() { "${environment[@]}" rad --config "$work/radius.yaml" "$@"; }
"${environment[@]}" kind get kubeconfig --name "$cluster" >"$kubeconfig"
chmod 600 "$kubeconfig"
kube config view --minify -o json | jq -e --arg context "$context" '
  .["current-context"]==$context and (.contexts|length)==1 and
  (.clusters|length)==1 and (.users|length)==1 and
  .contexts[0].context.cluster==.clusters[0].name and
  .contexts[0].context.user==.users[0].name and
  (.clusters[0].cluster|keys)==["certificate-authority-data","server"] and
  .clusters[0].cluster.server=="https://127.0.0.1:35495" and
  (.users[0].user|keys)==["client-certificate-data","client-key-data"]
' >/dev/null || { demo_error 'Fresh management access differs from the selected local profile'; exit 1; }
kube config view --minify --raw -o json | jq -er \
  '.clusters[0].cluster["certificate-authority-data"]' >"$work/ca"
kube get namespace kube-system -o json >"$work/namespace.json"
kube get node "$cluster-control-plane" -o json >"$work/node.json"
kube -n default get service kubernetes -o json >"$work/service.json"
address=$(jq -er '[.status.addresses[] | select(.type=="InternalIP") | .address] |
  select(length == 1) | .[0]' "$work/node.json")
operator=$(jq -er '.images.operator.id' "$work/images.json")
container=$(docker_cli create --network none --entrypoint /bin/true "$operator")
[[ "$container" =~ ^[a-f0-9]{64}$ ]] || { demo_error 'Invalid prepared artifact container'; exit 1; }
docker_cli cp "$container:/opt/radplanes/bootstrap/." "$work/prepared"
docker_cli rm "$container" >/dev/null
container=''
assets setup-plan "$work/prepared" "$work/images.json" "$prefix" "$prefix" \
  "$access_namespace" "$address" >"$work/plan.json"
assets live-config "$DEMO_PROJECT" "$DEMO_DEPLOYMENT" "$work" >"$work/config.json"

jq -n --arg name "$access_namespace" --arg prefix "$prefix" \
  '{apiVersion:"v1",kind:"Namespace",metadata:{name:$name,labels:{
    "plane-demo/resource-prefix":$prefix,"plane-demo/radius-group":$prefix}}}' >"$work/access.json"
kube get namespace "$access_namespace" --ignore-not-found -o json >"$work/current.json"
if [[ -s "$work/current.json" ]]; then
  assets owned-object "$work/access.json" "$work/current.json" >&2
else
  [[ "$mode" == apply ]] || { demo_error 'Run canonical local setup to create access ownership'; exit 1; }
  kube create -f "$work/access.json" >&2
fi

for module in cluster postgresql redis gateway; do
  name=$(jq -er --arg module "$module" '.recipes[$module].moduleServer' "$work/plan.json")
  count=0
  for kind in ConfigMap Deployment Service; do
    jq --arg name "$name" --arg kind "$kind" \
      '.objects[] | select(.metadata.name==$name and .kind==$kind)' \
      "$work/plan.json" >"$work/expected-$kind.json"
    kube -n radius-system get "$kind" "$name" --ignore-not-found -o json >"$work/actual-$kind.json"
    if [[ -s "$work/actual-$kind.json" ]]; then
      assets owned-object "$work/expected-$kind.json" "$work/actual-$kind.json" >&2
      count=$((count + 1))
    fi
  done
  if ((count != 3)); then
    ((count == 0)) && [[ "$mode" == apply ]] || {
      demo_error 'Recipe publication is partial or missing; inspect the actual owners'; exit 1;
    }
    jq -n --slurpfile cm "$work/expected-ConfigMap.json" \
      --slurpfile deployment "$work/expected-Deployment.json" --slurpfile service "$work/expected-Service.json" \
      '{apiVersion:"v1",kind:"List",items:[$cm[0],$deployment[0],$service[0]]}' |
      kube create -f - >&2
  fi
  kube -n radius-system rollout status "deployment/$name" --timeout=180s >&2
done

kube -n radius-system get deployment applications-rp -o json >"$work/rp.json"
jq -e '.spec.template.spec | (.containers|length)==1 and .containers[0].name=="applications-rp" and
  all(.volumes[]?; .hostPath == null)' "$work/rp.json" >/dev/null
jq '.terraform' "$work/plan.json" >"$work/terraform.json"
kube -n radius-system get configmap applications-rp-config -o json >"$work/tf-config.json"
assets terraform-settings "$work/tf-config.json" >"$work/tf-settings.json"
if [[ "$mode" == apply ]]; then
  kube -n radius-system patch configmap applications-rp-config --type=merge \
    --patch-file "$work/tf-settings.json" >&2
  kube -n radius-system patch deployment applications-rp --type=strategic \
    --patch-file "$work/terraform.json" >&2
else
  assets owned-object "$work/terraform.json" "$work/rp.json" --patch >&2
  assets owned-object "$work/tf-settings.json" "$work/tf-config.json" --patch >&2
fi
kube -n radius-system rollout status deployment/applications-rp --timeout=300s >&2
scope="/planes/radius/local/resourceGroups/$prefix/providers/Applications.Core/environments/management"
kube get --raw "/apis/api.ucp.dev/v1alpha3$scope?api-version=2023-10-01-preview" >"$work/environment.json"
jq -e --arg id "$scope" --arg namespace "$prefix-management" '
  (.id|ascii_downcase)==($id|ascii_downcase) and .properties.compute.namespace==$namespace
' "$work/environment.json" >/dev/null
jq '.environment' "$work/plan.json" >"$work/expected-environment.json"
if ! jq -e --slurpfile expected "$work/expected-environment.json" '
  (.properties.recipes // {}) == {} or .properties.recipes == $expected[0].properties.recipes
' "$work/environment.json" >/dev/null; then
  demo_error 'Existing management Recipe bindings differ; inspect their owner before changing them'
  exit 1
fi
if [[ "$mode" == apply ]]; then
  radius workspace create kubernetes "$cluster" --context "$context" --group "$prefix" \
    --environment management >&2
  radius resource create Applications.Core/environments management \
    --from-file "$work/expected-environment.json" --workspace "$cluster" >&2
fi
kube get --raw "/apis/api.ucp.dev/v1alpha3$scope?api-version=2023-10-01-preview" >"$work/environment.json"
assets owned-object "$work/expected-environment.json" "$work/environment.json" >&2
kube get namespace kube-system -o json >"$work/recheck.json"
jq -e --slurpfile before "$work/namespace.json" \
  '.metadata.uid == $before[0].metadata.uid' "$work/recheck.json" >/dev/null
cat "$work/config.json"
