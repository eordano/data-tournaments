#!/bin/sh
set -eu

WO_FILE="${AGENT_WORKORDER_FILE:?AGENT_WORKORDER_FILE must point at the WorkOrder markdown}"
AGENT="${AGENT_CLI:-claude-achtung-achtung}"

PROMPT="You are fixing one bug in this Rust workspace (you are on branch ${BRANCH_NAME} at base ${BASE_SHA}).

WORK ORDER (${WORKORDER_REF}):
$(cat "$WO_FILE")

HARD RULES:
- Fix the bug in crates/catalyrst-hashing/src/verify.rs ONLY. Do not touch
  red.sh, green.sh, guard.sh, any tests/ file, Cargo.toml, Cargo.lock, or
  any other file: the validation harness is trusted-at-base and any edit
  to it is auto-refused as tampering.
- Acceptance: ./red.sh must print 'RED 1/1', ./green.sh 'GREEN 30/30',
  ./guard.sh 'GUARD 5/5' (exact-length guards + upstream @dcl/hashing
  oracle parity). Run them to check your work (they need
  CARGO_TARGET_DIR/HOME/CATALYRST_HASHING_FIXTURES already set in your
  environment).
- Make the change minimal and root-caused. Do NOT commit — the platform
  commits for you."

exec "$AGENT" -p "$PROMPT" --allowedTools "Bash,Read,Edit,Write" --max-turns 25
