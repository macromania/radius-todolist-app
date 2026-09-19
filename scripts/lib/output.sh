#!/usr/bin/env bash

demo_status() {
  local kind="$1" message="$2" color='' reset='' code marker='    ' entity detail text first
  local rule='------------------------------------------------------------------------------'
  case "${COLOR:-auto}" in
    auto|always|never) ;;
    *) printf 'Invalid COLOR. Use COLOR=auto, always, or never.\n' >&2; return 2 ;;
  esac
  case "$kind" in
    section) code='1;34' ;;
    progress) code=36 ;;
    success) code=32 ;;
    warning) code=33 ;;
    error) code=31 ;;
    *) printf 'Invalid status kind.\n' >&2; return 2 ;;
  esac
  if [[ -z "${NO_COLOR:-}" ]] && { [[ "${COLOR:-auto}" == always ]] ||
    { [[ "${COLOR:-auto}" == auto && -t 2 && "${TERM:-dumb}" != dumb ]]; }; }; then
    color=$(printf '\033[%sm' "$code")
    reset=$(printf '\033[0m')
  fi
  if [[ "$message" == *[[:cntrl:]]* ]]; then
    message=$(printf '%s' "$message" | LC_ALL=C tr '[:cntrl:]' ' ') || return
  fi
  text=$message
  if [[ "$message" == *': '* ]]; then
    entity="${message%%: *}" detail="${message#*: }"
    if [[ "$kind" == section ]]; then
      entity=$(printf '%s' "$entity" | tr '[:lower:]' '[:upper:]') || return
      first=$(printf '%s' "${detail:0:1}" | tr '[:lower:]' '[:upper:]') || return
      printf -v text '%s\n%s%s' "$entity" "$first" "${detail:1}"
    else
      printf -v text '%-26s  %s' "$entity" "$detail"
    fi
  fi
  if [[ "$kind" == section ]]; then
    printf '\n\n%s%s\n%s%s\n\n' "$color" "$text" "$rule" "$reset" >&2
    return
  fi
  case "$kind" in
    success)
      marker='OK  '
      [[ -z "$color" ]] || marker="$(printf '\342\234\205')  " ;;
    warning) marker='WARNING: ' ;;
    error) marker='ERROR: ' ;;
  esac
  printf '%s  %s%s%s\n' "$color" "$marker" "$text" "$reset" >&2
  if [[ "$kind" == success || "$kind" == error ]]; then
    printf '\n' >&2
  fi
  return 0
}

demo_run() (
  trap - EXIT ERR INT TERM
  label=$1
  shift
  demo_status progress "$label" || exit
  trap 'demo_status warning "$label interrupted; an external operation may still be running"; exit 130' INT
  trap 'demo_status warning "$label terminated; an external operation may still be running"; exit 143' TERM
  # The command remains in the foreground, preserving stdin, native logs and signals.
  set +e
  "$@"
  status=$?
  if (( status == 0 )); then
    demo_status success "$label completed"
  else
    demo_status error "$label failed (exit $status)"
  fi
  exit "$status"
)
