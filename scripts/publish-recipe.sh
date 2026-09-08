#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

ACR_NAME="${ACR_NAME:?set ACR_NAME}"
SUBSCRIPTION="${SUBSCRIPTION:?set SUBSCRIPTION}"
RECIPE_TAG="${RECIPE_TAG:?set RECIPE_TAG}"
RECIPE_EXPECTED_DIGEST="${RECIPE_EXPECTED_DIGEST:?set RECIPE_EXPECTED_DIGEST}"
REPOSITORY=radius-recipes/azure-managed-redis
IMAGE="$REPOSITORY:$RECIPE_TAG"
REF="$ACR_NAME.azurecr.io/$IMAGE"

fail() { echo "FAIL: $*" >&2; exit 1; }

[[ "$RECIPE_TAG" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}$ ]] \
  || fail "RECIPE_TAG must be a valid OCI tag"
[[ "$RECIPE_EXPECTED_DIGEST" =~ ^sha256:[a-f0-9]{64}$ ]] \
  || fail "RECIPE_EXPECTED_DIGEST must be a sha256 digest"

echo "==> checking for $REF"
# Successful listings distinguish absence from authentication or network errors.
# A failed lookup must never be treated as permission to overwrite a tag.
EXISTS=$(az acr repository list -n "$ACR_NAME" --subscription "$SUBSCRIPTION" \
  --query "contains(@, '$REPOSITORY')" -o tsv)
ACTUAL_DIGEST=
case "$EXISTS" in
  true)
    ACTUAL_DIGEST=$(az acr repository show-tags -n "$ACR_NAME" \
      --subscription "$SUBSCRIPTION" --repository "$REPOSITORY" --detail \
      --query "[?name=='$RECIPE_TAG'].digest | [0]" -o tsv)
    ;;
  false) ;;
  *) fail "unexpected repository lookup result: $EXISTS";;
esac

if [ -n "$ACTUAL_DIGEST" ]; then
  if [ "$ACTUAL_DIGEST" != "$RECIPE_EXPECTED_DIGEST" ]; then
    fail "$REF resolves to $ACTUAL_DIGEST, not $RECIPE_EXPECTED_DIGEST.
Refusing to overwrite it. Review the existing digest or publish a new RECIPE_TAG."
  fi
  echo "Recipe already matches the pinned digest; skipping publish."
  echo "Local source is not republished. Use a new RECIPE_TAG for source changes."
else
  az acr login -n "$ACR_NAME" --subscription "$SUBSCRIPTION"
  rad bicep publish --file infra/radius/recipes/azure/managed-redis.bicep --target "br:$REF"
  ACTUAL_DIGEST=$(az acr repository show -n "$ACR_NAME" \
    --subscription "$SUBSCRIPTION" --image "$IMAGE" --query digest -o tsv)
fi

[[ "$ACTUAL_DIGEST" =~ ^sha256:[a-f0-9]{64}$ ]] \
  || fail "registry returned an invalid digest: $ACTUAL_DIGEST"

# Also finish locking a matching tag if an earlier run stopped after publishing.
LOCKED=$(az acr repository update -n "$ACR_NAME" --subscription "$SUBSCRIPTION" \
  --image "$IMAGE" --write-enabled false --delete-enabled false \
  --query "digest == '$ACTUAL_DIGEST' && changeableAttributes.writeEnabled == \`false\` && changeableAttributes.deleteEnabled == \`false\`" \
  -o tsv)
[ "$LOCKED" = true ] || fail "could not confirm the digest and locks for $REF"

echo "Tag locked: $REF"
echo "RECIPE_EXPECTED_DIGEST=$ACTUAL_DIGEST"
if [ "$ACTUAL_DIGEST" != "$RECIPE_EXPECTED_DIGEST" ]; then
  echo "Update RECIPE_TAG and RECIPE_EXPECTED_DIGEST in the Makefile before make env-azure."
fi
