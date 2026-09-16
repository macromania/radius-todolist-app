#!/usr/bin/env bash
# Build prepares public dependencies. Inspect is offline and rechecks actual image bytes.
# Neither mode publishes images or reads deployment records.
# shellcheck disable=SC2016
set +x
set -euo pipefail
umask 077
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
# shellcheck source=../../lib/env.sh
source "$ROOT/scripts/lib/env.sh"
case "${1:-build}" in
  build|inspect) stage=${1:-build} ;;
  --help) printf 'Usage: bash scripts/operations/local/build.sh [build|inspect]\n'; exit 0 ;;
  *) demo_error 'Expected build or inspect'; exit 1 ;;
esac
(( $# <= 1 )) || { demo_error 'Unexpected build arguments'; exit 1; }
demo_status section "Local images: $stage and verify"
for tool in docker jq git tar uv rad helm; do
  command -v "$tool" >/dev/null || { demo_error "Required tool missing: $tool"; exit 1; }
done
demo_load_env "$ROOT/.env"
[[ "$DEMO_ENV" == local ]] || { demo_error 'This entrypoint is local only'; exit 1; }
unset DEMO_KEY_MANAGEMENT DEMO_KEY_SHARED_CONTROL DEMO_KEY_SHARED_DATA \
  DEMO_KEY_ISOLATED_1_CONTROL DEMO_KEY_ISOLATED_1_DATA
stem="$DEMO_PROJECT-$DEMO_DEPLOYMENT-local"
revision=$(git -C "$ROOT" rev-parse HEAD)
[[ "$revision" =~ ^[a-f0-9]{40}$ && "${DEMO_REVISION:-$revision}" == "$revision" ]] || {
  demo_error 'Check out the selected full source revision before building'; exit 1;
}
dirty=$(git -C "$ROOT" status --porcelain --untracked-files=all -- \
  src sql images scripts infra pyproject.toml uv.lock .dockerignore)
[[ -z "$dirty" ]] || { demo_error 'Commit all image inputs first'; exit 1; }
[[ -z "$(git -C "$ROOT" ls-files -- .env)" ]] || { demo_error '.env must not be tracked'; exit 1; }
host=$(env -i "PATH=$PATH" "HOME=$HOME" "LC_ALL=C" \
  docker context inspect desktop-linux --format '{{json .Endpoints.docker.Host}}' | jq -er .)
[[ "$host" =~ ^unix:///[-a-zA-Z0-9_./]+$ && "$host" != */../* ]] || {
  demo_error 'Docker Desktop must use its local Unix socket'; exit 1;
}
work=$(mktemp -d "${TMPDIR:-/tmp}/plane-local-build.XXXXXX")
host_bicep="${HOME:?}/.rad/bin/bicep"
inspection_container=''
cleanup() {
  local status=$?
  if [[ -n "$inspection_container" ]]; then
    docker_cli rm "$inspection_container" >/dev/null || status=1
  fi
  case "$work" in */plane-local-build.*) rm -rf -- "$work" ;; *) status=1 ;; esac
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -m 700 "$work/home" "$work/source" "$work/proofs"
if [[ ! -x "$host_bicep" ]] || ! "$host_bicep" --version | grep -q '0.42.1'; then
  demo_error 'Prepare the Radius-bundled Bicep 0.42.1 before building or inspecting'; exit 1;
fi
mkdir -p "$work/home/.rad/bin"
cp "$host_bicep" "$work/home/.rad/bin/bicep"
environment=(env -i "PATH=$PATH" "HOME=$work/home" "DOCKER_HOST=$host" \
  "LC_ALL=C" "UV_PYTHON_DOWNLOADS=never" "UV_OFFLINE=1")
docker_cli() { demo_run "Docker $1" "${environment[@]}" docker --host "$host" "$@"; }
assets() {
  "${environment[@]}" uv run --no-sync --project "$ROOT" python \
    "$ROOT/scripts/operations/local/assets.py" "$@"
}
radius() { "${environment[@]}" rad --config "$work/radius.yaml" "$@"; }
helm_cli() { "${environment[@]}" helm "$@"; }
info=$(docker_cli info --format '{{json .}}')
arch=$(jq -er '
  select(.OperatingSystem == "Docker Desktop" and .OSType == "linux") |
  if .Architecture == "aarch64" or .Architecture == "arm64" then "arm64"
  elif .Architecture == "x86_64" or .Architecture == "amd64" then "amd64"
  else error("unsupported Docker architecture") end
' <<<"$info")
radius version --cli | grep -q '0.60.2' || { demo_error 'Radius 0.60.2 is required'; exit 1; }
# Git modes belong to public source; the enclosing workspace and client profiles stay private.
git -C "$ROOT" archive "$revision" | tar -xp --no-same-owner -C "$work/source"
for type in clusters postgresql gateways; do
  radius bicep publish-extension --from-file "$work/source/infra/radius/types/$type.yaml" \
    --target "$work/source/infra/radius/types/$type.tgz" --force >&2
  chmod 0644 "$work/source/infra/radius/types/$type.tgz"
done
api="localhost/$stem-api:$revision"
provisioner="localhost/$stem-provisioner:$revision"
executor="localhost/$stem-executor:$revision"
operator="localhost/$stem-operator:$revision"
tools_base="localhost/$stem-tools-base:$revision"
executor_base="localhost/$stem-executor-base:$revision"
operator_base="localhost/$stem-operator-base:$revision"
provisioner_base="localhost/$stem-provisioner-base:$revision"
radius_base='ghcr.io/radius-project/dynamic-rp:0.60@sha256:225dcb42382cc8f83fa1eda22e2a193fce9a76428af39cb67e9033172ecd4783'
node_image='kindest/node:v1.35.0@sha256:452d707d4862f52530247495d180205e029056831160e22870e37e3f6c1ac31f'
python_image='docker.io/library/python:3.13.12-alpine3.23@sha256:bb1f2fdb1065c85468775c9d680dcd344f6442a2d1181ef7916b60a623f11d40'
postgres_image='docker.io/library/postgres:17.8-alpine3.23@sha256:3430fe182f5065a6ea505c3d432d2c7fff18fbab954df8f277c1dbf4c70124af'
redis_image='docker.io/library/redis:7.4-alpine@sha256:ff02b58f971e7d7d156a1267e283fcbbeee91773b6aa36c49dac28ecfe28eadf'
envoy_image='docker.io/envoyproxy/envoy:v1.37.1@sha256:29496a88fba9c4c9cdef4afe8fec70f536c5ba111b1c2bddbc5436b091ceca33'

owned_image() {
  docker_cli image inspect "$1" | jq -er --arg revision "$revision" --arg arch "$arch" \
    --arg project "$DEMO_PROJECT" --arg deployment "$DEMO_DEPLOYMENT" '
    select(length == 1) | .[0] |
    select(.Os == "linux" and .Architecture == $arch and
      .Config.Labels["org.opencontainers.image.revision"] == $revision and
      .Config.Labels["plane-demo/project"] == $project and
      .Config.Labels["plane-demo/deployment"] == $deployment) |
    .Id | select(test("^sha256:[a-f0-9]{64}$"))
  '
}
build_image() {
  local reference=$1 file=$2
  shift 2
  if [[ -n "$(docker_cli image ls --quiet "$reference")" ]]; then
    owned_image "$reference" >/dev/null || { demo_error 'An image tag has another owner'; exit 1; }
  else
    docker_cli build --progress=plain --platform "linux/$arch" --tag "$reference" \
      --label "org.opencontainers.image.revision=$revision" \
      --label "plane-demo/project=$DEMO_PROJECT" --label "plane-demo/deployment=$DEMO_DEPLOYMENT" \
      --build-arg "SOURCE_REVISION=$revision" --build-arg "TARGETARCH=$arch" \
      --file "$work/source/$file" "$@" "$work/source"
  fi
}
if [[ "$stage" == build ]]; then
  docker_cli pull --platform "linux/$arch" "$radius_base"
  build_image "$api" images/api/Dockerfile
  build_image "$provisioner_base" images/local-provisioner/Dockerfile --build-arg "API_IMAGE=$api"
  for target in tools executor operator; do
    case "$target" in tools) ref=$tools_base ;; executor) ref=$executor_base ;; operator) ref=$operator_base ;; esac
    build_image "$ref" images/radius-kind/Dockerfile --target "$target"
  done
  packaged="$work/source/scripts/operations/local/.packaged"
  mkdir -m 700 "$packaged" "$work/chart"
  helm_cli pull oci://ghcr.io/radius-project/helm-chart --version 0.60.2 \
    --destination "$work/chart"
  charts=("$work/chart/"*.tgz)
  (( ${#charts[@]} == 1 )) && [[ -f "${charts[0]}" ]] || {
    demo_error 'Exactly one Radius chart package is required'; exit 1;
  }
  cp "${charts[0]}" "$packaged/radius.tgz"
  helm_cli template radius "$packaged/radius.tgz" --namespace radius-system \
    --set dashboard.enabled=false --set global.terraform.enabled=false \
    --set dynamicrp.buildkit.enabled=false --set global.terraform.loglevel=OFF |
    assets images "$executor" >"$work/chart-images.json"
  jq -c --arg node "$node_image" --arg python "$python_image" --arg postgres "$postgres_image" \
    --arg envoy "$envoy_image" --arg redis "$redis_image" \
    '. + [$node,$python,$postgres,$envoy,$redis] | unique' \
    "$work/chart-images.json" >"$work/dependencies.json"
  jq -er '.[]' "$work/dependencies.json" >"$work/dependency-list"
  : >"$work/pulled.jsonl"
  while IFS= read -r image; do
    docker_cli pull --platform "linux/$arch" "$image"
    docker_cli image inspect "$image" | jq -ce --arg reference "$image" --arg arch "$arch" '
      select(length == 1) | .[0] | select(.Os == "linux" and .Architecture == $arch) |
      {reference:$reference,id:.Id}
    ' >>"$work/pulled.jsonl"
  done <"$work/dependency-list"
  jq -s . "$work/pulled.jsonl" >"$packaged/images.json"
  assets package-recipes "$work/source" "$packaged" "$revision" >&2
  for target in executor operator provisioner; do
    case "$target" in executor) ref=$executor ;; operator) ref=$operator ;; provisioner) ref=$provisioner ;; esac
    build_image "$ref" scripts/operations/local/bootstrap-assets.Dockerfile --target "$target" \
      --build-arg "EXECUTOR_BASE=$executor_base" --build-arg "OPERATOR_BASE=$operator_base" \
      --build-arg "PROVISIONER_BASE=$provisioner_base" --build-arg "TOOLS_BASE=$tools_base"
  done
fi

upstream=$(docker_cli run --rm --network none --pull=never --entrypoint /bin/sh \
  "$radius_base" -ec 'sha256sum /dynamic-rp')
upstream=${upstream%% *}
[[ "$upstream" =~ ^[a-f0-9]{64}$ ]] || { demo_error 'Invalid upstream binary proof'; exit 1; }
: >"$work/images.jsonl"
for role in api provisioner executor operator; do
  case "$role" in api) ref=$api; user=10001:10001 ;; provisioner) ref=$provisioner; user=10001:10001 ;;
    executor) ref=$executor; user=65532:65532 ;; operator) ref=$operator; user=65532:65532 ;; esac
  image_id=$(owned_image "$ref") || { demo_error 'Image ownership or platform differs'; exit 1; }
  docker_cli image inspect "$image_id" | jq -e --arg user "$user" --arg role "$role" '
    .[0].Config.User == $user and
    ($role != "executor" or .[0].Config.Entrypoint == ["/dynamic-rp"]) and
    ($role != "operator" or .[0].Config.Entrypoint == ["python3"]) and
    ($role != "api" or .[0].Config.Cmd == ["python","-m","plane_demo.management.api"]) and
    ($role != "provisioner" or .[0].Config.Cmd == ["python","-m","plane_demo.management.provisioner"])
  ' >/dev/null || { demo_error 'Image runtime identity differs'; exit 1; }
  inspection_container=$(docker_cli create --network none --entrypoint /bin/true "$image_id")
  [[ "$inspection_container" =~ ^[a-f0-9]{64}$ ]] || { demo_error 'Invalid inspection container'; exit 1; }
  docker_cli export --output "$work/rootfs.tar" "$inspection_container"
  assets inspect "$work/source" "$role" "$arch" "$work/rootfs.tar" --upstream "$upstream" \
    >"$work/proofs/$role.json"
  docker_cli rm "$inspection_container" >/dev/null
  inspection_container=''
  jq -n --arg role "$role" --arg reference "$ref" --arg id "$image_id" \
    '{key:$role,value:{reference:$reference,id:$id}}' >>"$work/images.jsonl"
done
jq -er '.dependencies[] | [.reference,.id] | @tsv' \
  "$work/proofs/operator.json" >"$work/dependency-identities"
while IFS=$'\t' read -r reference expected; do
  actual=$(docker_cli image inspect "$reference" | jq -er '.[0].Id')
  [[ "$actual" == "$expected" ]] || { demo_error 'A prepared dependency image changed'; exit 1; }
done <"$work/dependency-identities"
[[ "$(git -C "$ROOT" rev-parse HEAD)" == "$revision" ]] || {
  demo_error 'The selected source revision changed during the operation'; exit 1;
}
jq -n --arg revision "$revision" --arg stem "$stem" --arg arch "$arch" \
  --arg radiusHash "$upstream" --slurpfile images "$work/images.jsonl" \
  --slurpfile operator "$work/proofs/operator.json" \
  '{revision:$revision,stem:$stem,architecture:$arch,images:($images|from_entries),
    radiusBinarySHA256:$radiusHash,dependencies:$operator[0].dependencies,
    coldChildReady:false,
    pending:["Deploy the management application and service-owned credentials through the parent entrypoint",
             "Prove first-child startup with external access blocked"]}'
