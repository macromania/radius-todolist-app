#!/usr/bin/env bash
# Fake az/rad executables for test-publish-recipe.sh. Never contact Azure.
set -euo pipefail

fail() { echo "FAKE CLI: $*" >&2; exit 1; }

case "$(basename "$0")" in
  az)
    [[ " $* " == *" -n testregistry "* ]] || fail "wrong registry: $*"
    [[ " $* " == *" --subscription test-subscription "* ]] || fail "wrong subscription: $*"
    if [ "$1 $2" = "acr login" ]; then
      OP=login
    else
      [ "$1 $2" = "acr repository" ] || fail "unexpected command: $*"
      OP="$3"
    fi
    ;;
  rad)
    [ "$*" = "bicep publish --file infra/radius/recipes/azure/managed-redis.bicep --target br:testregistry.azurecr.io/radius-recipes/azure-managed-redis:0.2.0" ] \
      || fail "unexpected publish command: $*"
    [ -f "$4" ] || fail "Recipe source does not exist: $4"
    OP=publish
    ;;
  *) fail "run this fixture through test-publish-recipe.sh";;
esac
echo "$OP" >> "$TEST_CALLS"

if [ "$SCENARIO" = "$OP-error" ]; then
  echo "$OP failed" >&2
  exit 1
fi

case "$OP" in
  list)
    case "$SCENARIO" in
      new-repository) echo false;;
      invalid-list) echo unexpected;;
      *) echo true;;
    esac
    ;;
  show-tags)
    [[ " $* " == *" --repository radius-recipes/azure-managed-redis "* ]] || fail "wrong repository"
    [[ " $* " == *"[?name=='0.2.0'].digest | [0]"* ]] || fail "wrong tag query"
    case "$SCENARIO" in
      mismatch) echo "$TEST_OTHER_DIGEST";;
      invalid-existing-digest) echo malformed;;
      existing|update-error|unconfirmed-lock) echo "$TEST_DIGEST";;
      *) ;;
    esac
    ;;
  login|publish) ;;
  show)
    [[ " $* " == *" --image radius-recipes/azure-managed-redis:0.2.0 "* ]] || fail "wrong image"
    if [ "$SCENARIO" = invalid-new-digest ]; then echo malformed; else echo "$TEST_OTHER_DIGEST"; fi
    ;;
  update)
    [[ " $* " == *" --image radius-recipes/azure-managed-redis:0.2.0 "* ]] || fail "wrong image"
    [[ " $* " == *" --write-enabled false --delete-enabled false "* ]] || fail "tag must stay locked"
    [[ " $* " == *"changeableAttributes.writeEnabled == \`false\` && changeableAttributes.deleteEnabled == \`false\`"* ]] \
      || fail "must confirm both locks"
    case "$SCENARIO" in
      existing|update-error|unconfirmed-lock) DIGEST="$TEST_DIGEST";;
      *) DIGEST="$TEST_OTHER_DIGEST";;
    esac
    [[ " $* " == *"digest == '$DIGEST'"* ]] || fail "must confirm digest"
    if [ "$SCENARIO" = unconfirmed-lock ]; then echo false; else echo true; fi
    ;;
  *) fail "unexpected operation: $OP";;
esac
