#!/usr/bin/env bash
# setup-hermes-slots.sh [N]  — register N Hermes MCP slots in ~/.hermes/config.yaml.
#
# Builds the flake-pinned `tournament-mcp-server` binary and writes its
# absolute /nix/store path into each slot's `command:` field. That way
# Hermes always invokes the right Python (with langfuse + httpx baked in)
# regardless of which shell spawned it.
#
# Re-run this whenever flake.nix changes the pinned langfuse version.
set -euo pipefail
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"

N="${1:-4}"
YAML=~/.hermes/config.yaml

# Repo root: parent of bin/.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Build (or pull from cache) the flake's mcp-server. The store path is
# stable as long as flake.nix + bin/hermes_mcp_server.py don't change.
echo "Building tournament-mcp-server from $REPO_ROOT/flake.nix ..."
MCP_OUT=$(nix build "path:$REPO_ROOT#mcp-server" --print-out-paths --no-link)
MCP_BIN="$MCP_OUT/bin/tournament-mcp-server"

if [ ! -x "$MCP_BIN" ]; then
  echo "ERROR: built mcp-server but no executable at $MCP_BIN" >&2
  exit 1
fi
echo "  -> $MCP_BIN"

python3 - "$YAML" "$N" "$DATA_HOME" "$MCP_BIN" <<'PY'
import re, sys
path, n, home, mcp_bin = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
text = open(path).read()

def block_for(name, config_path):
    return (
        f"  {name}:\n"
        f"    command: {mcp_bin}\n"
        f"    enabled: true\n"
        f"    env:\n"
        f"      HERMES_HARNESS_CONFIG: {config_path}\n"
    )

# Wipe any existing tournament-slot-* blocks (also clean up the legacy
# python3-+-args shape from the pre-flake setup).
text = re.sub(
    r"^  tournament-slot-\d+:\n(?:    [^\n]*\n)+", "", text, flags=re.M,
)
# Also clean up the unsuffixed "tournament:" block (legacy).
text = re.sub(
    r"^  tournament:\n(?:    [^\n]*\n)+", "", text, flags=re.M,
)

new_blocks = "".join(
    block_for(f"tournament-slot-{i}", f"{home}/active-{i}.json")
    for i in range(n)
)

if "mcp_servers:\n" not in text:
    text += "\nmcp_servers:\n" + new_blocks
else:
    match = re.search(r"^mcp_servers:\n((?:  [^\n]*\n|    [^\n]*\n)*)", text, re.M)
    existing = match.group(1)
    replacement = "mcp_servers:\n" + existing + new_blocks
    text = text[:match.start()] + replacement + text[match.end():]

open(path, "w").write(text)
print(f"Registered {n} slot(s).")
print(f"  command: {mcp_bin}")
print(f"  config base: {home}/active-N.json")
PY
