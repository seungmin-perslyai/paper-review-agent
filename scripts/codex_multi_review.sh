#!/usr/bin/env bash
set -eo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ORCH_SCRIPT="$ROOT_DIR/scripts/persly_review_orchestrator.py"
RICH_DASHBOARD_SCRIPT="$ROOT_DIR/scripts/persly_rich_dashboard.py"

OUTPUT_DIR="$ROOT_DIR/outputs"
UI_LANG="${UI_LANG:-}"
UI_LANG_FROM_CLI=""
ACTIVE_AGENTS="${PERSLY_ACTIVE_AGENTS:-summary,prior_work,method,critique}"
ACTIVE_AGENTS_FROM_CLI=""
SUPERVISOR_COMMAND="${PERSLY_SUPERVISOR_COMMAND:-}"
SUPERVISOR_COMMAND_FROM_CLI=""
AUTO_YES=0
NO_COLOR_FLAG=0
FORCE_RICH_DASHBOARD=0
DISABLE_RICH_DASHBOARD=0
FORCE_TMUX_DASHBOARD=0
DISABLE_TMUX_DASHBOARD=0
RUN_ID="${PERSLY_RUN_ID:-}"
RUN_LOG_DIR="${PERSLY_LOG_DIR:-}"

declare -a ORCH_ARGS=()

usage() {
  cat <<'EOF'
Usage:
  bash scripts/codex_multi_review.sh [options] --papers <path> [--papers <path> ...]

Options:
  --papers <path>       Paper file or directory (repeatable)
  --output <dir>        Output directory (default: ./outputs)
  --config <env-file>   Agent model/runtime env file (default: ./config/codex_agents.env)
  --prompt-dir <dir>    Prompt template directory (default: ./prompts)
  --max-parallel <n>    Concurrent paper review count override
  --max-revisions <n>   Supervisor revision rounds override
  --max-chars <n>       Max extracted paper chars per paper
  --continue-added      Review only new/unfinished papers from current file list
  --supervisor-command  Natural-language supervisor command
  --lang <ko|en>        UI/output language
  --active-agents <csv> Enable sub-agents (summary,prior_work,method,critique)
  --rich-dashboard      Force Rich split dashboard mode
  --no-rich-dashboard   Disable Rich dashboard
  --tmux-dashboard      Force tmux split dashboard mode
  --no-tmux-dashboard   Disable tmux dashboard
  --yes                 Run without confirmation prompt
  --no-color            Disable color output
  --verbose-ui          Print detailed runtime logs
  --help                Show help

Supported paper types: .txt, .md, .pdf
For PDF input, `pdftotext` must be installed.
EOF
}

require_command() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    printf '%s\n' "Required command not found: $cmd" >&2
    exit 1
  fi
}

detect_default_language() {
  local locale="${LC_ALL:-${LANG:-}}"
  if [[ "$locale" == ko* || "$locale" == *"KR"* ]]; then
    printf 'ko'
  else
    printf 'en'
  fi
}

validate_ui_language() {
  case "$1" in
    ko|en) return 0 ;;
    *) return 1 ;;
  esac
}

trim_whitespace() {
  local text="$1"
  text="${text#"${text%%[![:space:]]*}"}"
  text="${text%"${text##*[![:space:]]}"}"
  printf '%s' "$text"
}

validate_agent_name() {
  case "$1" in
    summary|prior_work|method|critique) return 0 ;;
    *) return 1 ;;
  esac
}

normalize_active_agents_csv() {
  local raw="$1"
  local -a parts=()
  local -a normalized=()
  local seen=","
  local part item

  IFS=',' read -r -a parts <<< "$raw"
  for part in "${parts[@]}"; do
    item="$(trim_whitespace "$part")"
    item="$(printf '%s' "$item" | tr '[:upper:]' '[:lower:]')"
    [[ -z "$item" ]] && continue
    if ! validate_agent_name "$item"; then
      return 1
    fi
    if [[ "$seen" == *",$item,"* ]]; then
      continue
    fi
    seen+="$item,"
    normalized+=("$item")
  done

  if [[ "${#normalized[@]}" -eq 0 ]]; then
    return 1
  fi
  local joined
  joined="$(IFS=,; printf '%s' "${normalized[*]}")"
  printf '%s' "$joined"
}

ui_select_language() {
  local -a codes=("ko" "en")
  local -a labels=("Korean (한국어)" "English")
  local focus=0
  local selected=-1
  local key esc
  local notice=""
  local i cursor mark

  if [[ "$UI_LANG" == "en" ]]; then
    focus=1
    selected=1
  elif [[ "$UI_LANG" == "ko" ]]; then
    focus=0
    selected=0
  fi

  while true; do
    printf '\033[2J\033[H'
    printf '%s\n' "+-----------------------------------------------+"
    printf '%s\n' "| LANGUAGE SELECT                               |"
    printf '%s\n' "| SPACE: check/uncheck | ENTER: run             |"
    printf '%s\n' "| Move: ↑/↓ (or k/j)                            |"
    printf '%s\n' "+-----------------------------------------------+"

    for i in "${!codes[@]}"; do
      cursor=" "
      mark=" "
      if [[ "$focus" -eq "$i" ]]; then
        cursor=">"
      fi
      if [[ "$selected" -eq "$i" ]]; then
        mark="✓"
      fi
      printf '| %s [%s] %s\n' "$cursor" "$mark" "${labels[$i]}"
    done
    if [[ -n "$notice" ]]; then
      printf '| %s\n' "$notice"
    else
      printf '%s\n' "|                                               |"
    fi
    printf '%s\n' "+-----------------------------------------------+"

    IFS= read -rsn1 key || break
    case "$key" in
      $'\x1b')
        IFS= read -rsn2 esc || esc=""
        case "$esc" in
          "[A")
            focus=$(((focus + ${#codes[@]} - 1) % ${#codes[@]}))
            ;;
          "[B")
            focus=$(((focus + 1) % ${#codes[@]}))
            ;;
          *)
            ;;
        esac
        ;;
      k|K)
        focus=$(((focus + ${#codes[@]} - 1) % ${#codes[@]}))
        ;;
      j|J)
        focus=$(((focus + 1) % ${#codes[@]}))
        ;;
      " ")
        if [[ "$selected" -eq "$focus" ]]; then
          selected=-1
          notice="No language selected. Press SPACE to check one."
        else
          selected="$focus"
          notice=""
        fi
        ;;
      $'\n'|$'\r'|"")
        if [[ "$selected" -lt 0 ]]; then
          notice="Select one language with SPACE before ENTER."
          continue
        fi
        UI_LANG="${codes[$selected]}"
        printf '\033[2J\033[H'
        return
        ;;
      *)
        ;;
    esac
  done
}

ui_select_active_agents() {
  local -a codes=("summary" "prior_work" "method" "critique")
  local -a labels=("Summary agent" "Prior-work agent" "Method agent" "Critique agent")
  local -a checked=(0 0 0 0)
  local focus=0
  local key esc
  local notice=""
  local i cursor mark selected_count
  local selected_csv=""

  for i in "${!codes[@]}"; do
    if [[ ",$ACTIVE_AGENTS," == *",${codes[$i]},"* ]]; then
      checked[$i]=1
    fi
  done

  while true; do
    printf '\033[2J\033[H'
    printf '%s\n' "+-----------------------------------------------+"
    printf '%s\n' "| AGENT SELECT                                  |"
    printf '%s\n' "| SPACE: check/uncheck | ENTER: run             |"
    printf '%s\n' "| Move: ↑/↓ (or k/j)                            |"
    printf '%s\n' "+-----------------------------------------------+"

    for i in "${!codes[@]}"; do
      cursor=" "
      mark=" "
      if [[ "$focus" -eq "$i" ]]; then
        cursor=">"
      fi
      if [[ "${checked[$i]}" -eq 1 ]]; then
        mark="✓"
      fi
      printf '| %s [%s] %-13s (%s)\n' "$cursor" "$mark" "${codes[$i]}" "${labels[$i]}"
    done

    if [[ -n "$notice" ]]; then
      printf '| %s\n' "$notice"
    else
      printf '%s\n' "|                                               |"
    fi
    printf '%s\n' "+-----------------------------------------------+"

    IFS= read -rsn1 key || break
    case "$key" in
      $'\x1b')
        IFS= read -rsn2 esc || esc=""
        case "$esc" in
          "[A")
            focus=$(((focus + ${#codes[@]} - 1) % ${#codes[@]}))
            ;;
          "[B")
            focus=$(((focus + 1) % ${#codes[@]}))
            ;;
          *)
            ;;
        esac
        ;;
      k|K)
        focus=$(((focus + ${#codes[@]} - 1) % ${#codes[@]}))
        ;;
      j|J)
        focus=$(((focus + 1) % ${#codes[@]}))
        ;;
      " ")
        if [[ "${checked[$focus]}" -eq 1 ]]; then
          checked[$focus]=0
        else
          checked[$focus]=1
        fi
        notice=""
        ;;
      $'\n'|$'\r'|"")
        selected_count=0
        selected_csv=""
        for i in "${!codes[@]}"; do
          if [[ "${checked[$i]}" -eq 1 ]]; then
            selected_count=$((selected_count + 1))
            if [[ -n "$selected_csv" ]]; then
              selected_csv+=","
            fi
            selected_csv+="${codes[$i]}"
          fi
        done
        if [[ "$selected_count" -eq 0 ]]; then
          notice="Select at least one agent before ENTER."
          continue
        fi
        ACTIVE_AGENTS="$selected_csv"
        printf '\033[2J\033[H'
        return
        ;;
      *)
        ;;
    esac
  done
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --papers|--config|--prompt-dir|--max-parallel|--max-revisions|--max-chars)
        if [[ $# -lt 2 ]]; then
          printf '%s\n' "$1 requires a value." >&2
          exit 1
        fi
        ORCH_ARGS+=("$1" "$2")
        if [[ "$1" == "--config" ]]; then
          :
        fi
        shift 2
        ;;
      --supervisor-command)
        if [[ $# -lt 2 ]]; then
          printf '%s\n' "--supervisor-command requires a value." >&2
          exit 1
        fi
        SUPERVISOR_COMMAND_FROM_CLI="$2"
        ORCH_ARGS+=("$1" "$2")
        shift 2
        ;;
      --output)
        if [[ $# -lt 2 ]]; then
          printf '%s\n' "--output requires a value." >&2
          exit 1
        fi
        OUTPUT_DIR="$2"
        ORCH_ARGS+=("$1" "$2")
        shift 2
        ;;
      --lang)
        if [[ $# -lt 2 ]]; then
          printf '%s\n' "--lang requires a value (ko|en)." >&2
          exit 1
        fi
        UI_LANG_FROM_CLI="$2"
        ORCH_ARGS+=("$1" "$2")
        shift 2
        ;;
      --active-agents)
        if [[ $# -lt 2 ]]; then
          printf '%s\n' "--active-agents requires csv value." >&2
          exit 1
        fi
        ACTIVE_AGENTS_FROM_CLI="$2"
        ORCH_ARGS+=("$1" "$2")
        shift 2
        ;;
      --run-id)
        if [[ $# -lt 2 ]]; then
          printf '%s\n' "--run-id requires a value." >&2
          exit 1
        fi
        RUN_ID="$2"
        shift 2
        ;;
      --run-log-dir)
        if [[ $# -lt 2 ]]; then
          printf '%s\n' "--run-log-dir requires a value." >&2
          exit 1
        fi
        RUN_LOG_DIR="$2"
        shift 2
        ;;
      --continue-added|--yes|--verbose-ui)
        ORCH_ARGS+=("$1")
        if [[ "$1" == "--yes" ]]; then
          AUTO_YES=1
        fi
        shift
        ;;
      --no-color)
        NO_COLOR_FLAG=1
        shift
        ;;
      --rich-dashboard)
        FORCE_RICH_DASHBOARD=1
        shift
        ;;
      --no-rich-dashboard)
        DISABLE_RICH_DASHBOARD=1
        shift
        ;;
      --tmux-dashboard)
        FORCE_TMUX_DASHBOARD=1
        shift
        ;;
      --no-tmux-dashboard)
        DISABLE_TMUX_DASHBOARD=1
        shift
        ;;
      --help|-h)
        usage
        exit 0
        ;;
      *)
        ORCH_ARGS+=("$1")
        shift
        ;;
    esac
  done
}

contains_arg() {
  local needle="$1"
  local item
  for item in "${ORCH_ARGS[@]}"; do
    if [[ "$item" == "$needle" ]]; then
      return 0
    fi
  done
  return 1
}

init_runtime_values() {
  local interactive_picker=0
  local normalized_agents=""
  local normalized_command=""

  if [[ -n "$UI_LANG_FROM_CLI" ]]; then
    if ! validate_ui_language "$UI_LANG_FROM_CLI"; then
      printf '%s\n' "--lang must be ko or en." >&2
      exit 1
    fi
    UI_LANG="$UI_LANG_FROM_CLI"
  elif validate_ui_language "$UI_LANG"; then
    :
  else
    UI_LANG="$(detect_default_language)"
  fi

  if [[ -n "$ACTIVE_AGENTS_FROM_CLI" ]]; then
    normalized_agents="$(normalize_active_agents_csv "$ACTIVE_AGENTS_FROM_CLI" || true)"
    if [[ -z "$normalized_agents" ]]; then
      printf '%s\n' "--active-agents must include one or more of: summary,prior_work,method,critique" >&2
      exit 1
    fi
    ACTIVE_AGENTS="$normalized_agents"
  else
    normalized_agents="$(normalize_active_agents_csv "$ACTIVE_AGENTS" || true)"
    if [[ -z "$normalized_agents" ]]; then
      ACTIVE_AGENTS="summary,prior_work,method,critique"
    else
      ACTIVE_AGENTS="$normalized_agents"
    fi
  fi

  if [[ -n "$SUPERVISOR_COMMAND_FROM_CLI" ]]; then
    SUPERVISOR_COMMAND="$SUPERVISOR_COMMAND_FROM_CLI"
  fi
  normalized_command="$(trim_whitespace "$SUPERVISOR_COMMAND")"
  SUPERVISOR_COMMAND="$normalized_command"

  if [[ "$AUTO_YES" -ne 1 && -t 0 && -t 1 && "${PERSLY_RICH_CHILD:-0}" != "1" && "${PERSLY_TMUX_CHILD:-0}" != "1" ]]; then
    interactive_picker=1
  fi

  if [[ "$interactive_picker" -eq 1 && -z "$UI_LANG_FROM_CLI" ]]; then
    ui_select_language
  fi
  if ! contains_arg "--lang"; then
    ORCH_ARGS+=("--lang" "$UI_LANG")
  fi

  if [[ "$interactive_picker" -eq 1 && -z "$ACTIVE_AGENTS_FROM_CLI" ]]; then
    ui_select_active_agents
  fi
  if ! contains_arg "--active-agents"; then
    ORCH_ARGS+=("--active-agents" "$ACTIVE_AGENTS")
  fi

  if ! contains_arg "--supervisor-command" && [[ -n "$SUPERVISOR_COMMAND" ]]; then
    ORCH_ARGS+=("--supervisor-command" "$SUPERVISOR_COMMAND")
  fi

  if [[ -z "$RUN_ID" ]]; then
    RUN_ID="$(date '+%Y%m%d_%H%M%S')"
  fi

  if [[ -z "$RUN_LOG_DIR" ]]; then
    RUN_LOG_DIR="$OUTPUT_DIR/$RUN_ID/.run_logs"
  fi

  mkdir -p "$RUN_LOG_DIR"

  if [[ "$NO_COLOR_FLAG" -eq 1 ]]; then
    export NO_COLOR=1
  fi

  export UI_LANG
  export PERSLY_ACTIVE_AGENTS="$ACTIVE_AGENTS"
  export PERSLY_SUPERVISOR_COMMAND="$SUPERVISOR_COMMAND"
  export PERSLY_RUN_ID="$RUN_ID"
}

has_rich_dashboard_support() {
  if [[ ! -f "$RICH_DASHBOARD_SCRIPT" ]]; then
    return 1
  fi
  uv run --project "$ROOT_DIR" python - <<'PY' >/dev/null 2>&1
import importlib.util
import sys
sys.exit(0 if importlib.util.find_spec("rich") else 1)
PY
}

should_use_rich_dashboard() {
  if [[ "${PERSLY_RICH_CHILD:-0}" == "1" || "${PERSLY_TMUX_CHILD:-0}" == "1" ]]; then
    return 1
  fi
  if [[ "$DISABLE_RICH_DASHBOARD" -eq 1 ]]; then
    return 1
  fi
  if [[ "$FORCE_TMUX_DASHBOARD" -eq 1 ]]; then
    return 1
  fi
  if [[ ! -t 0 || ! -t 1 ]]; then
    return 1
  fi
  has_rich_dashboard_support
}

should_use_tmux_dashboard() {
  if [[ "${PERSLY_RICH_CHILD:-0}" == "1" || "${PERSLY_TMUX_CHILD:-0}" == "1" ]]; then
    return 1
  fi
  if [[ "$DISABLE_TMUX_DASHBOARD" -eq 1 ]]; then
    return 1
  fi
  if [[ "$FORCE_RICH_DASHBOARD" -eq 1 ]]; then
    return 1
  fi
  if [[ "$FORCE_TMUX_DASHBOARD" -ne 1 ]]; then
    return 1
  fi
  if [[ ! -t 0 || ! -t 1 ]]; then
    return 1
  fi
  command -v tmux >/dev/null 2>&1
}

build_child_args() {
  local force_yes="${1:-0}"
  local has_yes=0
  local item
  local -a args=("${ORCH_ARGS[@]}")

  for item in "${args[@]}"; do
    if [[ "$item" == "--yes" ]]; then
      has_yes=1
      break
    fi
  done
  if [[ "$has_yes" -eq 0 && "$force_yes" -eq 1 ]]; then
    args+=("--yes")
  fi

  args+=("--run-id" "$RUN_ID" "--run-log-dir" "$RUN_LOG_DIR")
  printf '%s\n' "${args[@]}"
}

run_orchestrator() {
  local -a cmd=(
    uv run --project "$ROOT_DIR" python "$ORCH_SCRIPT"
  )
  local item

  for item in "${ORCH_ARGS[@]}"; do
    cmd+=("$item")
  done

  cmd+=("--run-id" "$RUN_ID" "--run-log-dir" "$RUN_LOG_DIR")

  exec "${cmd[@]}"
}

launch_rich_dashboard() {
  local script_path
  local -a child_args=()
  local -a cmd=()

  script_path="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
  while IFS= read -r line; do
    child_args+=("$line")
  done < <(build_child_args 0)

  cmd=(
    uv run --project "$ROOT_DIR" python "$RICH_DASHBOARD_SCRIPT"
    --script "$script_path"
    --run-log-dir "$RUN_LOG_DIR"
    --run-id "$RUN_ID"
    --ui-lang "$UI_LANG"
  )

  local item
  for item in "${child_args[@]}"; do
    cmd+=("--child-arg=$item")
  done

  "${cmd[@]}"
}

launch_tmux_dashboard() {
  local script_path session_name child_cmd
  local -a child_args=()
  local sup_cmd pri_cmd sum_cmd met_cmd cri_cmd
  local sup_pane sum_pane pri_pane met_pane cri_pane

  script_path="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
  session_name="persly-paper-review-${RUN_ID}"

  while IFS= read -r line; do
    child_args+=("$line")
  done < <(build_child_args 0)

  child_cmd="PERSLY_TMUX_CHILD=1 PERSLY_LOG_DIR=$(printf '%q' "$RUN_LOG_DIR") PERSLY_RUN_ID=$(printf '%q' "$RUN_ID") bash $(printf '%q' "$script_path")"
  local arg
  for arg in "${child_args[@]}"; do
    child_cmd+=" $(printf '%q' "$arg")"
  done

  sup_cmd="$child_cmd"
  pri_cmd="tail -n 240 -F $(printf '%q' "$RUN_LOG_DIR/prior_work.log")"
  sum_cmd="tail -n 240 -F $(printf '%q' "$RUN_LOG_DIR/summary.log")"
  met_cmd="tail -n 240 -F $(printf '%q' "$RUN_LOG_DIR/method.log")"
  cri_cmd="tail -n 240 -F $(printf '%q' "$RUN_LOG_DIR/critique.log")"

  tmux new-session -d -s "$session_name" -n review "$sup_cmd"
  sup_pane="$(tmux display-message -p -t "$session_name:0.0" '#{pane_id}')"
  sum_pane="$(tmux split-window -h -t "$sup_pane" -p 50 -P -F '#{pane_id}' "$sum_cmd")"
  pri_pane="$(tmux split-window -v -t "$sup_pane" -p 35 -P -F '#{pane_id}' "$pri_cmd")"
  met_pane="$(tmux split-window -v -t "$sum_pane" -p 67 -P -F '#{pane_id}' "$met_cmd")"
  cri_pane="$(tmux split-window -v -t "$met_pane" -p 50 -P -F '#{pane_id}' "$cri_cmd")"

  tmux select-pane -t "$sup_pane" -T "SUPERVISOR"
  tmux select-pane -t "$pri_pane" -T "PRIOR_WORK"
  tmux select-pane -t "$sum_pane" -T "SUMMARY"
  tmux select-pane -t "$met_pane" -T "METHOD"
  tmux select-pane -t "$cri_pane" -T "CRITIQUE"
  tmux select-pane -t "$sup_pane"
  tmux set-option -t "$session_name" mouse on >/dev/null
  tmux set-option -t "$session_name" status on >/dev/null
  tmux set-option -t "$session_name" status-left-length 120 >/dev/null
  tmux set-option -t "$session_name" status-right-length 60 >/dev/null
  tmux set-option -t "$session_name" status-left " PERSLY - Research Agent Console " >/dev/null
  tmux set-option -t "$session_name" status-right "run:$RUN_ID" >/dev/null
  tmux set-window-option -t "$session_name:0" pane-border-status top >/dev/null
  tmux set-window-option -t "$session_name:0" pane-border-format " #T " >/dev/null

  tmux attach-session -t "$session_name"
  return $?
}

main() {
  require_command uv

  if [[ ! -f "$ORCH_SCRIPT" ]]; then
    printf '%s\n' "Orchestrator script not found: $ORCH_SCRIPT" >&2
    exit 1
  fi

  parse_args "$@"
  init_runtime_values

  if [[ "$FORCE_RICH_DASHBOARD" -eq 1 && "$FORCE_TMUX_DASHBOARD" -eq 1 ]]; then
    printf '%s\n' "Use either --rich-dashboard or --tmux-dashboard, not both." >&2
    exit 1
  fi

  if [[ "$FORCE_RICH_DASHBOARD" -eq 1 && "$DISABLE_RICH_DASHBOARD" -eq 1 ]]; then
    printf '%s\n' "Use either --rich-dashboard or --no-rich-dashboard, not both." >&2
    exit 1
  fi

  if [[ "$FORCE_TMUX_DASHBOARD" -eq 1 && "$DISABLE_TMUX_DASHBOARD" -eq 1 ]]; then
    printf '%s\n' "Use either --tmux-dashboard or --no-tmux-dashboard, not both." >&2
    exit 1
  fi

  if [[ "${PERSLY_RICH_CHILD:-0}" == "1" || "${PERSLY_TMUX_CHILD:-0}" == "1" ]]; then
    run_orchestrator
  fi

  if [[ "$FORCE_RICH_DASHBOARD" -eq 1 ]]; then
    if ! has_rich_dashboard_support; then
      printf '%s\n' "Rich dashboard unavailable. Run: cd $ROOT_DIR && uv sync" >&2
      exit 1
    fi
    launch_rich_dashboard
    return
  fi

  if should_use_rich_dashboard; then
    launch_rich_dashboard
    return
  fi

  if should_use_tmux_dashboard; then
    launch_tmux_dashboard
    return
  fi

  run_orchestrator
}

main "$@"
