# Unity Explorer top-three-model E2E and UX audit

Date: 2026-08-16

Target: `~/Projects/unity-editor/Explorer` (read-only; no Unity project files were changed)

App under test: `http://localhost:4000`

## Verdict

The core workflow works end to end, but the app currently feels like a capable internal evaluation console rather than one coherent product. I would rate the overall experience **6.5/10**: the start, domain, and human-judge flow are clear; result analysis, prompt administration, and brackets do not yet join into the same mental model.

The tournament was useful as a bug-candidate generator, not as an autonomous bug oracle. Human source review found 14 plausible, source-supported issues. The three-model majority agreed with the human-selected side on only 2 of 7 pairs, largely because the judge was not receiving each card's authoritative `source_ref`. That input defect was fixed after the run and the full Python suite passes.

## Completed run

- Created domain `unity-explorer-bugs-luna-e2e` against six representative authored C# files from a 4,945-file Explorer corpus.
- Generated 14 cards with 0 item errors, producing 7 pairwise matches.
- Completed 28/28 queued judgements: 7 human and 7 from each of the three OpenRouter models.
- Wrote 56 score rows (verdict and confidence for each judgement).
- Used the current OpenRouter `sort=most-popular` structured-output-capable top three:
  1. `deepseek/deepseek-v4-flash-0731`
  2. `tencent/hy3`
  3. `openai/gpt-5.6-luna`
- Raw run artifacts are in `/tmp/data-tournaments-unity-e2e-20260816/`.

## Human findings in Explorer

These are static source findings, not runtime-reproduced Unity failures.

1. `PlayersWrap.BuildPlayersJson` enumerates participants, refetches a nullable participant, and force-dereferences it; removal between those operations can throw. (`PlayersWrap.cs:48-57`)
2. `ENetTransport.ConnectAsync` catches timeout but not caller cancellation, leaving its host/listener lifecycle unfinalized. (`ENetTransport.cs:92-124`)
3. Oversized ENet receive and send payloads can exceed fixed buffers and throw during copy/span creation. (`ENetTransport.cs:243-250,292-301`)
4. Texture deserialization trusts corrupt metadata and raw payload dimensions before texture construction/loading. (`TextureDiskSerializer.cs:13-40,148-162`)
5. `DiskCache` assumes one `FileStream.ReadAsync` fills the requested buffer and ignores the returned byte count. (`DiskCache.cs:114-123`)
6. `TextureDiskSerializer.DeserializeAsync` accepts a cancellation token but does not observe it.
7. ENet ports are converted to `ushort` without range validation, so invalid caller input can wrap to a different endpoint.
8. Truncated texture metadata is sliced before a length check; the typed cache catches and evicts it, making this recoverable but noisy.
9. Cache replacement deletes the valid destination before moving the new file, so a crash or move failure loses the prior entry.
10. `DCLWebSocket.ConnectAsync` lacks the disposed guard/error normalization used by its send, receive, and close methods.
11. `ENetTransport.Send` calls ENet directly even though the listener establishes a dedicated-thread access pattern.
12. An exception in the ENet listener can leave `listenLoopIsActive` true because cleanup is not in `finally`, allowing disconnect to wait forever.
13. The process-global ENet library initialized flag is unsynchronized; disposing one overlapping transport may deinitialize another transport's library.
14. A world-access request already in flight can repopulate a password cache immediately after successful-password invalidation.

The strongest immediate code-review candidates are the ENet listener-finally issue, direct cross-thread ENet send, caller-cancellation cleanup, partial file read, and PlayersWrap removal race.

## Human versus model panel

“Side match” counts a model as agreeing when it chose the same A/B/tie side, even if its strength differed.

| Match | Human | DeepSeek | Luna | Hy3 | Panel side match |
|---|---|---|---|---|---|
| 0 | B marginal | B marginal | A marginal | A clear | No |
| 1 | A clear | A clear | A clear | tie strong | Yes |
| 2 | A marginal | A clear | A clear | A clear | Yes |
| 3 | A marginal | B marginal | B clear | B marginal | No |
| 4 | B marginal | A clear | A clear | A clear | No |
| 5 | tie strong | A clear | B marginal | B clear | No |
| 6 | A marginal | B clear | B clear | B clear | No |

Side agreement with the human judge was DeepSeek 3/7, Luna 2/7, Hy3 1/7, and panel majority 2/7. This should not be read as a clean model-quality ranking: the judge input omitted authoritative source paths, while the human audit used them and inspected the source.

Latency and reliability also differed. Luna was usually fastest and was the only model to finish full-file generation for this run. DeepSeek and Hy3 generation trials remained stuck for several minutes even with a configured 60-second timeout and one retry. Judging was more reliable, though Hy3 was often slower than 30 seconds. The timeout setting therefore does not reliably bound the full DSPy/LiteLLM generation call.

## Navigation and screen coherence

### Start

The strongest page. “Start with one question” and the Source → Fan out → Judge pairs → Learn sequence explain the product quickly. Category cards provide an obvious entry point.

### Domains

Domain cards make Edit, Improve judge, and Fan out candidates discoverable. The screen becomes cluttered after experimental runs, long domain names and paths wrap heavily, and cards do not show generation progress, last-run outcome, or the resulting match count. A domain should feel like the persistent home for its corpus, runs, and results; today it mostly feels like a launcher.

### Judge

The best task screen. Queue status, two-card comparison, confidence, and verdict actions form a focused flow. The `0 pending · 38 done` inbox-zero state is reassuring and provides a direct route to all judgements. Source references are visible on cards.

### Judgements

Useful as an audit log, but weak as an analysis destination. It displays identifiers such as `domain:5` and `#6` instead of the domain name and card titles. Source references are absent, and there is no per-match grouping that lets a reviewer compare human, DeepSeek, Luna, and Hy3 verdicts together. This is the largest navigation dead end: completing Judge sends the user here, but the page cannot answer “where did the raters disagree and why?”

### Prompts

The model selectors correctly show the new top three. In `PROMPT_BACKEND=local`, however, the page still says “Langfuse-backed prompts” and “No prompts in Langfuse,” while generation successfully uses local `prompts.json`. Domain edit likewise says prompt edits push a Langfuse version. This makes a working local configuration look broken and creates two conflicting sources of truth.

### Inspect

A solid developer/admin surface with domains, pending rows, scores, prompts, and exports. It is data-centric and appropriately secondary, although long values truncate and there are few links back to the corresponding domain or judgement.

### Brackets

Visually consistent but conceptually separate. It opens a split-screen tournament configuration (`actions-style-hermes`, `bookmarks-style`) that is not clearly connected to domain fan-out or the judgement queue. The label “brackets” is ambiguous and the page currently feels like a second product sharing the same navigation bar.

## App defects and recommended order

1. **Fix request cancellation/timeout ownership.** Full-file generation must have a hard, observable deadline and transition the run to a terminal error/cancelled state. The two stalled trial domains produced no pending rows and exposed no UI progress.
2. **Make Results a comparison view.** Group by domain and match; show domain name, both card titles/source refs, every rater verdict, disagreement, and rationale. Keep the current flat table as an audit-log mode.
3. **Unify local and Langfuse prompt backends in the UI.** The Prompts and domain-edit screens should read and label the active backend, not hard-code Langfuse language.
4. **Give domains a run history.** Show state, model, source item/card/match counts, errors, elapsed time, and links into Judge/Results.
5. **Clarify or relocate Brackets.** Explain its relationship to domain tournaments, or move legacy tournament configuration under an Advanced/Legacy section.
6. **Add archival controls for experiments.** Failed/stalled domains otherwise accumulate alongside successful ones with no visual distinction.

## Fix made during this audit

`MatchJudge` now receives `card_a_source_ref` and `card_b_source_ref`, the live worker forwards the authoritative refs from each trace payload, and optimizer examples retain the same fields. This prevents future judges from treating an available source path as absent merely because generated prose failed to repeat it.

Verification: the focused judge/optimizer tests pass, and the complete Python suite passes with 2 expected skips. The existing completed E2E verdicts were preserved as audit evidence rather than rewritten after the fact.

