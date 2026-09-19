#!/usr/bin/env bash

demo_status() {
  local kind="$1" message="$2" color='' reset='' code marker='' detail text first columns size
  local styled=false rule='--------------------------------------------'
  case "${COLOR:-auto}" in
    auto|always|never) ;;
    *) printf 'Invalid COLOR. Use COLOR=auto, always, or never.\n' >&2; return 2 ;;
  esac
  case "$kind" in
    title|section) code=1 ;;
    phase) code='1;34' ;;
    detail|next|progress) code='' ;;
    success) code=32 ;;
    warning) code=33 ;;
    error) code=31 ;;
    *) printf 'Invalid status kind.\n' >&2; return 2 ;;
  esac
  if [[ -z "${NO_COLOR:-}" ]] && { [[ "${COLOR:-auto}" == always ]] ||
    { [[ "${COLOR:-auto}" == auto && -t 2 && "${TERM:-dumb}" != dumb ]]; }; }; then
    styled=true
    if [[ -n "$code" ]]; then
      color=$(printf '\033[%sm' "$code")
      reset=$(printf '\033[0m')
    fi
  fi
  if [[ "$message" == *[[:cntrl:]]* ]]; then
    message=$(printf '%s' "$message" | LC_ALL=C tr '[:cntrl:]' ' ') || return
  fi
  text=$message
  case "$kind" in
    title) printf '\n%s%s%s\n' "$color" "$text" "$reset" >&2; return ;;
    detail) printf '%s\n' "$text" >&2; return ;;
    next) printf '  Next: %s\n' "$text" >&2; return ;;
    phase)
      columns=${COLUMNS:-80}
      if [[ -t 2 ]] && size=$(stty size <&2 2>/dev/null); then columns="${size##* }"; fi
      [[ "$columns" =~ ^[1-9][0-9]{0,3}$ ]] || columns=80
      printf '\n%s%s%s\n%s\n' "$color" "$text" "$reset" "${rule:0:columns}" >&2
      return ;;
    section)
      if [[ "$message" == *': '* ]]; then
        detail="${message#*: }"
        first=$(printf '%s' "${detail:0:1}" | tr '[:lower:]' '[:upper:]') || return
        text="$first${detail:1}"
      fi
      printf '\n  %s%s%s\n' "$color" "$text" "$reset" >&2
      return ;;
  esac
  case "$kind" in
    success)
      marker='OK  '
      [[ "$styled" == false ]] || marker="$(printf '\342\234\223') " ;;
    warning) marker='WARNING: ' ;;
    error) marker='ERROR: ' ;;
  esac
  printf '  %s%s%s%s\n' "$color" "$marker" "$reset" "$text" >&2
  return 0
}

demo_phase() {
  local number=$1
  shift
  if [[ ! "$number" =~ ^[1-9][0-9]*$ ]] || (( number > $# )); then
    demo_status error 'Runbook phase is outside the workflow'; return 2
  fi
  local steps=("$@")
  demo_status phase "$number / $#  ${steps[number-1]}" || return
  if (( number < $# )); then demo_status next "${steps[number]}"; fi
}

demo_run() (
  trap - EXIT ERR INT TERM
  announce=true
  if [[ "${1:-}" == --summary-only ]]; then announce=false; shift; fi
  label=$1
  shift
  if [[ "$announce" == true ]]; then demo_status progress "$label" || exit; fi
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
