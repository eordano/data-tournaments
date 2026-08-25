#!/usr/bin/env bash
# End-to-end driver for the branch-fix loop showcase (wave-9 E2).
#
# Prereqs: branch-fix spine landed (05562e2), UI landed (5758b57),
# Temporal dev server on :7233 for the post-decision stage.
#
# Usage: run-e2e.sh <data-home> <fixture-repo-dir>
# Produces: artifacts under docs/showcases/branch-fix-loop/artifacts/
#
# HONEST-STATUS DISCIPLINE: every step prints a REAL/FIXTURE/DRY-RUN tag.
# set -e: a partial run must never masquerade as a full one.
set -euo pipefail

HOME_DIR="${1:?usage: run-e2e.sh <data-home> <fixture-repo>}"
REPO="${2:?usage: run-e2e.sh <data-home> <fixture-repo>}"
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
ART="$ROOT/docs/showcases/branch-fix-loop/artifacts"
mkdir -p "$ART"

export DATA_TOURNAMENTS_HOME="$HOME_DIR" PROMPT_BACKEND=local
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
cd "$ROOT"

jqget() { python3 -c "import json,sys; print(json.load(sys.stdin)$1)"; }
step() { echo; echo "━━ $1"; }

step "0. init + landscape seed + policies [REAL LOCAL]"
python3 bin/catalog.py init
python3 bin/catalog.py create-project --name bfl \
  --description "branch-fix loop: engineers validate each fix branch in isolation"
python3 bin/campaigns.py create-campaign --project bfl --name bfl-retry --kind bugsweep \
  --objective "Fix the carried-deadline retry bug; validate every fix branch independently" \
  --base-commit "$(git -C "$REPO" rev-parse main)"
# Developer decisions on branches + release approvals — both fail-closed
# until these policies exist (proven live in the baseline run).
python3 bin/catalog.py create-policy --name branchfix-approvals --kind approval \
  --rule '{"approvers": ["esteban"], "scope": "branchfix:*"}'
python3 bin/catalog.py create-policy --name release-approvals --kind approval \
  --rule '{"approvers": ["esteban"], "scope": "release:*"}'

step "1. finding + dossier [REAL LOCAL]"
FINDING_ID=$(python3 bin/campaigns.py create-finding --campaign bfl-retry \
  --slug retry-carried-deadline \
  --title "Retry reuses attempt-1 deadline; attempt 2 starved and unlogged" \
  --source-kind sentry-csv \
  --root-cause "Deadline computed once before the loop; clone carries fired budget" \
  | jqget '["id"]')
python3 bin/campaigns.py set-finding-state --campaign bfl-retry \
  --slug retry-carried-deadline --state investigating
echo "FINDING_ID=$FINDING_ID"

step "2. register both fix branches [REAL LOCAL — SHA-bound, merge-free]"
A_ID=$(python3 bin/fix_branches.py register --repo "$REPO" \
  --branch fix/retry-deadline-reset --finding "$FINDING_ID" \
  --workorder-ref retry-carried-deadline | jqget '["id"]')
B_ID=$(python3 bin/fix_branches.py register --repo "$REPO" \
  --branch fix/retry-token-clone --finding "$FINDING_ID" \
  --workorder-ref retry-carried-deadline | jqget '["id"]')
echo "A_ID=$A_ID B_ID=$B_ID"

step "3. validate each branch in an ISOLATED detached worktree [REAL LOCAL — no merges]"
python3 bin/fix_branches.py validate --id "$A_ID" \
  --red-cmd ./red.sh --green-cmd ./green.sh --guard-cmd ./guard.sh \
  > "$ART/validation-A.json"
python3 bin/fix_branches.py validate --id "$B_ID" \
  --red-cmd ./red.sh --green-cmd ./green.sh --guard-cmd ./guard.sh \
  > "$ART/validation-B.json"
python3 bin/fix_branches.py get --id "$A_ID" > "$ART/branch-A.json"
python3 bin/fix_branches.py get --id "$B_ID" > "$ART/branch-B.json"

step "4. invariant check [REAL LOCAL]"
python3 - "$A_ID" "$B_ID" <<'PY'
import json, subprocess, sys
def get(i):
    out = subprocess.run(
        [sys.executable, "bin/fix_branches.py", "get", "--id", str(i)],
        capture_output=True, text=True, check=True).stdout
    return json.loads(out)
a, b = get(sys.argv[1]), get(sys.argv[2])
assert a["status"] == "validated", f"A must validate: {a['status']}"
assert b["status"] == "failed", f"B must FAIL its guard: {b['status']}"
va = [v for v in a["validations"] if v["tested_sha"] == a["head_sha"]]
vb = [v for v in b["validations"] if v["tested_sha"] == b["head_sha"]]
assert va and va[-1]["passed"] == 1, "A's current validation must pass"
assert vb and vb[-1]["passed"] == 0, "B's current validation must fail"
print("INVARIANT HOLDS: A validated (approvable), B failed (blocked)")
print(f"  A: RED {va[-1]['red_observed']}/{va[-1]['red_intended']}"
      f" GREEN {va[-1]['green_passed']}/{va[-1]['green_total']}"
      f" GUARD {va[-1]['guard_passed']}/{va[-1]['guard_total']} @ {a['head_sha'][:12]}")
print(f"  B: RED {vb[-1]['red_observed']}/{vb[-1]['red_intended']}"
      f" GREEN {vb[-1]['green_passed']}/{vb[-1]['green_total']}"
      f" GUARD {vb[-1]['guard_passed']}/{vb[-1]['guard_total']} @ {b['head_sha'][:12]}")
PY

echo
echo "Driver complete. Next: developer decisions in /branch-fixes (browser),"
echo "staleness demo, then Temporal for the approved branch ONLY."
echo "A_ID=$A_ID B_ID=$B_ID FINDING_ID=$FINDING_ID"
