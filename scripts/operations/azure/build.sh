#!/usr/bin/env bash
set -euo pipefail
set +x
umask 077
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd -P)
# shellcheck source=azure.shlib
source "$ROOT/scripts/operations/azure/azure.shlib"
# shellcheck source=images.shlib
source "$ROOT/scripts/operations/azure/images.shlib"
# shellcheck source=provenance.shlib
source "$ROOT/scripts/operations/azure/provenance.shlib"

if [[ "${1:-}" == --help ]]; then
  printf '%s\n' 'Usage: CONFIRM_AZURE=yes bash scripts/operations/azure/build.sh [--recipes-only]' \
    '       bash scripts/operations/azure/build.sh --inspect' \
    'Uses DEMO_REVISION or HEAD, with a temporary committed-source context and registry login.' \
    'Recipe reuse requires exact compiled OCI content. Build/push logs remain visible on stderr.' \
    'Default mode builds or reuses digest-pinned images only after non-executing Docker Desktop filesystem inspection.' \
    '--inspect requires no mutation confirmation and makes no Azure/ACR writes. Missing, mutable, or mismatching artifacts fail.' \
    'Inspection uses temporary local Bicep compilation/type generation, registry authentication, and never-started Docker exports.' \
    'Images require v2 ARM-owned ACR build-run provenance; unrecorded existing images are never attested retroactively.' \
    'Canonical Recipes are ARM-import-only repositories in an ABAC registry; publisher credentials cannot change their tags or content.' \
    'Canonical tags are never overwritten. No images.json or recipes.json is retained.'
  exit 0
fi
[[ $# == 0 || ( $# == 1 && ( "$1" == --recipes-only || "$1" == --inspect ) ) ]] || {
  demo_error 'build accepts --recipes-only or --inspect'; exit 1;
}
RECIPES_ONLY=false
INSPECT_ONLY=false
[[ "${1:-}" != --recipes-only ]] || RECIPES_ONLY=true
[[ "${1:-}" != --inspect ]] || INSPECT_ONLY=true
if [[ "$INSPECT_ONLY" == true ]]; then
  azure_init read
else
  azure_init
fi
azure_foundation
azure_registry
azure_recipe_policy

REVISION=$(git -C "$ROOT" rev-parse --verify "${DEMO_REVISION:-HEAD}^{commit}")
[[ "$REVISION" =~ ^[a-f0-9]{40}$ ]] || { demo_error 'Expected a full selected Git commit'; exit 1; }
[[ -z "${DEMO_REVISION:-}" || "$REVISION" == "$DEMO_REVISION" ]] || {
  demo_error 'Selected revision resolved to a different commit'; exit 1;
}
SOURCE="$AZURE_WORKSPACE/source"
mkdir -m 700 "$SOURCE"
git -C "$ROOT" archive --format=tar "$REVISION" \
  src sql scripts infra images pyproject.toml uv.lock .dockerignore > "$AZURE_WORKSPACE/source.tar"
tar -xpf "$AZURE_WORKSPACE/source.tar" -C "$SOURCE"
[[ -z "$(find "$SOURCE" -type l -print)" ]] || {
  demo_error 'Build context must not contain symlinks'; exit 1;
}

RADIUS_VERSION=$(rad version --cli)
[[ "$RADIUS_VERSION" =~ (^|[^0-9])0\.60\.2([^0-9.]|$) ]] || {
  demo_error 'Radius 0.60.2 is required'; exit 1;
}
RADIUS_HOME="$AZURE_WORKSPACE/radius-home"
mkdir -p "$RADIUS_HOME/.rad/bin" "$AZURE_WORKSPACE/docker"
ln -s "$BICEP" "$RADIUS_HOME/.rad/bin/bicep"
export DOCKER_CONFIG="$AZURE_WORKSPACE/docker"
azure_json acr login --name "$REGISTRY" --expose-token > "$AZURE_WORKSPACE/registry-token.json"
jq -e --arg host "$HOST" '
  .loginServer == $host and (.accessToken | type == "string" and test("^[A-Za-z0-9._~+/=-]+$"))
' "$AZURE_WORKSPACE/registry-token.json" >/dev/null || {
  demo_error 'Registry authentication response is invalid'; exit 1;
}
jq -jr '.accessToken' "$AZURE_WORKSPACE/registry-token.json" \
  | docker --config "$DOCKER_CONFIG" login "$HOST" \
    --username 00000000-0000-0000-0000-000000000000 --password-stdin >&2
[[ -f "$DOCKER_CONFIG/config.json" && ! -L "$DOCKER_CONFIG/config.json" ]] || {
  demo_error 'Registry login did not create a private configuration'; exit 1;
}
chmod 600 "$DOCKER_CONFIG/config.json"

verify_recipe() {
  local repository="$1" digest="$2" compiled="$3" layer actual
  azure_json acr manifest show --registry "$REGISTRY" --name "$repository@$digest" --raw \
    > "$AZURE_WORKSPACE/manifest.json" || return
  jq -e '
    .schemaVersion == 2 and (.layers | length) == 1 and
    .layers[0].mediaType == "application/vnd.ms.bicep.module.layer.v1+json"
  ' "$AZURE_WORKSPACE/manifest.json" >/dev/null || {
    demo_error 'Unexpected Recipe OCI manifest'; return 1;
  }
  layer=$(jq -er '.layers[0].digest | select(test("^sha256:[a-f0-9]{64}$"))' \
    "$AZURE_WORKSPACE/manifest.json") || return
  jq -r --arg host "$HOST" --arg scope "repository:$repository:pull" '
    ["grant_type=refresh_token", ("service=" + $host), ("scope=" + $scope),
     ("refresh_token=" + .accessToken)][] | "data-urlencode = " + @json
  ' "$AZURE_WORKSPACE/registry-token.json" > "$AZURE_WORKSPACE/token-request.curl" || return
  curl --fail --silent --show-error --max-time 30 --proto '=https' \
    --config "$AZURE_WORKSPACE/token-request.curl" "https://$HOST/oauth2/token" \
    --output "$AZURE_WORKSPACE/pull-token.json" || return
  jq -er '
    .access_token | select(type == "string" and test("^[A-Za-z0-9._~+/=-]+$")) |
    "header = " + (("Authorization: Bearer " + .) | @json)
  ' "$AZURE_WORKSPACE/pull-token.json" > "$AZURE_WORKSPACE/pull.curl" || return
  curl --fail --silent --show-error --max-time 60 --max-filesize 20971520 \
    --proto '=https' --proto-redir '=https' --location \
    --config "$AZURE_WORKSPACE/pull.curl" "https://$HOST/v2/$repository/blobs/$layer" \
    --output "$AZURE_WORKSPACE/recipe-blob.json" || return
  actual="sha256:$(shasum -a 256 "$AZURE_WORKSPACE/recipe-blob.json" | cut -d ' ' -f 1)"
  [[ "$actual" == "$layer" ]] || { demo_error 'Recipe OCI blob digest differs'; return 1; }
  jq -Sc . "$compiled" > "$AZURE_WORKSPACE/expected.json" || return
  jq -Sc . "$AZURE_WORKSPACE/recipe-blob.json" > "$AZURE_WORKSPACE/observed.json" || return
  cmp -s "$AZURE_WORKSPACE/expected.json" "$AZURE_WORKSPACE/observed.json" || {
    demo_error 'Recipe OCI content differs from the selected committed source'; return 1;
  }
}

RECIPES='{}'
for name in cluster postgresql gateway redis; do
  source_file="infra/radius/recipes/azure/$name.bicep"
  inputs=("$source_file" infra/radius/recipes/azure/bicepconfig.json)
  if [[ "$name" == cluster ]]; then
    for module in "$SOURCE"/infra/bootstrap/*.bicep; do
      inputs+=("${module#"$SOURCE"/}")
    done
  fi
  source_hash=$(
    { printf '%s\n' "$BICEP_VERSION"; (cd "$SOURCE" && shasum -a 256 "${inputs[@]}"); } \
      | shasum -a 256 | cut -d ' ' -f 1
  )
  [[ "$source_hash" =~ ^[a-f0-9]{64}$ ]] || { demo_error 'Invalid Recipe source hash'; exit 1; }
  tag="src-$source_hash"
  repository="radius-recipes/$name"
  staging_repository="radius-recipe-staging/$name"
  image="$repository:$tag"
  "$BICEP" build "$SOURCE/$source_file" --outfile "$AZURE_WORKSPACE/compiled.json"
  if azure_has_tag "$repository" "$tag"; then
    digest=$(azure_digest "$image")
    printf 'Verifying existing Recipe %s\n' "$image" >&2
  else
    status=$?
    [[ "$status" == 1 ]] || { demo_error 'Cannot determine whether the Recipe tag exists'; exit 1; }
    [[ "$INSPECT_ONLY" == false ]] || {
      demo_error 'Selected committed Recipe artifact is missing; inspection will not publish it'; exit 1;
    }
    staging_recipe="$staging_repository:publish-$source_hash-${AZURE_WORKSPACE##*.}"
    HOME="$RADIUS_HOME" rad --config "$AZURE_WORKSPACE/radius.yaml" bicep publish \
      --file "$SOURCE/$source_file" --target "br:$HOST/$staging_recipe" >&2
    digest=$(azure_digest "$staging_recipe")
    verify_recipe "$staging_repository" "$digest" "$AZURE_WORKSPACE/compiled.json"
    az acr import --subscription "$AZURE_SUBSCRIPTION_ID" --name "$REGISTRY" \
      --registry "$PLATFORM_SCOPE/providers/Microsoft.ContainerRegistry/registries/$REGISTRY" \
      --source "$staging_repository@$digest" --image "$image" >&2
  fi
  verify_recipe "$repository" "$digest" "$AZURE_WORKSPACE/compiled.json"
  observed_digest=$(azure_digest "$image")
  [[ "$observed_digest" == "$digest" ]] || {
    demo_error 'Recipe tag changed during verification'; exit 1;
  }
  RECIPES=$(printf '%s' "$RECIPES" | jq --arg name "$name" --arg reference "$HOST/$image" \
    --arg digest "$digest" --arg source "$source_hash" \
    '. + {($name): {reference:$reference, digest:$digest, source_sha256:$source,
      content_verified:true, immutability:"acr-abac-arm-import-v1"}}')
done

if [[ "$RECIPES_ONLY" == true ]]; then
  jq -n --arg revision "$REVISION" --argjson recipes "$RECIPES" \
    '{source_revision:$revision, recipes:$recipes}'
  exit 0
fi

for types in "$SOURCE"/infra/radius/types/*.yaml; do
  HOME="$RADIUS_HOME" rad --config "$AZURE_WORKSPACE/radius.yaml" bicep publish-extension \
    --from-file "$types" --target "${types%.yaml}.tgz" >&2
  chmod 644 "${types%.yaml}.tgz"
done
IMAGES='{}'
INSPECTIONS='{}'
azure_image_tools
if [[ "$INSPECT_ONLY" == false ]]; then
  BUILD_RUN=$("$ROOT/.venv/bin/python" -c 'import uuid; print(uuid.uuid4().hex)')
  [[ "$BUILD_RUN" =~ ^[a-f0-9]{32}$ ]] || { demo_error 'Invalid unique build identity'; exit 1; }
fi
for component in api provisioner; do
  image="plane-$component:$REVISION"
  api_base=''
  [[ "$component" != provisioner ]] || api_base=$(printf '%s' "$IMAGES" | jq -er '.api')
  created=false
  if azure_has_tag "plane-$component" "$REVISION"; then
    digest=$(azure_digest "$image")
    azure_existing_provenance "$component" "$digest" "$api_base"
  else
    status=$?
    [[ "$status" == 1 ]] || { demo_error 'Cannot determine whether the image tag exists'; exit 1; }
    [[ "$INSPECT_ONLY" == false ]] || {
      demo_error 'Selected committed image is missing; inspection will not build it'; exit 1;
    }
    staging_tag="build-$REVISION-$BUILD_RUN"
    staging="plane-$component:$staging_tag"
    if azure_has_tag "plane-$component" "$staging_tag"; then
      demo_error 'Unique staging tag already exists; no image will be overwritten'; exit 1
    else
      status=$?
      [[ "$status" == 1 ]] || { demo_error 'Cannot verify the staging tag is absent'; exit 1; }
    fi
    azure_fresh_build "$component" "$staging" "$api_base"
    digest=$(jq -er '.provenance.digest' "$AZURE_WORKSPACE/fresh-$component.json")
    created=true
  fi
  reference="$HOST/plane-$component@$digest"
  azure_inspect_image "$component" "$reference" "$api_base" "$created" "${staging:-}"
  if [[ "$created" == true ]]; then
    # ACR import without --force refuses a concurrently created canonical tag.
    az acr import --subscription "$AZURE_SUBSCRIPTION_ID" --name "$REGISTRY" \
      --registry "$PLATFORM_SCOPE/providers/Microsoft.ContainerRegistry/registries/$REGISTRY" \
      --source "plane-$component@$digest" --image "$image" >&2
  fi
  observed_digest=$(azure_digest "$image")
  [[ "$observed_digest" == "$digest" ]] || { demo_error 'Image tag differs from inspected content'; exit 1; }
  if [[ "$INSPECT_ONLY" == true ]]; then
    azure_verify_lock "$image" "$digest"
  else
    azure_lock "$image" "$digest"
  fi
  observed_digest=$(azure_digest "$image")
  [[ "$observed_digest" == "$digest" ]] || { demo_error 'Image tag changed during verification'; exit 1; }
  if [[ "$created" == true ]]; then
    azure_record_build_proof "$component" "$digest" "$api_base"
  fi
  IMAGES=$(printf '%s' "$IMAGES" | jq --arg component "$component" \
    --arg reference "$reference" '. + {($component):$reference}')
  INSPECTIONS=$(printf '%s' "$INSPECTIONS" | jq --arg component "$component" \
    --slurpfile proof "$AZURE_WORKSPACE/inspection-$component.json" '. + {($component):$proof[0]}')
done
jq -n --arg revision "$REVISION" --argjson recipes "$RECIPES" --argjson images "$IMAGES" \
  --argjson inspections "$INSPECTIONS" \
  '{source_revision:$revision, recipes:$recipes, images:$images,
    inspections:$inspections, content_verified:true, status:"artifacts_verified"}'
