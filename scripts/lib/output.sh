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
  started=$SECONDS
  umask 077
  timer_dir=$(mktemp -d "${TMPDIR:-/tmp}/plane-progress.XXXXXX") || exit
  [[ "$timer_dir" == /* ]] || timer_dir="$PWD/$timer_dir"
  progress_dir="${PLANE_DEMO_PROGRESS_DIR:-$timer_dir}"
  owns_progress_dir=false
  [[ -n "${PLANE_DEMO_PROGRESS_DIR:-}" ]] || owns_progress_dir=true
  heartbeat_gate="$progress_dir/heartbeat"
  monitor='' timer_open=false
  finish_progress() {
    local result=0
    if [[ "$timer_open" == true ]]; then
      printf '\n' >&9 || result=1
      [[ -z "$monitor" ]] || wait "$monitor" || result=1
      exec 9>&-
      timer_open=false
    fi
    if [[ "$owns_progress_dir" == true && ( -e "$heartbeat_gate" || -L "$heartbeat_gate" ) ]]; then
      rmdir -- "$heartbeat_gate" || result=1
    fi
    rm -f -- "$timer_dir/timer" || result=1
    rmdir -- "$timer_dir" || result=1
    return "$result"
  }
  trap finish_progress EXIT
  trap 'demo_status warning "$label interrupted; an external operation may still be running"; finish_progress; trap - EXIT; exit 130' INT
  trap 'demo_status warning "$label terminated; an external operation may still be running"; finish_progress; trap - EXIT; exit 143' TERM
  if [[ "$progress_dir" != /* || "${progress_dir##*/}" != plane-progress.* \
    || ! -d "$progress_dir" || -L "$progress_dir" || ! -O "$progress_dir" ]]; then
    demo_status error 'Invalid progress coordination directory'
    exit 1
  fi
  if [[ "$owns_progress_dir" == false ]]; then
    permissions=$(LC_ALL=C ls -ld "$progress_dir") || exit
    [[ "${permissions:0:10}" == 'drwx------' ]] || {
      demo_status error 'Progress coordination directory must be private'; exit 1;
    }
  fi
  export PLANE_DEMO_PROGRESS_DIR="$progress_dir"
  mkfifo "$timer_dir/timer" || exit
  exec 9<> "$timer_dir/timer" || exit
  timer_open=true
  # A timed read needs no child sleep process. Writing to this private FIFO
  # stops the monitor even when the command finishes before the monitor starts.
  (
    set +e
    trap - EXIT ERR INT TERM
    gate_held=false
    # shellcheck disable=SC2329
    release_heartbeat() {
      local result=$?
      if [[ "$gate_held" == true ]]; then
        rmdir -- "$heartbeat_gate" || result=1
      fi
      exit "$result"
    }
    trap 'release_heartbeat' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    while :; do
      IFS= read -r -t 15 -u 9 _
      timer_status=$?
      if (( timer_status == 0 )); then exit 0; fi
      # Bash 3.2 reports a read timeout as 1; newer Bash returns greater than 128.
      if (( timer_status != 1 && timer_status <= 128 )); then
        demo_status warning "$label: progress timer failed (exit $timer_status)"
        exit 1
      fi
      # Prompts hold the same gate, so no ancestor timer can print into user input.
      if gate_error=$(LC_ALL=C mkdir -m 700 -- "$heartbeat_gate" 2>&1); then
        gate_held=true
        demo_status progress "$label: $((SECONDS - started))s elapsed" || exit
        gate_held=false
        rmdir -- "$heartbeat_gate" || exit
      else
        case "$gate_error" in
          *': File exists')
            if [[ -L "$heartbeat_gate" || -f "$heartbeat_gate" || -p "$heartbeat_gate" \
              || -S "$heartbeat_gate" || -b "$heartbeat_gate" || -c "$heartbeat_gate" ]]; then
              demo_status error "$label: invalid progress coordination gate"
              exit 1
            fi ;;
          *) printf '%s\n' "$gate_error" >&2; exit 1 ;;
        esac
      fi
    done
  ) &
  monitor=$!
  # The command remains in the foreground, preserving stdin, native logs and signals.
  set +e
  "$@" 9>&-
  status=$?
  if (( status == 0 )); then
    demo_status success "$label completed ($((SECONDS - started))s)"
  else
    demo_status error "$label failed (exit $status, $((SECONDS - started))s)"
  fi
  # Bash does not reliably run a subshell's EXIT trap inside a caller's EXIT
  # handler. Clean up explicitly as well as handling early exits and signals.
  if ! finish_progress; then
    demo_status error "$label: progress helper cleanup incomplete"
    (( status != 0 )) || status=1
  fi
  trap - EXIT
  exit "$status"
)
