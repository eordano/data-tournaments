
DATA_HOME="${DATA_TOURNAMENTS_HOME:-/tmp/data-tournaments}"
BIN_DIR="${DATA_TOURNAMENTS_BIN:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

mkdir -p "$DATA_HOME" "$DATA_HOME/sessions" "$DATA_HOME/runs" "$DATA_HOME/uploads"

_repo_root="$(cd "$BIN_DIR/.." && pwd)"
if [ -f "$_repo_root/.env" ]; then
  while IFS= read -r _line || [ -n "$_line" ]; do
    case "$_line" in
      ''|'#'*) continue ;;
    esac
    _line="${_line#export }"
    case "$_line" in
      *=*) ;;
      *) continue ;;
    esac
    _key="${_line%%=*}"
    _val="${_line#*=}"
    _key="${_key#"${_key%%[![:space:]]*}"}"
    _key="${_key%"${_key##*[![:space:]]}"}"
    _val="${_val#"${_val%%[![:space:]]*}"}"
    _val="${_val%"${_val##*[![:space:]]}"}"
    case "$_val" in
      \"*\") _val="${_val#\"}"; _val="${_val%\"}" ;;
      \'*\') _val="${_val#\'}"; _val="${_val%\'}" ;;
    esac
    if [ -z "${!_key:-}" ]; then
      export "$_key=$_val"
    fi
  done < "$_repo_root/.env"
  if [ -z "${LANGFUSE_HOST:-}" ] && [ -n "${LANGFUSE_BASE_URL:-}" ]; then
    export LANGFUSE_HOST="$LANGFUSE_BASE_URL"
  fi
fi
unset _repo_root _line _key _val
