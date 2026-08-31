#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
MODE="${1:-smoke}"
shift || true

CACHE="$REPO/.lint-jail-cache"
mkdir -p "$CACHE/home" "$CACHE/nuget"

TOOLCHAIN="${TOOLCHAIN:-}"
if [ -z "$TOOLCHAIN" ] && [ -s "$CACHE/toolchain-path" ]; then
  TOOLCHAIN="$(cat "$CACHE/toolchain-path")"
  [ -x "$TOOLCHAIN/bin/dotnet" ] || TOOLCHAIN=""
fi
if [ -z "$TOOLCHAIN" ]; then
  TOOLCHAIN="$(nix build --no-link --print-out-paths "path:$HERE#toolchain")"
  printf '%s\n' "$TOOLCHAIN" > "$CACHE/toolchain-path"
fi

: > "$CACHE/empty-resolv.conf"
RESOLV_DEST="$(readlink -f /etc/resolv.conf 2>/dev/null || echo /etc/resolv.conf)"

prep_feed() {
  mkdir -p "$CACHE/feed" "$CACHE/home/.nuget/NuGet"
  find "$CACHE/nuget" -name '*.nupkg' ! -name '*.symbols.nupkg' 2>/dev/null \
    | while read -r p; do ln -f "$p" "$CACHE/feed/$(basename "$p")"; done
  REFPACK="$CACHE/feed/microsoft.netcore.app.ref.3.1.0.nupkg"
  [ -s "$REFPACK" ] || curl -sfL -o "$REFPACK" \
    https://api.nuget.org/v3-flatcontainer/microsoft.netcore.app.ref/3.1.0/microsoft.netcore.app.ref.3.1.0.nupkg \
    || echo "lint-jail: warning — could not fetch microsoft.netcore.app.ref (offline?); CodeAnalysis.Testing suites need it once" >&2
  printf '<configuration><packageSources><clear/><add key="local" value="%s" /></packageSources></configuration>\n' \
    "$CACHE/feed" > "$CACHE/nuget-local.config"
  printf '<configuration><packageSources><clear/><add key="local" value="%s" /><add key="nuget.org" value="https://api.nuget.org/v3/index.json" /></packageSources></configuration>\n' \
    "$CACHE/feed" > "$CACHE/nuget-online.config"
  if [ "${MODE_ONLINE:-0}" = "1" ]; then
    cp "$CACHE/nuget-online.config" "$CACHE/home/.nuget/NuGet/NuGet.Config"
  else
    cp "$CACHE/nuget-local.config" "$CACHE/home/.nuget/NuGet/NuGet.Config"
  fi
}

if [ "${MODE_ONLINE:-0}" = "1" ]; then
  NET_OPTS=()
else
  NET_OPTS=(
    --ro-bind "$CACHE/empty-resolv.conf" "$RESOLV_DEST"
    --unshare-user --uid 0 --gid 0
    --cap-add CAP_NET_ADMIN --unshare-net
  )
fi

JAIL_CHDIR="$REPO"
EXTRA_BINDS=()

jail() {
  "$TOOLCHAIN/bin/bwrap" \
    --ro-bind /nix /nix \
    --ro-bind /run/current-system /run/current-system \
    --ro-bind /etc /etc \
    --ro-bind /bin /bin --ro-bind-try /usr/bin /usr/bin \
    --ro-bind "$REPO" "$REPO" \
    --bind "$CACHE" "$CACHE" \
    --tmpfs /tmp \
    --proc /proc --dev /dev \
    --setenv HOME "$CACHE/home" \
    --setenv NUGET_PACKAGES "$CACHE/nuget" \
    --setenv DOTNET_CLI_TELEMETRY_OPTOUT 1 \
    --setenv DOTNET_NOLOGO 1 \
    --setenv PATH "$TOOLCHAIN/bin:/run/current-system/sw/bin" \
    "${NET_OPTS[@]}" \
    "${EXTRA_BINDS[@]}" \
    --unshare-pid --unshare-ipc --unshare-uts \
    --die-with-parent \
    --chdir "$JAIL_CHDIR" \
    bash -c 'ip link set lo up 2>/dev/null || true; exec "$@"' _ "$@"
}

run_tests() {
  local proj="$1"
  shift
  [ -d "$REPO/$proj" ] || {
    echo "lint-jail: no such project dir: $proj" >&2
    exit 3
  }
  prep_feed
  local nuget_cfg="nuget-local.config"
  [ "${MODE_ONLINE:-0}" = "1" ] && nuget_cfg="nuget-online.config"
  local bdir="$CACHE/build.$$"
  local rc=0
  jail bash -c 'rm -rf "$0" && cp -r "$2" "$0" \
    && cp "$3/$1" "$0/NuGet.config" \
    && cd "$0" && dotnet test -v q --nologo "${@:4}"' \
    "$bdir" "$nuget_cfg" "$proj" "$CACHE" "$@" || rc=$?
  rm -rf "$bdir"
  return "$rc"
}

case "$MODE" in
  smoke)
    run_tests scripts/lint/tests/smoke
    ;;
  test)
    [ $# -ge 1 ] || {
      echo "usage: lint-jail.sh test <project-dir-relative-to-repo> [dotnet test args..]" >&2
      exit 2
    }
    run_tests "$@"
    ;;
  exec)
    [ $# -ge 2 ] || {
      echo "usage: lint-jail.sh exec <writable-workspace-dir> <cmd..>" >&2
      exit 2
    }
    WS="$(cd "$1" && pwd)" || {
      echo "lint-jail: no such workspace dir: $1" >&2
      exit 3
    }
    shift
    prep_feed
    JAIL_CHDIR="$WS"
    EXTRA_BINDS=(--bind "$WS" "$WS")
    jail "$@"
    ;;
  *)
    echo "lint-jail: unknown mode '$MODE' (smoke|test)" >&2
    exit 2
    ;;
esac
