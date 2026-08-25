Fix is_canonical_cid: enforce exact CIDv1 decoded-length semantics (reject truncated 58-char and oversized CIDs)

**Domain:** catalyrst-cid-w11-workorders · **Created:** 2026-08-17T22:31:13Z · **Priority:** P0 — Ongoing error-level production issue (476 combined events/7d across CATA-2201 and CATA-2230, 70 users). A core correctness gate accepts malformed identifiers, so every downstream consumer pays a verify_hash failure and the server violates content-addressing integrity. · **Type:** bug-fix  
**Links:** [Repository](https://gitea/usr/data-tournaments)  
**Models:** openai/moonshotai/kimi-k3  
**Repo:** gitea:usr/data-tournaments @ `668ea3a79815` *(dirty working tree)*  
**Source:** `docs/showcases/catalyrst-branch-fix-loop/fixtures/sentry-cid.csv`  
**Files (approx.):** `src/cid.rs (or src/validation/cid.rs — approximate; wherever is_canonical_cid lives)`, `src/handlers/get_content.rs`, `src/sync/deploy_remote_entity.rs`, `tests/cid_validation.rs`, `tests/cid_oracle_parity.rs`

## Goal

Make is_canonical_cid reject structurally invalid CIDs (truncated, oversized, digest-length mismatch) so a malformed CID can never reach verify_hash. This is the shared root cause of CATA-2201 (412 events/7d, 58 users) and CATA-2230 (64 events/7d, 12 users): the validator accepts wrong-length CID strings and the failure only surfaces downstream as a hash mismatch, breaking the content-addressability guarantee at every ingress point.

## Context and evidence

CATA-2201: 'verify_hash mismatch after is_canonical_cid ACCEPTED a truncated 58-char CIDv1', culprit catalyrst-server::handlers::get_content, 412 events/7d, 1893 lifetime, ongoing. CATA-2230: 'deploy_remote_entity: oversized CID string accepted by validator, hash mismatch downstream', culprit catalyrst-server::sync::deploy_remote_entity, 64 events/7d. Both failure modes (too short and too long) indicate the validator checks shape/prefix but never verifies decoded length against the multihash digest size.

## Implementation plan

1. Add RED unit tests pinning exact length semantics: a canonical CIDv1 (dag-pb, sha2-256, base32 lower) is exactly 59 chars ('b' multibase prefix + 58); assert 58/57/32-char truncations are rejected, oversized strings and CIDs whose decoded digest byte count != multihash declared size are rejected.
2. Replace prefix/length heuristics in is_canonical_cid with a real decode: multibase prefix -> varint(version) -> varint(codec) -> multihash (code varint, size varint) -> assert remaining bytes == declared digest size and no trailing bytes.
3. Add upstream-oracle parity test: over a fixed vector set (canonical CIDs + systematic truncations/extensions/mutations), is_canonical_cid acceptance must exactly match cid::Cid::try_from acceptance plus canonical round-trip equality.
4. Add proptest: any truncation or extension of a canonical CID byte/string form is always rejected.
5. Route all three ingress validators (handlers::get_content, sync::deploy_remote_entity, sync::snapshots) through the fixed function; remove any local ad-hoc CID checks.

## Acceptance criteria

- RED: at least 8 new validator unit tests fail on current code (4 truncation lengths: 58/57/32/1 chars; 4 oversize/digest-mismatch cases); all pass after the fix.
- Guard test pins the exact invariant: canonical CIDv1 dag-pb/sha2-256 base32 is exactly 59 chars; acceptance for that vector is preserved.
- Oracle parity: >=200-case vector set (50 canonical, 150 mutations) where is_canonical_cid verdict == cid::Cid::try_from verdict; 0 divergences allowed.
- proptest: >=10k random truncations/extensions of canonical CIDs, 0 false accepts.
- Backward-compat guard: replay over a corpus of previously stored valid CIDs yields 0 false rejections (non-sha2-256 multihashes such as identity/sha3/blake2b included).
- Replay of CATA-2201 and CATA-2230 payloads fails validation with a 4xx-style rejection before any verify_hash call; 0 new events for either issue in the 7 days post-deploy.

## Risks and open questions

- Malformed CIDs previously accepted and stored/referenced may now be rejected; needs an audit/migration note before rollout.
- Over-strict decoding could falsely reject legitimate non-canonical-but-resolvable CIDs sent by legacy clients; oracle parity tests mitigate.
- Varint edge cases for multihash codes other than sha2-256 must be handled to avoid false rejections.
