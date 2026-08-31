# Unity editor bug-surfacing E2E

Date: 2026-08-16

Target: `~/Projects/unity-editor` (read-only; its existing worktree was not modified)

## Run shape

- Drafted a `unity-csharp-correctness-e2e` domain through the live DSPy/OpenRouter path.
- Fanned out 12 production C# files covering cache ownership, networking, cancellation, scene runtime, and concurrency.
- Generated 10 cards with 0 source-item errors, then enqueued and completed 5 LLM and 5 human pairwise judgements.
- OpenRouter LLM verdicts exactly matched the source audit on 4/5 pairs.
- The five human verdicts load as exactly five domain-scoped optimizer examples.
- Raw artifacts: `/tmp/data-tournaments-unity-e2e-20260816/{domain.json,report.json,judgements.db}`.

## Source-supported candidates

These are static source findings, not runtime-reproduced Unity failures.

1. **Owned texture-cache memory leaks when deserialization throws.**
   `IDiskSerializer.DeserializeAsync` explicitly transfers ownership of `SlicedOwnedMemory`, backed by `UnsafeUtility.Malloc`. `TextureDiskSerializer` disposes it only after successful texture construction/load/apply. A corrupt entry throwing in `Meta.FromSpan`, `Texture2D`, or `LoadRawTextureData` reaches `DiskCache`'s catch without either side freeing that memory.
   Sources: `TextureDiskSerializer.cs:13-40`, `IDiskCache.cs:70-75,116-149`, `DiskCache.cs:208-215`.

2. **Caller cancellation leaves an ENet host unfinalized.**
   `ConnectAsync` creates the host and starts the listener, but catches only `TimeoutException`. Caller cancellation from `WaitUntil` stops the linked listener and propagates without `ForceDisconnectAsync`/`FinalizeHost`, retaining `host`/`serverPeer` until some later explicit disconnect.
   Source: `ENetTransport.cs:92-138`.

3. **A second ENet connect can overlap two listener loops and corrupt the shared active flag.**
   A new host is assigned before the old lifecycle token is cancelled. The two loops use separate captured hosts but one `listenLoopIsActive`; the old loop can write `false` while the new loop is active, allowing disconnect/finalization to race the new loop.
   Source: `ENetTransport.cs:102-112,126-138,190-218`.

4. **`DCLWebSocket.ConnectAsync` is the only async operation that is unsafe after disposal.**
   `Dispose` disposes the underlying socket. Send, receive, and close all guard/catch disposal, while connect has neither guard nor `ObjectDisposedException` handling. `Dispose(); await ConnectAsync(...)` therefore leaks an unexpected framework exception through the wrapper contract.
   Source: `DCLWebSocket.cs:30-38,58-60,89-103`.

5. **`PlayersWrap` re-fetches a nullable participant and dereferences it.**
   It enumerates a dictionary entry, discards the already available value, then calls the nullable `RemoteParticipant(identity)!`. A participant disappearing between snapshot/enumeration and lookup makes `.Identity` fail; other project call sites explicitly null-check this API.
   Source: `PlayersWrap.cs:38-69`.

6. **Expired world-permission cache entries are never evicted.**
   TTL expiry only prevents reuse; it does not remove the key. Unique world/wallet keys accumulate for the service lifetime, while successful password validation removes only one world's prefix.
   Source: `CachedWorldPermissionsService.cs:36-69`.

7. **`MultiThreadSync.Acquire` can grant ownership after concurrent disposal.**
   Acquire checks disposal and queues under one lock, then records ownership under a second lock without rechecking. Dispose between those sections clears/disposes the queue owner, after which Acquire records the disposed owner and returns a scope whose release is ignored. This is narrow but violates the ownership invariant.
   Source: `MultiThreadSync.cs:69-87,89-147,150-161`.

## Rejected cards

The human audit marked one pair `tie-both-weak` and rejected other cards from the fast comparison batch:

- `CancellationTokenSource.SafeRestart()` explicitly accepts null, so the reported first-call null dereference is false.
- `ReusableTickDelay.Delay` already performs the exact atomic post-arm cancellation re-check the card claimed was missing.
- `Texture2D.GetRawTextureData()` returns a borrowed `NativeArray` view; disposing it in the serializer would be incorrect.
- `ApplyStaticMessages` passes `returnData: false`, and the implementation guarantees `PoolableByteArray.EMPTY`; there is no rented result to leak.
- SceneFacade calls `SetIsDisposing()` before runtime disposal, contradicting the alleged undisposed lifecycle token.
- Generic thread-safety warnings on documented main-thread-only cache/debouncer paths lacked a valid cross-thread trigger.

## Mechanism assessment

The mechanism is useful as a **candidate generator and prioritizer**, not an autonomous bug oracle. The main run surfaced seven source-supported candidates from ten cards, but a smaller fixed-model run produced eight mostly generic false positives. Pairwise judging ranked the supplied card text well (4/5 exact agreement) but cannot independently verify omitted guards or cross-file contracts.

The E2E exposed and prompted four mechanism fixes:

- C# is now included in the guided code globs.
- Corpus provenance always overrides model-guessed `source_ref`.
- Fan-out logs each active source item.
- DSPy requests now have configurable bounded timeouts and retries.

For production use, human source verification remains required. The highest-leverage next quality step is to put exact evidence snippets or related contract context into each card so the pairwise judge can challenge, rather than merely rank, plausible claims.
