# Sandbox Execution Options for AI Coding Agents (researched Aug 2026)

Context: WorkOrders pinned to base commits of `decentraland/unity-explorer`; want agents to execute them in sandboxes as part of release/deploy pipelines. Constraints: heavy Nix flake usage, mostly-macOS team (host already runs a strict seatbelt sandbox — nested `sandbox-exec` is a known failure mode), Phoenix control plane, self-hosted preferred, deny-by-default egress, per-step secrets.

---

## 1. E2B — managed ephemeral Firecracker sandboxes

- **Isolation model**: Firecracker microVMs on E2B's cloud (their infra repo is the actual production stack). Each sandbox is a full VM with `envd` control daemon inside; SDK talks to it over authenticated APIs ("secured access" is on by default since SDK v2, `X-Access-Token` per sandbox). Pause/resume/fork/snapshot of full memory state.
- **Egress control**: first-class. `allow_internet_access=False` for full block, or fine-grained `network.deny_out`/`allow_out` with CIDRs and domain allowlists (domains only in allow list; must pair with deny-all). Can update egress rules on a *running* sandbox via API (`Update sandbox network`). DSCP marking of egress for your firewall. Docs: https://docs.e2b.dev/network/internet-access.md , https://docs.e2b.dev/api-reference/sandboxes/update-sandbox-network.md
- **Secrets**: env vars per sandbox + **workload identity** — short-lived identity tokens issued to sandbox workloads instead of long-lived secrets (https://docs.e2b.dev/sandbox/workload-identity.md).
- **Artifact export**: filesystem download APIs, pre-signed URLs, volumes (team-level persistent storage mountable across sandboxes), cloud-bucket mounts, git integration (`sandbox.git` clone/branch/push). https://docs.e2b.dev/filesystem/download.md , https://docs.e2b.dev/volumes.md
- **macOS story**: client-side only (SDK/CLI from Mac is fine); execution is always Linux in E2B's cloud or your VPC. No on-Mac runtime.
- **Self-host**: two options. (a) **BYOC** — enterprise-only, AWS/GCP, E2B provisions a cluster in your VPC via Terraform; control plane stays with E2B (https://docs.e2b.dev/byoc.md). (b) **True self-host**: `e2b-dev/infra` is Apache-2.0 and actively maintained (pushed Aug 2026) — the full orchestrator/edge/envd stack, but it's a serious ops undertaking (Nomad/Terraform, needs bare-metal-ish Linux with KVM; not a laptop deployment). https://github.com/e2b-dev/infra
- **Cost** (Aug 2026): usage-based, per-second on *allocated* vCPU/RAM. vCPU $0.000014/s (≈$0.0504/hr), RAM $0.0000045/GiB-s (≈$0.0162/GiB-hr). Default 2 vCPU + 512 MiB ≈ **$0.109/hr**. Hobby $0/mo ($100 one-time credit, 1 hr max runtime, 20 concurrent); Pro $150/mo (24 hr runtime, 100–1,100 concurrent). https://docs.e2b.dev/billing.md , https://docs.e2b.dev/faq/calculate-sandbox-price.md
- **Notable**: they publish ready recipes for running Claude Code / Codex / Devin *inside* E2B sandboxes (https://docs.e2b.dev/use-cases/coding-agents.md) — relevant pattern: run the agent CLI inside the Linux VM instead of fighting macOS seatbelt nesting.

## 2. Daytona — agent sandboxes (⚠ open-source repo abandoned)

- **Isolation model**: container sandboxes (dedicated namespaces, cgroup-enforced hard limits, root-inside-container) plus **VM sandbox classes** (Linux VM & Windows, full kernel boundary, pause/resume/fork/hot-snapshot) and GPU sandboxes. ~90ms cold start for containers. https://www.daytona.io/docs/en/isolation.md
- **Egress control**: per-sandbox firewall; tier-based defaults; three mutually exclusive lockdowns: `network_block_all=True`, CIDR `network_allow_list`, or `domain_allow_list` (wildcards). Note: "essential services" (package registries) stay reachable on all tiers — so it's not a *true* deny-all. Outbound proxy option. https://www.daytona.io/docs/en/network-limits.md
- **Secrets**: the most interesting design of the bunch — org-scoped secrets are injected as **opaque placeholder tokens**; an outbound HTTPS proxy substitutes the real value only in request *headers* and only for allowlisted destination hosts. Plaintext never exists inside the sandbox; responses are scrubbed too. Limits: headers-only, no bodies/query params, breaks on Basic-Auth encoding. https://www.daytona.io/docs/en/secrets.md
- **Artifact export**: filesystem APIs, volumes, snapshots, forks; OCI-image based templates.
- **macOS story**: cloud service; client from Mac fine. No local runtime.
- **Self-host**: ⚠ **the OSS repo (daytonaio/daytona, AGPL-era codebase) is no longer maintained — as of June 2026 core development moved to a private codebase.** You may fork the last public release (v0.190.0) but it's unsupported. Self-hosting Daytona is now effectively a dead end. https://github.com/daytonaio/daytona
- **Cost**: pay-as-you-go per reserved vCPU/RAM/disk by lifecycle state (stopped = disk only). Rates on dashboard/pricing page; tier-based quotas. https://www.daytona.io/docs/en/billing.md

## 3. OpenHands — runtime architecture

- **Isolation model**: client-server "Action Executor" architecture. The backend builds an "OH runtime image" layered on your base Docker image containing the runtime client; the agent loop runs outside, sending Actions over REST/EventStream to the executor inside the container (bash, Jupyter, browser plugins). Isolation = whatever the container gives you (Docker by default; **Apptainer** rootless option for HPC; **API-based sandbox** = their hosted runtime; the SDK also supports remote agent-server on any host). So OpenHands is an *agent framework with pluggable workspaces*, not an isolation technology — you can point `DockerWorkspace` at any Docker daemon, including one inside a stronger boundary. https://docs.all-hands.dev/usage/architecture/runtime , https://docs.openhands.dev/sdk/guides/agent-server/docker-sandbox.md , https://docs.openhands.dev/sdk/guides/agent-server/apptainer-sandbox.md
- **Egress control**: none built-in beyond Docker network config — you supply it (docker network, host firewall, or run the workspace container inside a VM with egress rules).
- **Secrets**: env vars into the container; no proxy-substitution or scoping mechanism. BYO.
- **Artifact export**: workspace dir is a mounted volume / API file ops; git push from inside.
- **macOS story**: works on macOS via Docker Desktop / Colima / OrbStack — i.e. containers already run inside a Linux VM on Mac, which incidentally sidesteps seatbelt nesting (the agent's shell runs in Linux, not under macOS sandbox-exec).
- **Cost**: free/MIT OSS; you pay for compute. OpenHands Cloud is the paid hosted option.

## 4. Modal & Fly Machines as sandbox substrates

**Modal Sandboxes**
- **Isolation**: gVisor-based containers on Modal's fleet, defined at runtime, "secure containers for executing untrusted user or agent code". No inbound by default. https://modal.com/docs/guide/sandbox
- **Egress**: `block_network=True` (full deny), `outbound_cidr_allowlist`, `outbound_domain_allowlist` (beta, TLS/443 only); CIDR+domain combine additively. https://modal.com/docs/guide/sandbox-networking
- **Secrets**: Modal Secrets objects injected as env; scoped per sandbox creation — good per-step granularity since each step can be its own short-lived sandbox.
- **Artifacts**: Volumes, NFS, `sb.open()` file API, image snapshots, memory snapshots.
- **macOS**: client-only; execution in Modal's cloud. **No self-host.**
- **Cost**: per-second on allocated resources; CPU ≈ $0.0000131/core-s (≈$0.047/core-hr), RAM ≈ $0.00000222/GiB-s (≈$0.008/GiB-hr); $30/mo free credit on starter. https://modal.com/pricing

**Fly Machines**
- **Isolation**: **Firecracker microVMs** (hardware virtualization), boot ~300ms, REST API lifecycle. Real VM boundary, stronger than gVisor. https://fly.io/docs/machines/ , https://fly.io/docs/machines/guides-examples/functions-with-machines/
- **Egress**: no per-machine deny-by-default egress knob in the platform; you get org-private 6PN networks + `network` field isolation per app ("one app per customer" pattern) and DIY iptables/nftables inside the VM or an egress proxy machine. Weaker managed egress story than Modal/E2B.
- **Secrets**: app-level secrets (env at boot), tokens per machine possible via Fly tokens; not per-request scoping.
- **Artifacts**: volumes ($0.15/GB-mo), S3-compatible Tigris, or push out over network.
- **macOS**: client-only. **No self-host** (Firecracker requires their platform; though Firecracker itself is OSS).
- **Cost**: e.g. shared-1x-256MB ≈ $0.0028/hr class pricing; pay-per-second while started, rootfs $0.15/GB. https://fly.io/docs/about/pricing/

## 5. Nix + containers/microVMs — reproducible self-hosted sandboxes

- **microvm.nix** (https://github.com/microvm-nix/microvm.nix, handbook https://microvm-nix.github.io/microvm.nix/): declare MicroVMs as `nixosConfigurations` in a flake; 8 hypervisors (qemu, cloud-hypervisor, **firecracker**, crosvm, kvmtool, stratovirt, alioth, **vfkit on macOS**). Read-only root with prepopulated or host-shared `/nix/store`, writable overlay, virtiofs/9p shares, tap networking. **Now runs on macOS via vfkit** (Apple Virtualization.framework) — but vfkit mode has no tap/bridge networking (user-mode networking only), which limits host-enforced egress filtering on Mac.
  - **Isolation**: hardware VM (KVM on Linux, Virtualization.framework on macOS). Strongest boundary of anything here when using firecracker/cloud-hypervisor.
  - **Egress**: on Linux hosts, tap + nftables gives you exact deny-by-default per-VM rules (or route all VM traffic through a filtering proxy VM). On macOS/vfkit: user networking only ⇒ enforce egress with a host-side HTTP(S) proxy the VM is forced through, or PF rules on the NAT interface (coarser).
  - **Reproducibility**: the whole point — VM contents are a Nix closure; pin flake inputs + repo commit and the sandbox is bit-for-bit rebuildable. Nothing else on this list offers that.
- **Apple `container`** (https://github.com/apple/container): Apple's OCI-compatible tool running each Linux container in its own lightweight VM on Apple silicon; requires **macOS 26**; Swift/Containerization framework. Sub-second boot, VM-per-container isolation (better than Docker Desktop's shared VM). Networking is vmnet NAT; no built-in per-container egress allowlists yet — pair with PF rules or a proxy. Pre-1.0, breaking changes between minors. Good candidate as the *local* substrate on team Macs.
- **Firecracker directly**: Linux/KVM only — never on macOS. For the "some-Linux" machines, firecracker (raw, via microvm.nix, or via Cloud Hypervisor) is the gold standard; jailer gives cgroup/chroot/seccomp confinement of the VMM itself.
- **Plain OCI containers via Nix**: `dockerTools`/`nix2container` build images from the same flake; run under Docker/Podman on Linux or Colima/Apple container on macOS. Weaker boundary than VMs (shared kernel on Linux; on macOS you get the Linux VM boundary for free anyway).
- **Secrets**: DIY — inject per-step via virtiofs mount/vsock/cloud-init at boot, remove between steps; or copy Daytona's trick with an egress proxy (e.g. mitmproxy) that substitutes placeholder headers. sops-nix/agenix for at-rest.
- **Artifacts**: virtiofs shared dir, or have the VM push to object storage/git; snapshot block devices.
- **Cost**: hardware you already own; zero marginal cost.

---

## How current agent products describe their own sandboxing

- **Claude Code**: ships a sandboxed Bash tool — OS-enforced filesystem+network isolation for every Bash command and child process. macOS uses built-in **Seatbelt**; Linux/WSL2 use bubblewrap + optional seccomp filter; native Windows unsupported. You configure allowed paths and network domains; unsandboxable commands fall back to permission prompts; `sandbox.failIfUnavailable` makes missing sandbox a hard failure; enforceable org-wide via managed settings. (This Seatbelt usage is exactly why running Claude Code *inside* another seatbelt profile breaks — nested sandbox-exec is not supported by macOS.) https://code.claude.com/docs/en/sandboxing
- **Codex (OpenAI)**: two-layer model of sandbox mode + approval policy. CLI/IDE: OS-level enforcement (Seatbelt on macOS, Landlock/seccomp on Linux), default `workspace-write` with **network off by default**; optional `network_proxy` feature constrains enabled network to domain allow/deny rules. Codex cloud: isolated OpenAI-managed containers, two-phase runtime — networked setup phase installs deps, then the agent phase runs **offline by default**; cloud secrets exist only during setup and are removed before the agent phase. https://learn.chatgpt.com/docs/agent-approvals-security
- **Devin (Cognition)**: cloud Devin runs each session in its own isolated cloud VM/workspace (enterprise: VPC/"Assured" deployments, CMK encryption). Devin CLI's `--sandbox` flag adds OS-level isolation — writable paths derived from granted `Write()` scopes, deny-listed paths hidden entirely, **fail-closed** (refuses to start if sandboxing tools are unavailable; Linux requires bubblewrap+socat; Windows unsupported), plus (unstable) domain-level network filtering through a managed loopback proxy. https://docs.devin.ai/cli/sandbox.md

---

## Recommendation

Target: reproducible sandboxes pinned by **Nix flake + repo commit**, deny-by-default egress, per-step secrets, mostly-macOS + some-Linux, self-hosted preferred.

**Primary: self-hosted Nix microVMs, executed on the Linux box(es); Macs are control-plane clients.**

1. **Make the Linux machine(s) the execution substrate**, not the Macs. Every managed vendor and every serious isolation stack (Firecracker, KVM, bubblewrap) is Linux-first, and your own experience shows nested sandboxing on macOS (seatbelt-in-seatbelt) actively breaks agent CLIs. Phoenix on the Mac stays the control plane; WorkOrder execution is dispatched over the network to a Linux runner.
2. **Use microvm.nix + cloud-hypervisor or firecracker** for the sandbox itself: one flake defines the guest NixOS with (a) the unity-explorer dev shell closure pinned to the WorkOrder's base commit, (b) read-only `/nix/store`, (c) a writable overlay per run. The sandbox identity = `(flake.lock hash, repo commit)` — perfectly reproducible and cacheable. https://microvm-nix.github.io/microvm.nix/
3. **Deny-by-default egress on the host**: give each microVM a tap interface into a bridge with nftables default-drop; allow only your Nix binary cache, GitHub (for the pinned repo), and the LLM API endpoint — ideally via a MITM/CONNECT proxy VM so you get domain-level allowlists and full egress logs. This beats every managed offering's egress story because you own the ruleset.
4. **Per-step secrets**: don't bake secrets into the VM. Inject at step boundary over vsock/virtiofs (mount, use, unmount), and steal Daytona's placeholder-substitution pattern in your egress proxy for anything that must reach an external API — plaintext never enters the guest. https://www.daytona.io/docs/en/secrets.md
5. **On the Macs**, for local/dev-grade runs only: **Apple `container`** (macOS 26, VM-per-container, OCI images built from the same flake via nix2container) or microvm.nix's vfkit target. Accept that Mac-local egress control is coarser (PF/proxy, no tap); treat Mac runs as trusted-tier, and route anything untrusted to the Linux runner. Crucially, agent CLIs (claude/codex) run *inside* the Linux guest, so their own Seatbelt/Landlock sandboxing either engages cleanly (Landlock in the VM) or is unnecessary — no more sandbox-exec nesting failures.
6. **Managed fallback / burst capacity: E2B.** If self-hosting stalls or you need parallel fan-out, E2B is the best fit of the managed options: Firecracker isolation, real deny-all + domain allowlist egress mutable at runtime, workload identity for short-lived creds, pause/fork/snapshot, Apache-2.0 infra you could eventually self-host, and documented recipes for running Claude Code/Codex inside. ~$0.11/hr for a default sandbox. Avoid: Daytona for self-host (OSS abandoned June 2026); Modal/Fly only if you're already invested (Modal = best managed egress after E2B but gVisor + no self-host; Fly = great Firecracker substrate but weak managed egress controls).
7. **Agent framework layer**: OpenHands' SDK (workspace abstraction, Apptainer/Docker/remote agent-server) is worth borrowing or adopting — point its agent-server at your microVM guests and you get the action/observation loop without giving up your isolation layer.

**TL;DR stack**: Phoenix (Mac) → dispatch → Linux runner → microvm.nix Firecracker/cloud-hypervisor guest built from `flake.lock + unity-explorer@commit` → nftables/proxy deny-by-default egress with placeholder-substituting secret proxy → artifacts out via virtiofs/object storage. E2B as the managed escape hatch.
