# Known issues / tracked follow-ups

## Truncation detection depends on provider finish_reason (2026-08-16)

Status: open — deferred by design from commit 50bf344.

`bin/generators/card_gen.py` classifies a generation as `truncation` when the
adapter parse fails AND the winning call's `finish_reason` indicates length
exhaustion, and it rejects repaired-but-truncated JSON by checking the
winning call's finish_reason. This closes the silent-repair hole for
compliant providers.

Residual gap: a provider that returns an incomplete payload with
`finish_reason="stop"`, or omits finish_reason entirely, can still slip an
incomplete-but-parseable payload through as a success.

Proposed fix (separate change, needs compatibility tests): add a required
terminal sentinel field to the generation output schema (e.g. a final
`"complete": true` key emitted last), and fail items whose payload parses
but lacks the sentinel. This makes completeness detectable from payload
content alone, independent of provider metadata.

Why deferred: it is a schema-contract change affecting every generator
prompt and parser, beyond the scope of the failure-taxonomy work in
50bf344. All specified failure classes (timeout / parse-error / truncation)
are covered and tested for providers that report finish_reason correctly.
