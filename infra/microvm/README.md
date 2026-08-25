# microvm.nix Linux runner — self-hosted sandbox substrate (wave 5)

Status: configuration complete, **hardware-gated** — building/running these
guests requires a Linux host with KVM (`/dev/kvm`). The Mac stays the
control plane per docs/research/sandbox-execution-options-2026.md §Recommendation.

## Design

    Phoenix / Temporal worker (Mac or Linux)
      -> dispatch SandboxRunRequest (backend="microvm")
      -> Linux runner host: nix run .#run-sandbox -- <profile.json> <request.json>
           - guest = NixOS microVM (cloud-hypervisor), built from THIS flake
           - workspace pinned to (flake.lock, profile.base_commit)
           - /nix/store mounted read-only; writable overlay per run
           - tap interface into br-sandbox; nftables deny-by-default (egress.nft)
           - secrets: placeholder substitution at the egress proxy only —
             plaintext never enters the guest (Daytona pattern)
      -> artifacts out via virtiofs share -> CAS -> EvidenceRef

## Identity & reproducibility

Sandbox identity = sha256(flake.lock) + profile.base_commit. Two runs with
the same profile digest see byte-identical inputs. The guest closure pins
the unity-explorer dev toolchain; rebuilding on any Linux host with the
same lock file yields the same store paths.

## Egress

`egress.nft` implements deny-by-default per guest tap interface:
- default drop on forward from br-sandbox
- allowlist: the Nix binary cache, github.com (pinned repo fetch), and the
  egress proxy (which holds the domain-level allowlist + secret
  substitution). Everything else, including DNS to arbitrary resolvers, is
  dropped and logged.

## Deploying (Linux host, one-time)

1. Install NixOS or Nix-on-Linux with KVM available.
2. `nix build .#sandbox-guest` — builds the guest image.
3. `sudo nft -f egress.nft` (adjust interface/bridge names).
4. Wire `bin/sandbox/microvm_backend.py` (future) to `run-sandbox` over SSH
   or a local queue; until then, the `fake` and `e2b` backends serve
   preflight duty.

## Explicitly out of scope here

- Real Unity builds inside the guest (Unity licensing on Linux runners is
  its own project; builds stay on Unity Cloud Build in wave 6).
- Multi-tenant scheduling; one run per guest, guests are disposable.
