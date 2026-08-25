#!/usr/bin/env bash
# Build the branch-fix-loop fixture repo: one real bug, two independent fix
# branches from the SAME base. Never merged. Deterministic content.
#
# Usage: build-fixture-repo.sh <target-dir>
#
# Layout produced:
#   main               retry.py with the bug (attempt-1 deadline reused)
#   fix/retry-deadline-reset      Branch A — correct fix (fresh per-attempt
#                                 deadline + logging)  -> RED 2/2 GREEN 3/3 GUARD 2/2
#   fix/retry-token-clone         Branch B — plausible but incomplete fix
#                                 (resets token, keeps carried budget)
#                                 -> RED 2/2 GREEN 3/3 GUARD 1/2 (guard fails)
#
# The test conventions match bin/branch_validator.py:
#   red.sh   prints "RED <observed>/<intended>"
#   green.sh prints "GREEN <passed>/<total>"
#   guard.sh prints "GUARD <passed>/<total>"
set -euo pipefail

TARGET="${1:?usage: build-fixture-repo.sh <target-dir>}"
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
G() { git -C "$TARGET" -c user.name="fixture" -c user.email="fixture@example.invalid" "$@"; }

rm -rf "$TARGET"
mkdir -p "$TARGET"
git -C "$TARGET" init -q -b main

# ── main: the bug ────────────────────────────────────────────────────────
cat > "$TARGET/retry.py" <<'PY'
"""Release retry with a bug: attempt 1's deadline is carried into attempt 2."""
import time


class RetryRunner:
    def __init__(self, budget_s=1.0, attempts=2, clock=time.monotonic, log=None):
        self.budget_s = budget_s
        self.attempts = attempts
        self.clock = clock
        self.log = log or (lambda msg: None)

    def run(self, task):
        # BUG: deadline computed ONCE — attempt 2 inherits attempt 1's clock.
        deadline = self.clock() + self.budget_s
        for attempt in range(1, self.attempts + 1):
            if self.clock() >= deadline:
                # BUG: silent abort — no log line for the skipped attempt.
                return {"attempt": attempt, "ok": False, "aborted": True}
            try:
                result = task(attempt)
                return {"attempt": attempt, "ok": True, "result": result}
            except TimeoutError:
                continue
        return {"attempt": self.attempts, "ok": False, "aborted": False}
PY

cat > "$TARGET/test_retry_red.py" <<'PY'
"""RED suite: these tests FAIL on main (they encode the intended fix).
Prints RED <observed>/<intended> where observed = tests that fail on the
buggy code and pass after a correct fix.
"""
import io
import sys

from retry import RetryRunner


def fake_clock_seq(values):
    it = iter(values)
    return lambda: next(it)


def test_attempt2_gets_fresh_deadline():
    # attempt 1 times out; by then the shared deadline (1.0) has fired, so
    # buggy main ABORTS attempt 2. A correct fix gives attempt 2 its own
    # full budget and succeeds.
    clock = fake_clock_seq([0.0, 0.0, 1.0, 1.0, 1.5])
    calls = []

    def task(attempt):
        calls.append(attempt)
        if attempt == 1:
            raise TimeoutError()
        return "shipped"

    r = RetryRunner(budget_s=1.0, attempts=2, clock=clock)
    out = r.run(task)
    assert out["ok"], f"attempt 2 was starved: {out}"


def test_aborted_attempt_is_logged():
    lines = []
    clock = fake_clock_seq([0.0, 0.0, 2.0, 2.0, 2.0])

    def task(attempt):
        raise TimeoutError()

    r = RetryRunner(budget_s=1.0, attempts=2, clock=clock, log=lines.append)
    r.run(task)
    assert any("abort" in l.lower() for l in lines), f"no abort log: {lines}"
PY

cat > "$TARGET/test_retry_green.py" <<'PY'
"""GREEN suite: behavior that must keep working on every branch."""
from retry import RetryRunner


def test_success_first_attempt():
    r = RetryRunner(budget_s=10.0, attempts=2)
    out = r.run(lambda attempt: "ok")
    assert out == {"attempt": 1, "ok": True, "result": "ok"}


def test_exhausts_attempts():
    r = RetryRunner(budget_s=10.0, attempts=3)
    out = r.run(lambda attempt: (_ for _ in ()).throw(TimeoutError()))
    assert out["ok"] is False and out["attempt"] == 3


def test_result_passthrough():
    r = RetryRunner(budget_s=10.0, attempts=1)
    out = r.run(lambda attempt: {"build": 42})
    assert out["result"] == {"build": 42}
PY

cat > "$TARGET/test_retry_guard.py" <<'PY'
"""GUARD suite: regressions the fix must not introduce (review-bar rules).

guard 1: a fired attempt-1 deadline must NEVER be reused for attempt 2
         (the root cause; rule retry-paths-log-and-guard v1).
guard 2: per-attempt budget must come from CONFIG, not carried state —
         attempt 2's window equals budget_s even after a slow attempt 1.
"""
from retry import RetryRunner


def fake_clock_seq(values):
    it = iter(values)
    return lambda: next(it)


def test_guard_no_deadline_reuse():
    clock = fake_clock_seq([0.0, 0.0, 0.99, 0.99, 1.0, 1.5, 2.0])

    def task(attempt):
        if attempt == 1:
            raise TimeoutError()
        return "ok"

    out = RetryRunner(budget_s=1.0, attempts=2, clock=clock).run(task)
    assert out["ok"], "fired deadline was reused across attempts"


def test_guard_budget_from_config_not_carried():
    # Attempt 1 times out; the clock then jumps to t=1.6. A correct fix
    # (per-attempt budget FROM CONFIG) gives attempt 2 a fresh 1.0s window
    # (deadline 2.6) and succeeds at t=1.6. A carried-pool fix (e.g. B's
    # 1.5x total budget) has exhausted its pool (1.6 > 1.5) and aborts.
    clock = fake_clock_seq([0.0, 0.0, 1.6, 1.6, 1.8, 2.5])

    def task(attempt):
        if attempt == 1:
            raise TimeoutError()
        return "ok"

    out = RetryRunner(budget_s=1.0, attempts=2, clock=clock).run(task)
    assert out["ok"], "attempt 2 budget was carried, not from config"
PY

cat > "$TARGET/red.sh" <<'SH'
#!/bin/sh
# RED convention: report how many intended-failure tests now PASS (i.e. the
# fix landed). observed = passing RED tests; intended = total RED tests.
out=$(python3 -m pytest test_retry_red.py -q 2>&1) || true
total=$(echo "$out" | grep -oE '[0-9]+ (passed|failed)' | awk '{s+=$1} END {print s}')
passed=$(echo "$out" | grep -oE '[0-9]+ passed' | awk '{print $1}')
echo "RED ${passed:-0}/${total:-2}"
SH

cat > "$TARGET/green.sh" <<'SH'
#!/bin/sh
out=$(python3 -m pytest test_retry_green.py -q 2>&1) || true
total=$(echo "$out" | grep -oE '[0-9]+ (passed|failed)' | awk '{s+=$1} END {print s}')
passed=$(echo "$out" | grep -oE '[0-9]+ passed' | awk '{print $1}')
echo "GREEN ${passed:-0}/${total:-3}"
SH

cat > "$TARGET/guard.sh" <<'SH'
#!/bin/sh
out=$(python3 -m pytest test_retry_guard.py -q 2>&1) || true
total=$(echo "$out" | grep -oE '[0-9]+ (passed|failed)' | awk '{s+=$1} END {print s}')
passed=$(echo "$out" | grep -oE '[0-9]+ passed' | awk '{print $1}')
echo "GUARD ${passed:-0}/${total:-2}"
SH

chmod +x "$TARGET"/red.sh "$TARGET"/green.sh "$TARGET"/guard.sh
G add -A
G commit -qm "retry runner with carried-deadline bug + RED/GREEN/GUARD suites"
BASE_SHA=$(G rev-parse HEAD)

# ── Branch A: correct fix ────────────────────────────────────────────────
G checkout -qb fix/retry-deadline-reset
cat > "$TARGET/retry.py" <<'PY'
"""Release retry — FIXED: fresh per-attempt deadline from config + logging."""
import time


class RetryRunner:
    def __init__(self, budget_s=1.0, attempts=2, clock=time.monotonic, log=None):
        self.budget_s = budget_s
        self.attempts = attempts
        self.clock = clock
        self.log = log or (lambda msg: None)

    def run(self, task):
        for attempt in range(1, self.attempts + 1):
            # FIX: every attempt gets a FULL budget from config.
            deadline = self.clock() + self.budget_s
            self.log(f"attempt {attempt} start (budget={self.budget_s}s)")
            if self.clock() >= deadline:
                self.log(f"attempt {attempt} abort: deadline already passed")
                return {"attempt": attempt, "ok": False, "aborted": True}
            try:
                result = task(attempt)
                return {"attempt": attempt, "ok": True, "result": result}
            except TimeoutError:
                self.log(f"attempt {attempt} abort: timeout")
                continue
        return {"attempt": self.attempts, "ok": False, "aborted": False}
PY
G add retry.py
G commit -qm "fix: fresh per-attempt deadline from config; log every start/abort"
A_SHA=$(G rev-parse HEAD)

# ── Branch B: plausible but incomplete fix (from the SAME base) ─────────
G checkout -q main
G checkout -qb fix/retry-token-clone
cat > "$TARGET/retry.py" <<'PY'
"""Release retry — PARTIAL fix: resets the deadline check per attempt but
carries the REMAINING budget forward (looks right, fails the config guard)."""
import time


class RetryRunner:
    def __init__(self, budget_s=1.0, attempts=2, clock=time.monotonic, log=None):
        self.budget_s = budget_s
        self.attempts = attempts
        self.clock = clock
        self.log = log or (lambda msg: None)

    def run(self, task):
        start = self.clock()
        total_budget = self.budget_s * 1.5  # "generous" carried pool
        for attempt in range(1, self.attempts + 1):
            elapsed = self.clock() - start
            remaining = total_budget - elapsed
            self.log(f"attempt {attempt} start (remaining={remaining:.2f}s)")
            if remaining <= 0:
                self.log(f"attempt {attempt} abort: budget exhausted")
                return {"attempt": attempt, "ok": False, "aborted": True}
            try:
                result = task(attempt)
                return {"attempt": attempt, "ok": True, "result": result}
            except TimeoutError:
                self.log(f"attempt {attempt} abort: timeout")
                continue
        return {"attempt": self.attempts, "ok": False, "aborted": False}
PY
G add retry.py
G commit -qm "fix: per-attempt deadline check with carried budget pool"
B_SHA=$(G rev-parse HEAD)

G checkout -q main
echo "BASE_SHA=$BASE_SHA"
echo "A_SHA=$A_SHA (fix/retry-deadline-reset)"
echo "B_SHA=$B_SHA (fix/retry-token-clone)"
