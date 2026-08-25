{
  description = "data-tournaments sandbox guest: reproducible microVM pinned to (flake.lock, repo commit)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";
    microvm = {
      url = "github:microvm-nix/microvm.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, microvm }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
    in
    {
      # nix build .#sandbox-guest  (Linux + KVM host required)
      packages.${system}.sandbox-guest =
        self.nixosConfigurations.sandbox.config.microvm.declaredRunner;

      nixosConfigurations.sandbox = nixpkgs.lib.nixosSystem {
        inherit system;
        modules = [
          microvm.nixosModules.microvm
          ({ config, lib, pkgs, ... }: {
            system.stateVersion = "25.11";
            networking.hostName = "dt-sandbox";

            microvm = {
              hypervisor = "cloud-hypervisor";
              vcpu = 2;
              mem = 2048;

              # Host /nix/store mounted READ-ONLY; per-run writable overlay.
              shares = [
                {
                  tag = "ro-store";
                  source = "/nix/store";
                  mountPoint = "/nix/.ro-store";
                  proto = "virtiofs";
                }
                {
                  # Per-run workspace + artifact export. The runner script
                  # creates a fresh directory per run and harvests it into
                  # the CAS afterwards.
                  tag = "workspace";
                  source = "/var/lib/dt-sandbox/run";
                  mountPoint = "/workspace";
                  proto = "virtiofs";
                }
              ];
              writableStoreOverlay = "/nix/.rw-store";

              # Tap into the sandbox bridge; egress.nft governs this
              # interface with deny-by-default forwarding.
              interfaces = [
                {
                  type = "tap";
                  id = "vm-dtsbx";
                  mac = "02:00:00:00:dt:01";
                }
              ];
            };

            # Toolchain for work-order verification runs. The unity-explorer
            # checkout itself is pinned by the runner (git clone + checkout
            # of profile.base_commit into /workspace) — code is per-run
            # input, the TOOLCHAIN is what this image pins.
            environment.systemPackages = with pkgs; [
              git
              python3
              dotnet-sdk_8
              ripgrep
              jq
            ];

            # No inbound access, no users with passwords, no sshd. The
            # runner talks to the guest via the serial console / virtiofs
            # exchange only.
            services.openssh.enable = false;
            users.users.runner = {
              isNormalUser = true;
              # Locked account: console autologin only, no secrets.
              hashedPassword = "!";
            };
            services.getty.autologinUser = "runner";

            # Deterministic, minimal surface.
            documentation.enable = false;
            nix.enable = false; # guests never build; store is host-provided
          })
        ];
      };
    };
}
