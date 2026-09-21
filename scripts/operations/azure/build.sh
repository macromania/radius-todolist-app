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
    '       CONFIRM_AZURE=yes bash scripts/operations/azure/build.sh --recover-build api=RUN_ID' \
    'Uses DEMO_REVISION or HEAD, with a temporary committed-source context and registry login.' \
    'Recipe reuse requires exact compiled OCI content. Build/push logs remain visible on stderr.' \
    'Default mode builds or reuses digest-pinned images only after trusted, non-executing ACR-hosted inspection.' \
    '--inspect requires no mutation confirmation and makes no Azure/ACR writes. Missing, mutable, or mismatching artifacts fail.' \
    'Inspection revalidates recorded remote evidence; missing evidence requires a confirmed build run, never workstation Docker.' \
    'Pending ARM build receipts make retries resumable. Run only one build command per deployment at a time.' \
    'Unrecorded API images require explicit --recover-build api=RUN_ID, matching build logs and full image inspection.' \
    'Canonical Recipes are ARM-import-only repositories in an ABAC registry; publisher credentials cannot change their tags or content.' \
    'Canonical tags are never overwritten. No images.json or recipes.json is retained.'
  exit 0
fi
RECIPES_ONLY=false
INSPECT_ONLY=false
RECOVER_API=''
while (( $# )); do
  case "$1" in
    --recipes-only) RECIPES_ONLY=true; shift ;;
    --inspect) INSPECT_ONLY=true; shift ;;
    --recover-build)
      if (( $# < 2 )) || [[ ! "$2" =~ ^api=[a-zA-Z0-9]{1,32}$ ]]; then
        demo_error 'Use --recover-build api=RUN_ID'; exit 1;
      fi
      if [[ -z "$RECOVER_API" ]]; then
        RECOVER_API="${2#*=}"
      else
        demo_error 'Only one API recovery run may be selected'; exit 1
      fi
      shift 2 ;;
    *) demo_error 'build accepts --recipes-only, --inspect or --recover-build'; exit 1 ;;
  esac
done
if [[ "$RECIPES_ONLY" == true && "$INSPECT_ONLY" == true ]] \
  || { [[ -n "$RECOVER_API" ]] \
    && [[ "$RECIPES_ONLY" == true || "$INSPECT_ONLY" == true ]]; }; then
  demo_error 'Recovery, recipe-only and inspection modes cannot be combined'; exit 1
fi
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
SOURCE_FILES=(src sql scripts infra images pyproject.toml uv.lock .dockerignore)
LICENSE_ENTRY=$(git -C "$ROOT" ls-tree --name-only "$REVISION" -- LICENSE)
case "$LICENSE_ENTRY" in
  LICENSE) SOURCE_FILES+=(LICENSE) ;;
  '') ;; # Recovery can select a revision from before the license was added.
  *) demo_error 'Unexpected license path in selected source'; exit 1 ;;
esac
git -C "$ROOT" archive --format=tar "$REVISION" "${SOURCE_FILES[@]}" \
  > "$AZURE_WORKSPACE/source.tar"
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
azure_registry_credentials

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
  azure_pull_blob "$repository" "$layer" "$AZURE_WORKSPACE/recipe-blob.json" 20971520 || return
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
    inputs+=(scripts/operations/azure/plane-policy.json)
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
  recover_run="$RECOVER_API"
  [[ "$component" != provisioner ]] || recover_run=''
  created=false
  canonical_exists=false
  canonical_digest=''
  if azure_has_tag "plane-$component" "$REVISION"; then
    canonical_exists=true
    canonical_digest=$(azure_digest "$image")
  else
    status=$?
    [[ "$status" == 1 ]] || { demo_error 'Cannot determine whether the image tag exists'; exit 1; }
  fi
  azure_build_state "$component" "$api_base"
  build_state=$(jq -er '.state' "$AZURE_WORKSPACE/build-state-$component.json")
  if [[ "$build_state" == verified ]]; then
    [[ "$canonical_exists" == true ]] || {
      demo_error 'A verified ARM build proof exists but its canonical image is missing'; exit 1;
    }
    if [[ -n "$recover_run" ]]; then
      jq -e --arg runId "$recover_run" '.runId == $runId' \
        "$AZURE_WORKSPACE/build-state-$component.json" >/dev/null || {
        demo_error 'Recovery cannot replace a different verified build'; exit 1;
      }
    fi
    digest="$canonical_digest"
    azure_existing_provenance "$component" "$digest" "$api_base"
  else
    [[ "$INSPECT_ONLY" == false ]] || {
      demo_error 'arm_build_provenance_missing_or_mismatched; inspection will not repair it'; exit 1;
    }
    submit=false
    if [[ -n "$recover_run" ]]; then
      azure_recover_build "$component" "$api_base" "$recover_run"
    elif [[ "$build_state" == missing ]]; then
      [[ "$canonical_exists" == false ]] || {
        demo_error 'arm_build_provenance_missing_or_mismatched; existing images require explicit recovery'
        exit 1
      }
      staging_tag="build-$REVISION-$BUILD_RUN"
      if azure_has_tag "plane-$component" "$staging_tag"; then
        demo_error 'Unique staging tag already exists; no image will be overwritten'; exit 1
      else
        status=$?
        [[ "$status" == 1 ]] || { demo_error 'Cannot verify the staging tag is absent'; exit 1; }
      fi
      azure_provenance "$component" "$api_base" intent --nonce "$BUILD_RUN" \
        > "$AZURE_WORKSPACE/next-intent-$component.json"
      azure_store_build_intent "$component" "$AZURE_WORKSPACE/next-intent-$component.json"
      submit=true
    fi
    staging=$(jq -er '.staging' "$AZURE_WORKSPACE/build-state-$component.json")
    if [[ "$submit" == true ]]; then
      azure_fresh_build "$component" "$staging" "$api_base"
    else
      replay_logs=true
      [[ -z "$recover_run" ]] || replay_logs=false
      azure_finish_candidate "$component" "$staging" "$api_base" "$replay_logs"
    fi
    digest=$(jq -er '.provenance.digest' "$AZURE_WORKSPACE/fresh-$component.json")
    [[ "$canonical_exists" == false || "$canonical_digest" == "$digest" ]] || {
      demo_error 'Canonical image differs from the recorded build; it will not be overwritten'; exit 1;
    }
    created=true
  fi
  reference="$HOST/plane-$component@$digest"
  azure_inspect_image "$component" "$reference" "$api_base" "$created" "${staging:-}"
  if [[ "$created" == true && "$canonical_exists" == false ]]; then
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
