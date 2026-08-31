{
  description = "data-tournaments — single-elimination LLM agent brackets w/ Langfuse";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pythonOverlay = final: prev: {
          pythonPackagesExtensions = (prev.pythonPackagesExtensions or [ ]) ++ [
            (pyFinal: pyPrev: {
              magicattr = pyFinal.callPackage ./packages/magicattr.nix { };
              gepa = pyFinal.callPackage ./packages/gepa.nix { };
              dspy = pyFinal.callPackage ./packages/dspy.nix { };
            })
          ];
        };

        pkgs = import nixpkgs {
          inherit system;
          config.allowUnsupportedSystem = true;
          overlays = [ pythonOverlay ];
        };

        pythonEnv = pkgs.python3.withPackages (
          ps: with ps; [
            langfuse
            httpx
            dspy
            gepa
            pydantic
            pyyaml
            pytest
            pytest-asyncio
          ]
        );

        binSrc = pkgs.runCommand "data-tournaments-bin" { } ''
          mkdir -p $out
          cp ${./bin/run-tournament.py} $out/run-tournament.py
          cp ${./bin/hermes_mcp_server.py} $out/hermes_mcp_server.py
          cp ${./bin/judgement.py} $out/judgement.py
          cp ${./bin/swiss.py} $out/swiss.py
          cp ${./bin/judgement_schema.sql} $out/judgement_schema.sql
          cp ${./bin/with_lock.py} $out/with_lock.py
        '';

        mcp-server = pkgs.writeShellApplication {
          name = "tournament-mcp-server";
          runtimeInputs = [ pythonEnv ];
          text = ''
            exec ${pythonEnv}/bin/python3 ${binSrc}/hermes_mcp_server.py "$@"
          '';
        };

        run-tournament = pkgs.writeShellApplication {
          name = "run-tournament";
          runtimeInputs = [ pythonEnv ];
          text = ''
            export PYTHONPATH="${binSrc}:''${PYTHONPATH:-}"
            exec ${pythonEnv}/bin/python3 ${binSrc}/run-tournament.py "$@"
          '';
        };

        tournament-ui = pkgs.callPackage ./packages/tournament-ui.nix { };

        devTools = with pkgs; [
          shellcheck
          jq
          sqlite
          elixir
          erlang
          caddy
        ];
      in
      {
        devShells.default = pkgs.mkShell {
          packages = [
            pythonEnv
            mcp-server
            run-tournament
          ]
          ++ devTools;
          shellHook = ''
            export HEX_HOME="''${HEX_HOME:-/tmp/data-tournaments-hex}"
            export MIX_HOME="''${MIX_HOME:-/tmp/data-tournaments-mix}"
            mkdir -p "$HEX_HOME" "$MIX_HOME"
            if [ ! -d "$MIX_HOME/elixir" ]; then
              mix local.hex --force --if-missing >/dev/null 2>&1 || true
              mix local.rebar --force --if-missing >/dev/null 2>&1 || true
            fi
            echo "data-tournaments dev shell"
            echo "  python: $(which python3)  ($(python3 --version))"
            v=$(python3 -c 'import importlib.metadata as m; print(m.version("langfuse"))' 2>/dev/null || echo "??")
            echo "  langfuse: $v"
            echo "  mcp-server: $(which tournament-mcp-server)"
            echo "  orchestrator: $(which run-tournament)"
            echo
            echo "Required env for Langfuse:"
            echo "  LANGFUSE_PUBLIC_KEY  LANGFUSE_SECRET_KEY  LANGFUSE_HOST (optional)"
            echo "Optional env for LLM judge:"
            echo "  set in the tournament config under .judge.api_key_env"
          '';
        };

        apps.default = {
          type = "app";
          program = "${run-tournament}/bin/run-tournament";
        };

        apps.mcp-server = {
          type = "app";
          program = "${mcp-server}/bin/tournament-mcp-server";
        };

        packages = {
          inherit
            pythonEnv
            mcp-server
            run-tournament
            tournament-ui
            ;
          default = run-tournament;
        };

        nixosModules.default = import ./nix/module.nix {
          uiPackage = tournament-ui;
          inherit pythonEnv;
          repoRoot = self;
        };
      }
    );
}
