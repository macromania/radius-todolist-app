#!/usr/bin/env bash

# shellcheck source=output.sh
source "$(dirname "${BASH_SOURCE[0]}")/output.sh"

demo_error() {
  demo_status error "$1"
  return 1
}

demo_private_file() {
  local permissions
  [[ -f "$1" && ! -L "$1" ]] || { demo_error 'Expected a regular configuration file'; return 1; }
  permissions=$(LC_ALL=C ls -ld "$1") || return
  [[ "${permissions:4:6}" == '------' ]] || {
    demo_error '.env must have owner-only permissions'; return 1;
  }
}

demo_validate_env() {
  local stem key value
  case "${DEMO_ENV:-}" in azure|local) ;; *) demo_error 'DEMO_ENV must be azure or local'; return 1 ;; esac
  [[ "${DEMO_PROJECT:-}" =~ ^[a-z][a-z0-9-]{0,15}$ && "$DEMO_PROJECT" != *- ]] || {
    demo_error 'Invalid DEMO_PROJECT'; return 1;
  }
  [[ "${DEMO_DEPLOYMENT:-}" =~ ^[a-z][a-z0-9-]{0,15}$ && "$DEMO_DEPLOYMENT" != *- ]] || {
    demo_error 'Invalid DEMO_DEPLOYMENT'; return 1;
  }
  stem="$DEMO_PROJECT-$DEMO_DEPLOYMENT-$DEMO_ENV"
  (( ${#stem} <= 25 )) || { demo_error 'Combined deployment name is too long'; return 1; }
  if [[ "$DEMO_ENV" == azure ]]; then
    [[ "${AZURE_SUBSCRIPTION_ID:-}" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]] || {
      demo_error 'AZURE_SUBSCRIPTION_ID must be a UUID'; return 1;
    }
    [[ "${AZURE_LOCATION:-}" =~ ^[a-z][a-z0-9]{1,31}$ ]] || {
      demo_error 'Invalid AZURE_LOCATION'; return 1;
    }
    if [[ -n "${AZURE_NODE_VM_SIZE+x}" ]]; then
      [[ "$AZURE_NODE_VM_SIZE" =~ ^Standard_[A-Za-z0-9_]{1,64}$ ]] || {
        demo_error 'AZURE_NODE_VM_SIZE must be an Azure VM size'; return 1;
      }
    fi
    if [[ -n "${AZURE_POSTGRES_SKU+x}${AZURE_POSTGRES_TIER+x}" ]]; then
      [[ "${AZURE_POSTGRES_SKU:-}" =~ ^Standard_[A-Za-z0-9_]{1,64}$ \
        && "${AZURE_POSTGRES_TIER:-}" =~ ^(Burstable|GeneralPurpose|MemoryOptimized)$ ]] || {
        demo_error 'AZURE_POSTGRES_SKU and AZURE_POSTGRES_TIER must contain a valid SKU and tier'; return 1;
      }
    fi
  elif [[ -n "${AZURE_SUBSCRIPTION_ID+x}${AZURE_LOCATION+x}${DEMO_KEY_VAULT+x}${AZURE_NODE_VM_SIZE+x}${AZURE_POSTGRES_SKU+x}${AZURE_POSTGRES_TIER+x}" ]]; then
    demo_error 'Local configuration must not contain Azure settings'; return 1
  fi
  if [[ -n "${DEMO_KEY_VAULT+x}" ]]; then
    [[ "$DEMO_KEY_VAULT" =~ ^[a-z][a-z0-9-]{1,22}[a-z0-9]$ && "$DEMO_KEY_VAULT" != *--* ]] || {
      demo_error 'Invalid DEMO_KEY_VAULT'; return 1;
    }
  fi
  if [[ -n "${DEMO_REVISION+x}" ]]; then
    [[ "$DEMO_REVISION" =~ ^[a-f0-9]{40}$ ]] || { demo_error 'Invalid DEMO_REVISION'; return 1; }
  fi
  for key in DEMO_KEY_MANAGEMENT DEMO_KEY_SHARED_CONTROL DEMO_KEY_SHARED_DATA \
    DEMO_KEY_ISOLATED_1_CONTROL DEMO_KEY_ISOLATED_1_DATA; do
    if printenv "$key" >/dev/null; then
      value=$(printenv "$key" | jq -Rs '.[0:-1]') || return
      printf '%s' "$value" | jq -e 'test("^[!-~]{32,512}$")' >/dev/null || {
        demo_error 'Demo keys must be printable ASCII without whitespace, 32-512 characters'; return 1;
      }
    fi
  done
}

demo_load_env() {
  set +x
  local file="$1" bytes clean_bytes line key raw value seen=' '
  demo_private_file "$file" || return
  bytes=$(wc -c < "$file") || return
  (( bytes <= 16384 )) || { demo_error '.env is too large'; return 1; }
  clean_bytes=$(tr -d '\000' < "$file" | wc -c) || return
  [[ "$bytes" -eq "$clean_bytes" ]] || { demo_error '.env contains a null byte'; return 1; }
  unset DEMO_ENV DEMO_PROJECT DEMO_DEPLOYMENT AZURE_SUBSCRIPTION_ID AZURE_LOCATION AZURE_NODE_VM_SIZE \
    AZURE_POSTGRES_SKU AZURE_POSTGRES_TIER \
    DEMO_KEY_VAULT DEMO_REVISION DEMO_KEY_MANAGEMENT DEMO_KEY_SHARED_CONTROL DEMO_KEY_SHARED_DATA \
    DEMO_KEY_ISOLATED_1_CONTROL DEMO_KEY_ISOLATED_1_DATA
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "$line" || "$line" == \#* ]] && continue
    [[ "$line" == *=* ]] || { demo_error 'Invalid .env assignment'; return 1; }
    key="${line%%=*}" raw="${line#*=}"
    case "$key" in
      DEMO_ENV|DEMO_PROJECT|DEMO_DEPLOYMENT|AZURE_SUBSCRIPTION_ID|AZURE_LOCATION|AZURE_NODE_VM_SIZE|AZURE_POSTGRES_SKU|AZURE_POSTGRES_TIER|DEMO_KEY_VAULT|DEMO_REVISION|\
      DEMO_KEY_MANAGEMENT|DEMO_KEY_SHARED_CONTROL|DEMO_KEY_SHARED_DATA|DEMO_KEY_ISOLATED_1_CONTROL|DEMO_KEY_ISOLATED_1_DATA) ;;
      *) demo_error 'Unknown .env key'; return 1 ;;
    esac
    [[ "$seen" != *" $key "* ]] || { demo_error 'Duplicate .env key'; return 1; }
    seen="$seen$key "
    raw="${raw#"${raw%%[![:space:]]*}"}"
    raw="${raw%"${raw##*[![:space:]]}"}"
    case "$raw" in
      \"*)
        value=$(printf '%s' "$raw" | jq -er \
          'if type == "string" and (explode | all(. >= 32 and . != 127)) then . else error("invalid value") end' 2>/dev/null) || {
          demo_error 'Invalid quoted .env value'; return 1;
        } ;;
      \'*)
        [[ ${#raw} -ge 2 && "$raw" == *\' ]] || { demo_error 'Invalid quoted .env value'; return 1; }
        value="${raw:1:${#raw}-2}"
        [[ "$value" != *\'* ]] || { demo_error 'Invalid quoted .env value'; return 1; } ;;
      *) value="$raw" ;;
    esac
    export "$key=$value"
  done < "$file"
  demo_validate_env
}
