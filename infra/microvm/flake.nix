{
  description = "data-tournaments sandbox guest: reproducible microVM pinned to (flake.lock, repo commit)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";
    microvm = {
      url = "github:microvm-nix/microvm.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      microvm,
    }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
    in
    {
      packages.${system}.sandbox-guest = self.nixosConfigurations.sandbox.config.microvm.declaredRunner;

      nixosConfigurations.sandbox = nixpkgs.lib.nixosSystem {
        inherit system;
        modules = [
          microvm.nixosModules.microvm
          (
            {
              config,
              lib,
              pkgs,
              ...
            }:
            {
              system.stateVersion = "25.11";
              networking.hostName = "dt-sandbox";

              microvm = {
                hypervisor = "cloud-hypervisor";
                vcpu = 2;
                mem = 2048;

                shares = [
                  {
                    tag = "ro-store";
                    source = "/nix/store";
                    mountPoint = "/nix/.ro-store";
                    proto = "virtiofs";
                  }
                  {
                    tag = "workspace";
                    source = "/var/lib/dt-sandbox/run";
                    mountPoint = "/workspace";
                    proto = "virtiofs";
                  }
                ];
                writableStoreOverlay = "/nix/.rw-store";

                interfaces = [
                  {
                    type = "tap";
                    id = "vm-dtsbx";
                    mac = "02:00:00:00:dt:01";
                  }
                ];
              };

              environment.systemPackages = with pkgs; [
                git
                python3
                dotnet-sdk_8
                ripgrep
                jq
              ];

              services.openssh.enable = false;
              users.users.runner = {
                isNormalUser = true;
                hashedPassword = "!";
              };
              services.getty.autologinUser = "runner";

              documentation.enable = false;
              nix.enable = false;
            }
          )
        ];
      };
    };
}
