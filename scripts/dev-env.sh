#!/usr/bin/env bash
# Run a command against the isolated Hermes development environment.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
venv="${HERMES_DEV_VENV:-$HOME/.hermes/venvs/hermes-dev}"

if [[ ! -x "$venv/bin/python" || ! -x "$venv/bin/hermes" ]]; then
  echo "error: Hermes dev venv is missing at $venv" >&2
  echo "Create it with: uv venv $venv --python 3.11 && uv pip install --python $venv/bin/python -e '.[all,dev]'" >&2
  exit 1
fi

if [[ -n "${HERMES_DEV_NODE_BIN:-}" ]]; then
  node_bin="$HERMES_DEV_NODE_BIN"
elif command -v brew >/dev/null 2>&1 && [[ -x "$(brew --prefix node)/bin/node" ]]; then
  node_bin="$(brew --prefix node)/bin"
elif command -v node >/dev/null 2>&1; then
  node_bin="$(dirname "$(command -v node)")"
else
  echo "error: Node.js 26 is required; set HERMES_DEV_NODE_BIN if it is not on PATH" >&2
  exit 1
fi

node_major="$($node_bin/node --version | sed -E 's/^v([0-9]+).*/\1/')"
if [[ "$node_major" -ne 26 ]]; then
  echo "error: Node.js 26 is required by .nvmrc; found $($node_bin/node --version) at $node_bin" >&2
  exit 1
fi

export PATH="$node_bin:$venv/bin:$PATH"
export HERMES_PYTHON="$venv/bin/python"
# Never inherit the managed daily-use profile from the calling shell. The
# development launcher must remain isolated even when Hermes Desktop or a
# parent process exported HERMES_HOME. Use HERMES_DEV_HOME for an explicit
# alternate development profile.
export HERMES_HOME="${HERMES_DEV_HOME:-$HOME/.hermes-dev}"
export HERMES_DESKTOP_HERMES_ROOT="${HERMES_DESKTOP_HERMES_ROOT:-$repo_root}"

if [[ "$#" -eq 0 ]]; then
  printf 'HERMES_HOME=%s\n' "$HERMES_HOME"
  printf 'HERMES_DESKTOP_HERMES_ROOT=%s\n' "$HERMES_DESKTOP_HERMES_ROOT"
  printf 'Python=%s\n' "$venv/bin/python"
  printf 'HERMES_PYTHON=%s\n' "$HERMES_PYTHON"
  printf 'Node=%s\n' "$node_bin/node"
  exit 0
fi

exec "$@"
