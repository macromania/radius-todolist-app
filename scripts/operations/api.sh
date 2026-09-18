#!/usr/bin/env bash
set -euo pipefail
set +x
umask 077
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source=../lib/env.sh
source "$ROOT/scripts/lib/env.sh"
# shellcheck source=../lib/discovery.sh
source "$ROOT/scripts/lib/discovery.sh"
(( $# >= 3 && $# <= 4 )) || { demo_error 'Usage: api TARGET GET|POST|PUT /path [JSON]'; exit 1; }
target=$1 method=$2 route=$3
case "$target" in
  management) slot=management ;;
  control:shared) slot=shared-control ;;
  data:shared) slot=shared-data ;;
  control:isolated-1) slot=isolated-1-control ;;
  data:isolated-1) slot=isolated-1-data ;;
  control:isolated-*) slot="${target#control:}-control" ;;
  data:isolated-*) slot="${target#data:}-data" ;;
  *) demo_error 'Unknown API target'; exit 1 ;;
esac
case "$method" in GET|POST|PUT) ;; *) demo_error 'Unsupported API method'; exit 1 ;; esac
[[ "$route" == /* && "$route" != //* && "$route" != *'#'* \
  && "$route" != *$'\n'* && "$route" != *$'\r'* ]] || { demo_error 'Invalid API path'; exit 1; }
demo_load_env "$ROOT/.env"
demo_workspace
trap demo_remove_workspace EXIT
body="$DEMO_WORKSPACE/request.json"
if (( $# == 4 )); then
  printf '%s' "$4" > "$body"
elif [[ "$method" != GET && ! -t 0 ]]; then
  head -c 8193 > "$body"
else
  : > "$body"
fi
(( $(wc -c < "$body") <= 8192 )) || { demo_error 'API request body is too large'; exit 1; }
curl_body=()
if [[ -s "$body" ]]; then
  jq -e 'type == "object"' "$body" >/dev/null || { demo_error 'API body must be a JSON object'; exit 1; }
  curl_body=(--data-binary "@$body")
fi
demo_open_slot "$slot"
url=$(demo_endpoint)
variable="DEMO_KEY_$(printf '%s' "$slot" | tr '[:lower:]-' '[:upper:]_')"
if printenv "$variable" >/dev/null; then
  key=$(printenv "$variable" | jq -Rsr '.[0:-1]') || exit
else
  secret_name="$DEMO_ROLE-api-runtime"
  record=$(demo_kube get secret "$secret_name" --output \
    'jsonpath={.metadata.name}{"\n"}{.metadata.namespace}{"\n"}{.data.DEMO_KEY}{"\n"}') || exit
  prefix="$secret_name"$'\n'"$DEMO_NAMESPACE"$'\n'
  [[ "$record" == "$prefix"* ]] || { demo_error 'API credential scope differs'; exit 1; }
  key=$(printf '%s' "${record#"$prefix"}" | jq -Rser \
    '@base64d | select(test("^[!-~]{32,512}$"))' 2>/dev/null) || {
    demo_error 'API credential is missing or invalid'; exit 1;
  }
fi
{
  printf 'header = '
  printf 'X-Demo-Key: %s' "$key" | jq -Rs .
  printf 'header = "Content-Type: application/json"\n'
} > "$DEMO_WORKSPACE/curl.conf"
unset key record
status=$(curl -q --silent --show-error --noproxy '*' --connect-timeout 5 --max-time 30 \
  --max-filesize 1000000 --config "$DEMO_WORKSPACE/curl.conf" --request "$method" \
  --url "$url$route" "${curl_body[@]+"${curl_body[@]}"}" \
  --output "$DEMO_WORKSPACE/response.json" --write-out '%{http_code}') || exit
printf 'HTTP %s\n' "$status" >&2
cat "$DEMO_WORKSPACE/response.json"
printf '\n'
[[ "$status" =~ ^2[0-9][0-9]$ ]]
