{
  uiPackage,
  pythonEnv,
  repoRoot,
}:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.data-tournaments;
  repoEtc = "/etc/data-tournaments/repo";
  stateDir = "/var/lib/data-tournaments";
  releaseClientStub = pkgs.writeShellScript "dt-release-client-disabled" ''
    echo "data-tournaments: temporal release workflow is disabled on this deployment" >&2
    exit 69
  '';
  browseRoots = [
    stateDir
    repoEtc
  ]
  ++ cfg.browseRoots;
  needsHomeTmpfs = lib.any (p: p == "/home" || lib.hasPrefix "/home/" p) cfg.browseRoots;
  secretEnvFile = "${stateDir}/secret-env";
  generateSecretEnv = pkgs.writeShellScript "data-tournaments-secret-env" ''
    set -eu
    umask 077
    mkdir -p ${stateDir}/tmp
    if [ ! -s ${secretEnvFile} ]; then
      printf 'SECRET_KEY_BASE=%s\n' "$(${pkgs.openssl}/bin/openssl rand -hex 64)" \
        > ${secretEnvFile}
    fi
  '';
in
{
  options.services.data-tournaments = {
    enable = lib.mkEnableOption "data-tournaments Phoenix LiveView UI";

    port = lib.mkOption {
      type = lib.types.port;
      default = 18240;
      description = "Loopback port Bandit listens on.";
    };

    host = lib.mkOption {
      type = lib.types.str;
      default = "workflows.example.com";
      description = "Public hostname (PHX_HOST; url scheme/port are https/443).";
    };

    operator = lib.mkOption {
      type = lib.types.str;
      default = "changeme";
      description = "DT_OPERATOR identity gating runs, branch fixes and judgement revision.";
    };

    environmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = ''
        Optional systemd EnvironmentFile providing LANGFUSE_HOST /
        LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / OPENROUTER_API_KEY /
        LLM_BASE_URL / LLM_HJKL_API_KEY, or overriding the auto-generated
        SECRET_KEY_BASE.
      '';
    };

    package = lib.mkOption {
      type = lib.types.package;
      default = uiPackage;
      description = "tournament_ui mix release.";
    };

    extraEnvironment = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      description = "Extra environment variables merged into the unit (e.g. PROMPT_BACKEND).";
    };

    browseRoots = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "/home/alice/workspaces/unity-explorer" ];
      description = ''
        Host directories bind-mounted read-only into the unit's namespace and
        appended to TOURNAMENT_BROWSE_ROOTS, on top of the state directory and
        the /etc repo mirror. Each bind is "-" prefixed, so a root that
        disappears does not keep the unit from starting.
      '';
    };

    supplementaryGroups = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "dcl" ];
      description = ''
        Groups the DynamicUser joins, so a browse root whose owning directory
        is group-readable rather than world-readable can still be traversed.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    environment.etc."data-tournaments/repo".source = repoRoot;

    systemd.services.data-tournaments = {
      description = "data-tournaments Phoenix LiveView UI";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      wantedBy = [ "multi-user.target" ];

      path = [
        pythonEnv
        pkgs.bash
        pkgs.coreutils
        pkgs.sqlite
      ];

      environment = {
        PHX_SERVER = "1";
        PORT = toString cfg.port;
        PHX_HOST = cfg.host;
        RELEASE_DISTRIBUTION = "none";
        RELEASE_COOKIE = "no-distribution";
        RELEASE_TMP = "${stateDir}/tmp";
        HOME = stateDir;
        LANG = "C.UTF-8";
        LC_ALL = "C.UTF-8";

        DATA_TOURNAMENTS_HOME = stateDir;
        DATA_TOURNAMENTS_REPO = repoEtc;
        DATA_TOURNAMENTS_BIN = "${repoEtc}/bin";
        DATA_TOURNAMENTS_CONFIGS = "${repoEtc}/configs";
        OPTIMIZE_SCRIPT = "${repoEtc}/bin/optimize.py";
        DOMAIN_BUILDER_SCRIPT = "${repoEtc}/bin/domain_builder_cli.py";
        GENERATE_CARDS_SCRIPT = "${repoEtc}/bin/generate_cards.py";
        DT_OPERATOR = cfg.operator;
        DT_RELEASE_CLIENT_CMD = "${releaseClientStub}";
        TOURNAMENT_BROWSE_ROOTS = lib.concatStringsSep ":" browseRoots;
      }
      // cfg.extraEnvironment;

      serviceConfig = {
        ExecStartPre = "${generateSecretEnv}";
        ExecStart = "${cfg.package}/bin/tournament_ui start";
        WorkingDirectory = repoEtc;
        Restart = "on-failure";
        RestartSec = "5s";
        DynamicUser = true;
        StateDirectory = "data-tournaments";
        StateDirectoryMode = "0750";
        EnvironmentFile = [
          "-${secretEnvFile}"
        ]
        ++ lib.optional (cfg.environmentFile != null) cfg.environmentFile;

        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectSystem = "strict";
        ProtectHome = if needsHomeTmpfs then "tmpfs" else true;
        BindReadOnlyPaths = map (p: "-${p}") cfg.browseRoots;
        SupplementaryGroups = cfg.supplementaryGroups;
        PrivateDevices = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectKernelLogs = true;
        ProtectControlGroups = true;
        ProtectHostname = true;
        ProtectClock = true;
        ProtectProc = "invisible";
        RestrictNamespaces = true;
        LockPersonality = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        RemoveIPC = true;
        RestrictAddressFamilies = [
          "AF_INET"
          "AF_INET6"
          "AF_UNIX"
        ];
        SystemCallFilter = [ "@system-service" ];
        SystemCallArchitectures = "native";
        UMask = "0077";
      };
    };
  };
}
