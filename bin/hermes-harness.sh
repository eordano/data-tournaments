#!/usr/bin/env bash
set -euo pipefail
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"

if [ -n "${HARNESS_SLOT:-}" ]; then
  LABEL="$1"; shift
  INPUTS=("$@")
  SLOT="$HARNESS_SLOT"
  OUTFILE="$HARNESS_OUTFILE"
  WINNER_FILE="$HARNESS_WINNER_FILE"
  CONFIG="$DATA_HOME/active-$SLOT.json"
  SERVER_NAME="tournament-slot-$SLOT"
else
  PARALLEL=1
  while [ $# -gt 0 ]; do
    case "$1" in
      -p|--parallelism) PARALLEL="$2"; shift 2 ;;
      --) shift; break ;;
      -*) echo "unknown flag: $1" >&2; exit 64 ;;
      *) break ;;
    esac
  done

  if [ "$#" -lt 2 ]; then
    echo "usage: $0 [-p PARALLELISM] <match-label> <file1> [file2 ...]" >&2
    exit 64
  fi

  LABEL="$1"; shift
  INPUTS=("$@")

  OUTFILE=$(mktemp -t hermes-submit.XXXXXX.md); rm -f "$OUTFILE"
  WINNER_FILE=$(mktemp -t hermes-winner.XXXXXX.json); rm -f "$WINNER_FILE"

  PICKED=""
  for i in $(seq 0 $((PARALLEL - 1))); do
    lockfile="$DATA_HOME/slot-$i.lock"
    if python3 -c "
import fcntl, sys
f = open('$lockfile', 'a+')
try:
    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(1)
sys.exit(0)
" 2>/dev/null; then
      PICKED="$i"
      break
    fi
  done
  [ -z "$PICKED" ] && PICKED=0

  export HARNESS_SLOT="$PICKED"
  export HARNESS_OUTFILE="$OUTFILE"
  export HARNESS_WINNER_FILE="$WINNER_FILE"
  exec python3 "$BIN_DIR/with_lock.py" "$DATA_HOME/slot-$PICKED.lock" \
    "$0" "$LABEL" "${INPUTS[@]}"
fi

N_INPUTS="${#INPUTS[@]}"
python3 - "$CONFIG" "$OUTFILE" "$WINNER_FILE" "$LABEL" "$N_INPUTS" \
  "${TOURNAMENT_TRACE_ID:-}" "${TOURNAMENT_PARENT_OBSERVATION_ID:-}" <<'PY'
import json, sys
config_path, outfile, winner_file, label, n_inputs, trace_id, parent_obs = sys.argv[1:8]
cfg = {
    "outfile": outfile,
    "winner_file": winner_file,
    "match_label": label,
    "n_inputs": int(n_inputs),
}
if trace_id:
    cfg["trace_id"] = trace_id
if parent_obs:
    cfg["parent_observation_id"] = parent_obs
with open(config_path, "w") as f:
    json.dump(cfg, f)
PY

if [ -n "${TOURNAMENT_PROMPT_OVERRIDE:-}" ]; then
  PROMPT="$TOURNAMENT_PROMPT_OVERRIDE"
else
  read -r -d '' PROMPT <<EOF || true
You are judging a match. Tools: read_file(path), pick_winner(winner_id, reasoning, markdown). No others.

### Inputs
$(i=1; for p in "${INPUTS[@]}"; do printf '  %d. %s\n' "$i" "$p"; i=$((i+1)); done)

Read each input fully via read_file. Decide which input wins based on the
tournament criteria, then call pick_winner with winner_id (1 or 2),
a short reasoning, and a synthesis markdown answer. Then stop.
EOF
fi

LOG="$DATA_HOME/sessions/$(basename "$OUTFILE").log"

HERMES_ARGS=(chat -q "$PROMPT" -Q -t "$SERVER_NAME" --yolo --max-turns 20 --source tool)

HERMES_CMD_RAW="${TOURNAMENT_HERMES_CMD:-nix run ~/projects/sandboxed-agents#hermes --}"
HERMES_CMD_RAW="${HERMES_CMD_RAW//\~\//$HOME/}"
# shellcheck disable=SC2206  # intentional word-splitting: argv from env string
HERMES_PREFIX=( $HERMES_CMD_RAW )

LANGFUSE_PUBLIC_KEY="${LANGFUSE_PUBLIC_KEY:-}" \
LANGFUSE_SECRET_KEY="${LANGFUSE_SECRET_KEY:-}" \
LANGFUSE_HOST="${LANGFUSE_HOST:-https://cloud.langfuse.com}" \
"${HERMES_PREFIX[@]}" "${HERMES_ARGS[@]}" \
  >"$LOG" 2>&1 || { echo "ERROR: hermes chat failed (exit $?)" >&2; tail -40 "$LOG" >&2; exit 3; }

echo '{}' > "$CONFIG"

if [ ! -s "$OUTFILE" ]; then
  echo "ERROR: agent never called pick_winner() — OUTFILE empty (slot $SLOT)" >&2
  tail -40 "$LOG" >&2
  exit 1
fi
if [ ! -s "$WINNER_FILE" ]; then
  echo "ERROR: agent never set winner — WINNER_FILE empty (slot $SLOT)" >&2
  tail -40 "$LOG" >&2
  exit 1
fi

SUBMIT=$(cat "$OUTFILE")

if [ -n "${TOURNAMENT_REQUIRED_SECTIONS:-}" ]; then
  IFS=$'\x1f' read -ra REQUIRED <<<"$TOURNAMENT_REQUIRED_SECTIONS"
else
  REQUIRED=(
    "## Shared patterns" "## Divergent patterns" "## Naming & exports"
    "## Validation" "## Error handling" "## Auth & session"
    "## Return shapes" "## Database access" "## Other conventions"
    "## Guideline candidates"
  )
fi
MISSING=()
for s in "${REQUIRED[@]}"; do
  printf '%s' "$SUBMIT" | grep -qiF "$s" || MISSING+=("$s")
done
if [ "${#MISSING[@]}" -gt 0 ]; then
  echo "ERROR: missing required sections:" >&2
  printf '  - %s\n' "${MISSING[@]}" >&2
  exit 2
fi

printf '%s\n' "$SUBMIT"
