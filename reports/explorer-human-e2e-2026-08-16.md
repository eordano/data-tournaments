# Explorer human E2E review — 2026-08-16

## Scope

- Live app: `http://localhost:4000`
- Domain: `explorer-bugs-human-e2e-20260816`
- Corpus: four C# files from `~/Projects/unity-editor/Explorer`
- Flow exercised: Start → Domains → Review → Results → Prompts
- Panel: human reviewer, Kimi K3, GLM 5.2, Claude Opus 5

## Outcome

The workflow completed end to end with one generated pair and four recorded
ratings. The final Results view reports **Human and panel agree**: the human,
GLM 5.2, and Claude Opus 5 preferred candidate A marginally, while Kimi K3
preferred candidate B marginally.

Human verdict: `a-marginally-better`, confidence `mid`.

Candidate A identifies a deferred-sign-in lifetime bug: a replacement bridge
file inherits the running stopwatch of an older deferred link and can therefore
be deleted before it has had its own 300-second lifetime. Candidate B identifies
a read/delete TOCTOU window that can delete a newly overwritten bridge file.
Both follow from `DeepLinkSentinel.cs`; A was judged marginally stronger because
its reproduction is deterministic, whereas B depends on a very narrow timing
window.

## Navigation and coherence judgment

Overall: **8/10 after fixes**. The primary flow is understandable and the visual
hierarchy is consistent: a compact global workspace nav, strong page titles,
card-based work areas, and clear primary actions.

| Screen | Human judgment |
| --- | --- |
| Start | Strong entry point. “Start with one question” and the five review lenses make the product purpose immediately legible. The direct-bracket path is visibly secondary. |
| Domains | Works as the workflow hub. Configure, generate, review, compare, and improve actions are grouped per domain with useful counts. Dense once many domains exist, but still scannable. |
| Review | Best-focused screen: queue at left, judging brief and A/B evidence in the center, verdict controls below. Keyboard hints are useful without dominating. The completed filtered state now keeps the user in the same domain. |
| Results | Strongest synthesis screen. Candidate evidence, human baseline, named model raters, rationales, and agreement status are grouped into one match card. Domain and rater filters make sense. |
| Prompts | Clear advanced workspace for context evolution and promotion. It is coherent with “Improve,” though it feels more operator-oriented than the first four screens and is not automatically scoped to the domain just reviewed. |

The main path now preserves context:

`Start → domain hub → domain review → same-domain results → prompt studio`

“Direct brackets” and “Data” are appropriately separated as advanced paths.

## Bugs found and fixed during the run

1. **Generation appeared hung.** A 90-second LM timeout plus two retries could
   occupy roughly five minutes on one corpus item. Generation now has its own
   bounded configuration: 60-second default timeout, zero retries, and a
   configurable 4096-token output ceiling. Per-item failures continue through
   the corpus instead of blocking all progress.
2. **Multi-model identity leak.** The queue worker globally retained the first
   row's LM, so later GLM and Opus rows could silently run through Kimi while
   being labelled as their configured models. Each row now executes inside an
   isolated DSPy LM context. The live database proves configured and recorded
   identities match for all three models.
3. **Domain context was lost between Review and Results.** A filtered Review
   screen linked to global Results, while filtered Results displayed global
   pending counts and linked back to the global queue. Links and counts now
   remain scoped to the active domain.

## Residual observation

Reasoning-heavy models can spend most of their output budget on hidden analysis.
The audited run used a temporary 2048-token override and three of four source
items ended in parse/truncation errors; the loop recovered and generated the
tested pair. The shipped default is now 4096 tokens, but generation quality and
latency should still be monitored on larger files.

## Verification

- Python: full suite passed (`110 passed, 2 skipped`).
- Phoenix/Elixir: full suite passed (`70 tests, 0 failures`).
- Live HTTP: Start, Domains, filtered Review, filtered Results, and Prompts all
  returned successfully.
- Live state: one match, four completed ratings, zero pending ratings for the
  audited domain.
