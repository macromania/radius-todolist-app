#!/usr/bin/env bash
# Only management is created here. Child creation and application/database setup stay with Radius.
# A completed namespace owner is checked against live Radius, images, and the current node on rerun.
# shellcheck disable=SC2016
set +x
set -euo pipefail
umask 077
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
# shellcheck source=../../lib/env.sh
source "$ROOT/scripts/lib/env.sh"
if [[ "${1:-}" == --help ]]; then
  printf 'Usage: bash scripts/operations/local/bootstrap.sh [inspect]\n'
  exit 0
fi
mode=${1:-apply}
if [[ "$mode" != apply && "$mode" != inspect ]] || (( $# > 1 )); then
  demo_error 'Unexpected bootstrap arguments'; exit 1;
fi
for tool in docker kind kubectl jq rad uv; do
  command -v "$tool" >/dev/null || { demo_error "Required tool missing: $tool"; exit 1; }
done
demo_load_env "$ROOT/.env"
[[ "$DEMO_ENV" == local ]] || { demo_error 'This entrypoint is local only'; exit 1; }
unset DEMO_KEY_MANAGEMENT DEMO_KEY_SHARED_CONTROL DEMO_KEY_SHARED_DATA \
  DEMO_KEY_ISOLATED_1_CONTROL DEMO_KEY_ISOLATED_1_DATA
stem="$DEMO_PROJECT-$DEMO_DEPLOYMENT-local"
cluster="$stem-management"
node="$cluster-control-plane"
context="kind-$cluster"
namespace="$cluster-management"
host=$(env -i "PATH=$PATH" "HOME=$HOME" "LC_ALL=C" \
  docker context inspect desktop-linux --format '{{json .Endpoints.docker.Host}}' | jq -er .)
[[ "$host" =~ ^unix:///[-a-zA-Z0-9_./]+$ && "$host" != */../* ]] || {
  demo_error 'Docker Desktop must use its local Unix socket'; exit 1;
}
work=$(mktemp -d "${TMPDIR:-/tmp}/plane-local-bootstrap.XXXXXX")
copy_container=''
cleanup() {
  local status=$?
  if [[ -n "$copy_container" ]]; then docker_cli rm "$copy_container" >/dev/null || status=1; fi
  case "$work" in */plane-local-bootstrap.*) rm -rf -- "$work" ;; *) status=1 ;; esac
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -m 700 "$work/home" "$work/bootstrap"
mkdir -p "$work/home/.kube"
kubeconfig="$work/home/.kube/config"
environment=(env -i "PATH=$PATH" "HOME=$work/home" "DOCKER_HOST=$host" \
  "KIND_EXPERIMENTAL_PROVIDER=docker" "KUBECONFIG=$kubeconfig" "LC_ALL=C" \
  "UV_PYTHON_DOWNLOADS=never" "UV_OFFLINE=1")
docker_cli() { "${environment[@]}" docker --host "$host" "$@"; }
kind_cli() { "${environment[@]}" kind "$@"; }
kube() { "${environment[@]}" kubectl --kubeconfig "$kubeconfig" --context "$context" \
  --request-timeout=30s "$@"; }
radius() { "${environment[@]}" rad --config "$work/radius.yaml" "$@"; }
assets() { "${environment[@]}" uv run --no-sync --project "$ROOT" python \
  "$ROOT/scripts/operations/local/assets.py" "$@"; }

# This re-exports and checks image contents now; there is no saved image-review authority.
bash "$ROOT/scripts/operations/local/build.sh" inspect >"$work/images.json"
jq -e --arg stem "$stem" '.stem == $stem and (.images|length) == 4' \
  "$work/images.json" >/dev/null || { demo_error 'Inspected images belong to another deployment'; exit 1; }
executor=$(jq -er '.images.executor.reference' "$work/images.json")
operator_id=$(jq -er '.images.operator.id' "$work/images.json")
radius_hash=$(jq -er '.radiusBinarySHA256' "$work/images.json")
revision=$(jq -er '.revision' "$work/images.json")
kind_cli version | grep -q 'kind v0.31.0' || { demo_error 'kind 0.31.0 is required'; exit 1; }
radius version --cli | grep -q '0.60.2' || { demo_error 'Radius 0.60.2 is required'; exit 1; }
grep -qx 'PORT_BLOCK_START=35490' "$ROOT/ports.env"
grep -qx 'PORT_BLOCK_END=35499' "$ROOT/ports.env"
node_image='kindest/node:v1.35.0@sha256:452d707d4862f52530247495d180205e029056831160e22870e37e3f6c1ac31f'
node_ids=$(docker_cli ps -aq --no-trunc --filter "label=io.x-k8s.kind.cluster=$cluster")
existing=false
if [[ -n "$node_ids" ]]; then
  [[ "$node_ids" =~ ^[a-f0-9]{64}$ ]] || { demo_error 'Expected one management node'; exit 1; }
  existing=true
else
  [[ "$mode" != inspect ]] || { demo_error 'Run the canonical local bootstrap first'; exit 1; }
  [[ -z "$(docker_cli ps -aq --no-trunc --filter "name=^/$node$")" ]] || {
    demo_error 'A foreign container occupies the management name'; exit 1;
  }
  jq -n --arg name "$cluster" --arg image "$node_image" --arg project "$DEMO_PROJECT" \
    --arg deployment "$DEMO_DEPLOYMENT" '
    {kind:"Cluster",apiVersion:"kind.x-k8s.io/v1alpha4",name:$name,
      networking:{apiServerAddress:"127.0.0.1",apiServerPort:35495},
      nodes:[{role:"control-plane",image:$image,
        labels:{"radplanes.local/slot":"management","plane-demo/project":$project,
          "plane-demo/deployment":$deployment,"plane-demo/environment":"local"},
        extraMounts:[{hostPath:"/var/run/docker.sock",containerPath:"/run/radplanes/docker.sock",
          readOnly:false}],
        extraPortMappings:[{containerPort:31480,hostPort:35490,
          listenAddress:"127.0.0.1",protocol:"TCP"}]}]}
  ' >"$work/kind.json"
  kind_cli create cluster --name "$cluster" --config "$work/kind.json" \
    --kubeconfig "$kubeconfig" --image "$node_image" --wait 300s
fi
kind_cli get kubeconfig --name "$cluster" >"$kubeconfig"
chmod 600 "$kubeconfig"
kube config view --minify -o json | jq -e --arg context "$context" '
  .["current-context"] == $context and (.clusters|length) == 1 and (.users|length) == 1 and
  (.contexts|length) == 1 and .contexts[0].name == $context and
  .contexts[0].context.cluster == .clusters[0].name and
  .contexts[0].context.user == .users[0].name and
  (.clusters[0].cluster|keys) == ["certificate-authority-data","server"] and
  .clusters[0].cluster.server == "https://127.0.0.1:35495" and
  (.clusters[0].cluster["certificate-authority-data"]|type == "string" and length > 0) and
  (.users[0].user|keys) == ["client-certificate-data","client-key-data"] and
  (.users[0].user|all(.[];type == "string" and length > 0))
' >/dev/null || { demo_error 'Management access is not an embedded, verified local profile'; exit 1; }
node_info=$(docker_cli inspect --type container "$node")
node_id=$(jq -er --arg name "$node" --arg cluster "$cluster" --arg image "$node_image" '
  select(length == 1) | .[0] |
  select(.Name == ("/" + $name) and .Config.Image == $image and .State.Running == true and
    .Config.Labels["io.x-k8s.kind.cluster"] == $cluster) |
  .Id | select(test("^[a-f0-9]{64}$"))
' <<<"$node_info")
kube get node "$node" -o json | jq -e --arg project "$DEMO_PROJECT" --arg name "$node" \
  --arg deployment "$DEMO_DEPLOYMENT" '
  .metadata.name == $name and .metadata.labels["plane-demo/project"] == $project and
  .metadata.labels["plane-demo/deployment"] == $deployment and
  .metadata.labels["plane-demo/environment"] == "local" and
  .metadata.labels["radplanes.local/slot"] == "management"
' >/dev/null || { demo_error 'Management node has another deployment owner'; exit 1; }
if [[ "$existing" == true ]]; then
  kube get namespace "$namespace" -o json | jq -e --arg node "$node_id" \
    --arg revision "$revision" --arg project "$DEMO_PROJECT" --arg deployment "$DEMO_DEPLOYMENT" \
    --arg namespace "$namespace" '
    .metadata.name == $namespace and .metadata.deletionTimestamp == null and
    .metadata.labels["plane-demo/project"] == $project and
    .metadata.labels["plane-demo/deployment"] == $deployment and
    .metadata.labels["plane-demo/environment"] == "local" and
    .metadata.annotations["plane-demo/bootstrap-node"] == $node and
    .metadata.annotations["plane-demo/source-revision"] == $revision
  ' >/dev/null || {
    demo_error 'Existing management is foreign, partial, or another revision; inspect it explicitly'
    exit 1
  }
fi
encryption=$("${environment[@]}" bash "$ROOT/scripts/operations/local/encryption.sh" \
  --cluster "$cluster" --node "$node" --kubeconfig "$kubeconfig" --context "$context" \
  --docker-host "$host")
jq -e --arg id "$node_id" --arg cluster "$cluster" '
  .nodeId == $id and .cluster == $cluster and .syntheticCiphertextVerified == true
' <<<"$encryption" >/dev/null || { demo_error 'Management encryption proof differs'; exit 1; }

if [[ "$existing" == false ]]; then
  copy_container=$(docker_cli create --network none --entrypoint /bin/true "$operator_id")
  [[ "$copy_container" =~ ^[a-f0-9]{64}$ ]] || { demo_error 'Invalid artifact container'; exit 1; }
  docker_cli cp "$copy_container:/opt/radplanes/bootstrap/." "$work/bootstrap"
  docker_cli rm "$copy_container" >/dev/null
  copy_container=''
  jq -er '[.images[].reference] + [.dependencies[].reference] | unique | .[]' \
    "$work/images.json" >"$work/load-images"
  while IFS= read -r image; do
    kind_cli load docker-image --name "$cluster" "$image"
  done <"$work/load-images"
  radius install kubernetes --chart "$work/bootstrap/radius.tgz" --kubecontext "$context" \
    --skip-contour-install --set "dynamicrp.image=$executor" --set dashboard.enabled=false \
    --set global.terraform.enabled=false --set dynamicrp.buildkit.enabled=false \
    --set global.terraform.loglevel=OFF
  socket=$(docker_cli exec "$node" stat -c '%u %g %a' /run/radplanes/docker.sock)
  [[ "$socket" =~ ^[0-9]+\ [0-9]+\ [0-7]{3,4}$ ]] || {
    demo_error 'Unexpected management socket ownership'; exit 1;
  }
  read -r uid gid mode <<<"$socket"
  permissions=$((8#$mode))
  (( (uid == 65532 && (permissions & 128)) || (permissions & 18) )) || {
    demo_error 'The management socket is not writable by the reviewed group'; exit 1;
  }
  assets overlay "$ROOT" "$executor" "$gid" >"$work/executor.json"
  kube -n radius-system patch deployment dynamic-rp --type=strategic \
    --patch-file "$work/executor.json"
  jq -n --arg namespace "$namespace" --arg project "$DEMO_PROJECT" \
    --arg deployment "$DEMO_DEPLOYMENT" '
    {apiVersion:"v1",kind:"Namespace",metadata:{name:$namespace,labels:{
      "plane-demo/project":$project,"plane-demo/deployment":$deployment,
      "plane-demo/environment":"local"}}}
  ' | kube create -f -
  radius workspace create kubernetes "$cluster" --context "$context"
  radius group create "$stem" --workspace "$cluster"
  radius environment create management --group "$stem" --kubernetes-namespace "$cluster" \
    --workspace "$cluster"
  for type in clusters postgresql gateways; do
    radius resource-type create --from-file "$ROOT/infra/radius/types/$type.yaml" \
      --workspace "$cluster"
  done
fi
kube -n radius-system rollout status deployment/dynamic-rp --timeout=300s >&2
kube -n radius-system get deployment dynamic-rp -o json | jq -e --arg image "$executor" '
  .spec.template.metadata.labels["radplanes.local/executor"] == "management-only" and
  (.spec.template.spec.containers|length) == 1 and
  .spec.template.spec.containers[0].image == $image and
  .spec.template.spec.containers[0].command == null and
  .spec.template.spec.hostNetwork != true and
  .spec.template.spec.securityContext.fsGroupChangePolicy == "OnRootMismatch"
' >/dev/null || { demo_error 'The running executor contract differs'; exit 1; }
daemon=$(docker_cli info --format '{{.ID}}')
observed=$(kube -n radius-system exec deployment/dynamic-rp -c dynamic-rp -- sh -ec \
  'test "$(id -u)" = 65532; test -w /terraform; test -S /run/radplanes/docker.sock;
   docker --host unix:///run/radplanes/docker.sock info --format "{{.ID}}"')
[[ -n "$daemon" && "$observed" == "$daemon" ]] || {
  demo_error 'The executor does not address the selected Docker Desktop daemon'; exit 1;
}
binary=$(kube -n radius-system exec deployment/dynamic-rp -c dynamic-rp -- sha256sum /dynamic-rp)
[[ "${binary%% *}" == "$radius_hash" ]] || { demo_error 'The running Radius binary differs'; exit 1; }
scope="/planes/radius/local/resourceGroups/$stem/providers/Applications.Core/environments/management"
kube get --raw "/apis/api.ucp.dev/v1alpha3$scope?api-version=2023-10-01-preview" |
  jq -e --arg id "$scope" --arg namespace "$cluster" '
    (.id|ascii_downcase) == ($id|ascii_downcase) and
    .properties.compute.kind == "kubernetes" and .properties.compute.resourceId == "self" and
    .properties.compute.namespace == $namespace
  ' >/dev/null || { demo_error 'The live management Radius environment differs'; exit 1; }
if [[ "$existing" == false ]]; then
  kube annotate namespace "$namespace" "plane-demo/bootstrap-node=$node_id" \
    "plane-demo/source-revision=$revision"
fi
jq -n --arg cluster "$cluster" --arg namespace "$namespace" --arg group "$stem" \
  --arg id "$node_id" --arg revision "$revision" --argjson observed "$existing" \
  --slurpfile artifacts "$work/images.json" \
  '{cluster:$cluster,namespace:$namespace,environmentNamespace:$cluster,radiusGroup:$group,nodeId:$id,
    revision:$revision,observedExisting:$observed,stage:"management-radius",
    applicationsDeployed:false,coldChildReady:false,artifacts:$artifacts[0]}'
