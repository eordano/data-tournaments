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
  # v1: Temporal release-workflow (approvals) is disabled — no Temporal server
  # and no temporalio in pythonEnv. The stub fails loudly instead of hanging.
  releaseClientStub = pkgs.writeShellScript "dt-release-client-disabled" ''
    echo "data-tournaments: temporal release workflow is disabled on this deployment" >&2
    exit 69
  '';
  # SECRET_KEY_BASE (Phoenix cookie/session signing) is generated once on
  # first start and persisted in the state directory, so the service needs no
  # pre-provisioned secret. cfg.environmentFile is loaded afterwards and can
  # override it or add API keys.
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
      default = "workflows.decent.dev";
      description = "Public hostname (PHX_HOST; url scheme/port are https/443).";
    };

    operator = lib.mkOption {
      type = lib.types.str;
      default = "eordano";
      description = "DT_OPERATOR identity gating runs, branch fixes and judgement revision.";
    };

    environmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = ''
        Optional systemd EnvironmentFile providing LANGFUSE_HOST /
        LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / OPENROUTER_API_KEY /
        LLM_BASE_URL / LLM_GATEWAY_API_KEY, or overriding the auto-generated
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
  };

  config = lib.mkIf cfg.enable {
    # Stable, non-store path baked into the release at compile time and used
    # as WorkingDirectory so the LiveViews' default `python3 bin/<x>.py`
    # shell-outs resolve. Read-only: all writes go to DATA_TOURNAMENTS_HOME.
    environment.etc."data-tournaments/repo".source = repoRoot;

    systemd.services.data-tournaments = {
      description = "data-tournaments Phoenix LiveView UI";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      wantedBy = [ "multi-user.target" ];

      # python3 with langfuse/dspy/gepa for the CLI shell-outs; sqlite for
      # ad-hoc CLI use inside scripts.
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
        # The package strips releases/COOKIE (removeCookie); the release
        # script still `cat`s it when RELEASE_COOKIE is unset and dies.
        # Distribution is off, so any value works.
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
          # "-": tolerated as missing at unit load; ExecStartPre creates it
          # before ExecStart's environment is assembled.
          "-${secretEnvFile}"
        ]
        ++ lib.optional (cfg.environmentFile != null) cfg.environmentFile;

        # butterfly-effect-style hardening, minus IPAddressDeny (the app and
        # its python shell-outs call Langfuse/OpenRouter over the network)
        # and minus MemoryDenyWriteExecute (BEAM JIT).
        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectSystem = "strict";
        ProtectHome = true;
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
