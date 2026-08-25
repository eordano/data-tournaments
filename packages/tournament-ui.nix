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

  # fetchMixDeps output can differ per platform (compile artefacts from a few
  # deps leak into the fixed-output hash) — keep one hash per system, pinned
  # from the failed build's `got: sha256-...` line (garden_lens precedent).
  mixDepsHashes = {
    "x86_64-linux" = "sha256-rquHFeOgwbL7ikZXOBOdiTcM0E5iYLCb4fjMZMjyH7E=";
    "aarch64-darwin" = "sha256-rquHFeOgwbL7ikZXOBOdiTcM0E5iYLCb4fjMZMjyH7E=";
  };

  mixFodDeps = beamPackages.fetchMixDeps {
    pname = "mix-deps-${pname}";
    inherit src version;
    # mix.lock has one git (non-hex) dep: tailwindlabs/heroicons tag v2.2.0,
    # rev 0435d4ca364a608cc75e2f8683d374e55abbae26, sparse "optimized".
    # mix deps.get needs git on PATH to fetch it.
    nativeBuildInputs = [ git ];
    # Strip .git dirs so the FOD output is stable across git versions.
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

  # Several modules capture env vars into module attributes at COMPILE time
  # (ui/lib/tournament_ui_web/live/{domains_live,domain_edit_live,environment_live}.ex,
  # ui/lib/tournament_ui/judgement.ex). Bake the stable /etc symlink the NixOS
  # module materializes — NOT a store path — so editing the repo does not
  # rebuild the release and the baked paths stay valid across repo updates.
  env = {
    DATA_TOURNAMENTS_REPO = "/etc/data-tournaments/repo";
    OPTIMIZE_SCRIPT = "/etc/data-tournaments/repo/bin/optimize.py";
    DOMAIN_BUILDER_SCRIPT = "/etc/data-tournaments/repo/bin/domain_builder_cli.py";
    GENERATE_CARDS_SCRIPT = "/etc/data-tournaments/repo/bin/generate_cards.py";
    LANG = "C.UTF-8";
    LC_ALL = "C.UTF-8";
  };

  # MUST run before configurePhase: mix deps.compile builds the exqlite NIF,
  # which by default DOWNLOADS a precompiled sqlite3_nif (fails in the sandbox).
  # force_build makes it compile the bundled amalgamation with stdenv cc.
  # The esbuild/tailwind :path overrides point the hex wrappers at the nix
  # binaries instead of downloading (version mismatch vs the pins 0.25.4 /
  # 4.1.12 in config/config.exs only produces a warning when :path is set).
  preConfigure = ''
    cat >> config/prod.exs <<EOF

    # -- appended by nix build (packages/tournament-ui.nix) --
    config :exqlite, force_build: true
    config :esbuild, path: "${esbuild}/bin/esbuild"
    config :tailwind, path: "${tailwindcss_4}/bin/tailwindcss"
    EOF
  '';

  # Compile first: `mix compile` extracts the phoenix-colocated JS package
  # (imported by assets/js/app.js as "phoenix-colocated/tournament_ui") into
  # _build/prod, which is on esbuild's NODE_PATH (config/config.exs). Only
  # then can assets.deploy (tailwind --minify, esbuild --minify, phx.digest)
  # succeed; phx.digest writes priv/static/cache_manifest.json required by
  # prod.exs cache_static_manifest. deps/heroicons (from the FOD, .git
  # stripped) is read by assets/vendor/heroicons.js at ../../deps/heroicons —
  # the rev check needs .git, so deps checks must be skipped. A single `mix do`
  # VM is load-bearing: compile runs deps.loadpaths with --no-deps-check and
  # marks it done, so the assets.deploy subtasks (tailwind/esbuild call
  # Mix.Task.run("loadpaths") with no way to pass the flag) skip the re-check.
  preBuild = ''
    mix do compile --no-deps-check + assets.deploy
  '';

  # No distribution in prod (RELEASE_DISTRIBUTION=none set by the module);
  # strip the generated cookie rather than shipping one in the store.
  removeCookie = true;
}
