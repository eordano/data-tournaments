# lint-jail — C# build/test without network, in bwrap

Ported 2026-08-24 from `unity-explorer-dev/scripts/lint/lint-jail.sh` (the
bwrap C#-without-Unity jail). The Unity-specific pieces (`inspect` mode,
`pull-sln-from-build-host.sh`, `fixup-sln.py`) were dropped — this repo consumes no
Unity-generated solutions. What remains is the generic core: `jail()` +
`prep_feed()` plus a bundled smoke project.

## Usage

```sh
MODE_ONLINE=1 scripts/lint/lint-jail.sh smoke   # once: online NuGet restore
scripts/lint/lint-jail.sh smoke                 # fully offline from here on
scripts/lint/lint-jail.sh test <dir> [args..]   # any csproj dir, relative to repo root
scripts/lint/lint-jail.sh exec <ws> <cmd..>     # arbitrary command, <ws> bound writable
```

Restore/build state lives in `.lint-jail-cache/` at the repo root
(gitignored). `test` builds in a per-run scratch dir (`build.$$`), so
concurrent runs don't clobber each other; `exec` is the primitive the
sandbox backend uses.

## Sandbox runner integration

`bin/sandbox/bwrap_backend.py` exposes the jail as sandbox backend
`"bwrap"` (see `get_backend`): profile `flake_ref` is nix-built and passed
as `TOOLCHAIN`, commands run through `exec` in a per-run workspace under
`.lint-jail-cache/runs/`, and profiles requesting egress or egress-proxy
secrets are refused (the jail is offline-only; use e2b/microvm). It is a
trusted-tier substrate: isolation is weaker than a microVM (shared kernel,
host `/nix` + `/etc` visible read-only) — never point untrusted code at it.
Backend mapping is unit-tested in `tests/test_sandbox_bwrap.py` without
invoking bwrap; the jail itself stays manually verified (commands above).

## Toolchain = the synthetic flake

`flake.nix` here pins everything the runners execute — dotnet SDK, bwrap,
curl, iproute2, bash, coreutils — against the same nixpkgs rev as the repo's
`flake.lock` (bump both together). `lint-jail.sh` resolves it via
`nix build path:scripts/lint#toolchain` and puts `$toolchain/bin` first on
PATH inside the jail, so the environment is defined by the lockfile, not by
whatever the host has installed. The resolved store path is cached in
`.lint-jail-cache/toolchain-path` so offline runs never evaluate the flake.
Override with `TOOLCHAIN=/nix/store/...` for a one-off different SDK.
`nix develop ./scripts/lint` drops a human (or agent) into the same env.

## Trap ledger — each of these cost a debugging round upstream; do not undo

1. **vstest needs loopback TCP** (console↔testhost). In `--unshare-net`,
   `ip link set lo up` requires `--cap-add CAP_NET_ADMIN` (which itself needs
   `--unshare-user --uid 0 --gid 0`) — bwrap drops all caps even with
   `--uid 0`. Without lo up, test runs hang forever.
2. **Microsoft.CodeAnalysis.Testing dials nuget.org per test at RUNTIME**
   (`ReferenceAssemblies.ResolveAsync` → service index). The warmed
   `NUGET_PACKAGES` cache does NOT satisfy it; offline every test fails with
   FatalProtocolException after a DNS stall. Fix = `prep_feed()`: a flat
   folder feed hardlinking every cached `*.nupkg`, plus
   `microsoft.netcore.app.ref.3.1.0.nupkg` (which the restore itself never
   pulls — fetched once from nuget.org flatcontainer), and a `<clear/>` +
   local-feed NuGet.Config at BOTH `$CACHE/home/.nuget/NuGet/NuGet.Config`
   and the build dir.
3. **Bind an EMPTY file over `$(readlink -f /etc/resolv.conf)`** so stray DNS
   fails instantly instead of ~5s per test (an 8s suite became 8m29s
   upstream). NixOS `/etc` is symlinks — bind the resolved target, not the
   symlink.
4. **ro-bind `/bin` and `/usr/bin`** or `#!/bin/sh` shebangs break in the
   jail.
5. **The repo is read-only in the jail** — dotnet needs writable `obj/bin`,
   so tests build from a scratch copy under `.lint-jail-cache/build`, never
   in-tree.
6. **First run needs `MODE_ONLINE=1`** for the one-time NuGet restore (it
   keeps the host network namespace); everything after is fully offline.
   Adaptation vs upstream: online mode swaps in `nuget-online.config`
   (local feed + nuget.org) at both config locations — upstream's local-only
   config would make even the bootstrap restore fail with NU1101.

Full upstream context (including the Unity/LFS/sln traps that don't apply
here): the operator's private lint-jail notes.
