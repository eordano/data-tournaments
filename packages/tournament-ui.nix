{
  lib,
  stdenv,
  beamPackages,
  esbuild,
  tailwindcss_4,
  git,
}:
let
  pname = "tournament-ui";
  version = "0.1.0";
  src = ../ui;

  mixDepsHashes = {
    "x86_64-linux" = "sha256-rquHFeOgwbL7ikZXOBOdiTcM0E5iYLCb4fjMZMjyH7E=";
    "aarch64-darwin" = "sha256-rquHFeOgwbL7ikZXOBOdiTcM0E5iYLCb4fjMZMjyH7E=";
  };

  mixFodDeps = beamPackages.fetchMixDeps {
    pname = "mix-deps-${pname}";
    inherit src version;
    nativeBuildInputs = [ git ];
    postInstall = ''
      find "$out" -name .git -prune -exec rm -rf {} +
    '';
    hash = mixDepsHashes.${stdenv.hostPlatform.system} or lib.fakeHash;
  };
in
beamPackages.mixRelease {
  inherit
    pname
    version
    src
    mixFodDeps
    ;

  nativeBuildInputs = [
    esbuild
    tailwindcss_4
  ];

  env = {
    DATA_TOURNAMENTS_REPO = "/etc/data-tournaments/repo";
    OPTIMIZE_SCRIPT = "/etc/data-tournaments/repo/bin/optimize.py";
    DOMAIN_BUILDER_SCRIPT = "/etc/data-tournaments/repo/bin/domain_builder_cli.py";
    GENERATE_CARDS_SCRIPT = "/etc/data-tournaments/repo/bin/generate_cards.py";
    LANG = "C.UTF-8";
    LC_ALL = "C.UTF-8";
  };

  preConfigure = ''
    cat >> config/prod.exs <<EOF

    # -- appended by nix build (packages/tournament-ui.nix) --
    config :exqlite, force_build: true
    config :esbuild, path: "${esbuild}/bin/esbuild"
    config :tailwind, path: "${tailwindcss_4}/bin/tailwindcss"
    EOF
  '';

  preBuild = ''
    mix do compile --no-deps-check + assets.deploy
  '';

  removeCookie = true;
}
