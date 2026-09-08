#!/usr/bin/env bash
# Exercise the Make target, including variable forwarding, with fake CLIs.
set -euo pipefail

cd "$(dirname "$0")/.."
TEST_DIR=$(mktemp -d)
cleanup() {
  rm -f "$TEST_DIR/az" "$TEST_DIR/rad" "$TEST_DIR/calls" "$TEST_DIR/output"
  rmdir "$TEST_DIR"
}
trap cleanup EXIT
ln -s "$PWD/scripts/test-publish-recipe-cli.sh" "$TEST_DIR/az"
ln -s "$PWD/scripts/test-publish-recipe-cli.sh" "$TEST_DIR/rad"

export TEST_CALLS="$TEST_DIR/calls"
export TEST_DIGEST=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
export TEST_OTHER_DIGEST=sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb

fail() {
  echo "FAIL: $*" >&2
  sed -n '1,100p' "$TEST_DIR/output" >&2
  exit 1
}

run_case() {
  local scenario="$1" expected_status="$2" expected_calls="$3" message="$4"
  local status=0
  : > "$TEST_CALLS"
  SCENARIO="$scenario" PATH="$TEST_DIR:$PATH" \
    make --no-print-directory publish-recipe ACR_NAME=testregistry \
      SUBSCRIPTION=test-subscription RECIPE_TAG=0.2.0 \
      RECIPE_EXPECTED_DIGEST="$TEST_DIGEST" > "$TEST_DIR/output" 2>&1 || status=$?
  if [ "$expected_status" = success ]; then
    [ "$status" -eq 0 ] || fail "$scenario should succeed"
  else
    [ "$status" -ne 0 ] || fail "$scenario should fail"
  fi
  [ "$(<"$TEST_CALLS")" = "$expected_calls" ] || fail "$scenario ran unexpected commands"
  grep -qF "$message" "$TEST_DIR/output" || fail "$scenario did not report: $message"
  echo "PASS: $scenario"
}

EXISTING=$'list\nshow-tags'
PUBLISHED="$EXISTING"$'\nlogin\npublish'
LOCKED="$PUBLISHED"$'\nshow\nupdate'

run_case existing success "$EXISTING"$'\nupdate' "skipping publish"
run_case new-tag success "$LOCKED" "Update RECIPE_TAG and RECIPE_EXPECTED_DIGEST"
run_case new-repository success $'list\nlogin\npublish\nshow\nupdate' "$TEST_OTHER_DIGEST"
run_case mismatch failure "$EXISTING" "Refusing to overwrite"
run_case invalid-existing-digest failure "$EXISTING" "Refusing to overwrite"
run_case list-error failure list "list failed"
run_case invalid-list failure list "unexpected repository lookup result"
run_case show-tags-error failure "$EXISTING" "show-tags failed"
run_case login-error failure "$EXISTING"$'\nlogin' "login failed"
run_case publish-error failure "$PUBLISHED" "publish failed"
run_case show-error failure "$PUBLISHED"$'\nshow' "show failed"
run_case invalid-new-digest failure "$PUBLISHED"$'\nshow' "invalid digest"
run_case update-error failure "$EXISTING"$'\nupdate' "update failed"
run_case unconfirmed-lock failure "$EXISTING"$'\nupdate' "could not confirm the digest and locks"
