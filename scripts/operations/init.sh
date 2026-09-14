#!/usr/bin/env bash
set -euo pipefail
set +x
umask 077
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source=../lib/env.sh
source "$ROOT/scripts/lib/env.sh"

environment='' project='radplanes' deployment='learning'
subscription='' location='' vault='' revision=''
credential_inputs=()
while (( $# )); do
  case "$1" in
    --environment|--project|--deployment|--subscription|--location|--key-vault|--revision|--demo-key-from-env)
      (( $# >= 2 )) || { demo_error 'Missing initialization argument'; exit 1; }
      case "$1" in
        --environment) environment="$2" ;;
        --project) project="$2" ;;
        --deployment) deployment="$2" ;;
        --subscription) subscription="$2" ;;
        --location) location="$2" ;;
        --key-vault) vault="$2" ;;
        --revision) revision="$2" ;;
        --demo-key-from-env) credential_inputs+=("$2") ;;
      esac
      shift 2 ;;
    --help)
      printf '%s\n' 'Initialize .env: --environment azure|local [--project NAME] [--deployment NAME]' \
        'Azure: [--subscription UUID] [--location REGION] [--key-vault NAME]' \
        'Optional: --revision COMMIT --demo-key-from-env SLOT=VARIABLE'
      exit 0 ;;
    *) demo_error 'Unknown initialization option'; exit 1 ;;
  esac
done
if [[ -z "$environment" && -t 0 ]]; then
  read -r -p 'Environment (azure/local): ' environment
  read -r -p 'Project [radplanes]: ' project
  read -r -p 'Deployment [learning]: ' deployment
  project="${project:-radplanes}" deployment="${deployment:-learning}"
fi
case "$environment" in azure|local) ;; *) demo_error 'Choose --environment azure or local'; exit 1 ;; esac
if [[ "$environment" == azure ]]; then
  if [[ -z "$subscription" ]]; then
    subscription=$(az account show --query id --output tsv --only-show-errors) || {
      demo_error 'Azure account lookup failed; supply --subscription'; exit 1;
    }
  fi
  location="${location:-centralus}"
elif [[ -n "$subscription$location$vault" ]]; then
  demo_error 'Local initialization does not accept Azure settings'; exit 1
fi
pending=$(mktemp "$ROOT/.env.init.XXXXXX")
trap 'rm -f -- "$pending"' EXIT
emit() {
  printf '%s=' "$1" >> "$pending"
  printf '%s' "$2" | jq -Rs . >> "$pending"
}
emit DEMO_ENV "$environment"
emit DEMO_PROJECT "$project"
emit DEMO_DEPLOYMENT "$deployment"
if [[ "$environment" == azure ]]; then
  emit AZURE_SUBSCRIPTION_ID "$(printf '%s' "$subscription" | tr '[:upper:]' '[:lower:]')"
  emit AZURE_LOCATION "$location"
fi
[[ -z "$vault" ]] || emit DEMO_KEY_VAULT "$vault"
[[ -z "$revision" ]] || emit DEMO_REVISION "$revision"
for entry in "${credential_inputs[@]+"${credential_inputs[@]}"}"; do
  slot="${entry%%=*}" variable="${entry#*=}"
  [[ "$entry" == *=* && "$variable" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]] || {
    demo_error 'Use --demo-key-from-env SLOT=VARIABLE'; exit 1;
  }
  case "$slot" in management|shared-control|shared-data|isolated-1-control|isolated-1-data) ;;
    *) demo_error 'Unknown credential slot'; exit 1 ;;
  esac
  key="DEMO_KEY_$(printf '%s' "$slot" | tr '[:lower:]-' '[:upper:]_')"
  value=$(printenv "$variable" | jq -Rs '.[0:-1]') || {
    demo_error 'Credential environment variable is missing'; exit 1;
  }
  printf '%s=%s\n' "$key" "$value" >> "$pending"
done
demo_load_env "$pending"
if [[ -e "$ROOT/.env" || -L "$ROOT/.env" ]]; then
  demo_private_file "$ROOT/.env"
fi
mv -f "$pending" "$ROOT/.env"
printf 'Configured %s/%s (%s) in .env\n' "$DEMO_PROJECT" "$DEMO_DEPLOYMENT" "$DEMO_ENV"
