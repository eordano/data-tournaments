{
  description = "data-tournaments lint-jail toolchain — the pinned environment C# build/test runners execute in";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/64c08a7ca051951c8eae34e3e3cb1e202fe36786";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forSystems (pkgs: rec {
        toolchain = pkgs.buildEnv {
          name = "lint-jail-toolchain";
          paths = with pkgs; [
            dotnet-sdk_8
            bubblewrap
            curl
            iproute2
            bash
            coreutils
            findutils
            gnugrep
            gnused
          ];
        };
        default = toolchain;
      });

      devShells = forSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [ self.packages.${pkgs.stdenv.hostPlatform.system}.toolchain ];
        };
      });
    };
}
