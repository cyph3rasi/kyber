#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

load_twine_env_file() {
  local env_file="$1"
  if [[ ! -f "$env_file" ]]; then
    return 0
  fi

  # Preserve already-exported shell env vars; only fill from file when absent.
  local orig_user="${TWINE_USERNAME-__KYBER_UNSET__}"
  local orig_pass="${TWINE_PASSWORD-__KYBER_UNSET__}"

  _read_env_key() {
    local key="$1"
    local line
    line="$(grep -E "^(export[[:space:]]+)?${key}[[:space:]]*=" "$env_file" | tail -n 1 || true)"
    if [[ -z "$line" ]]; then
      return 0
    fi
    local value
    value="$(printf '%s\n' "$line" | sed -E "s/^(export[[:space:]]+)?${key}[[:space:]]*=[[:space:]]*//")"
    # Strip matching surrounding single or double quotes.
    if [[ "$value" =~ ^\"(.*)\"$ ]]; then
      value="${BASH_REMATCH[1]}"
    elif [[ "$value" =~ ^\'(.*)\'$ ]]; then
      value="${BASH_REMATCH[1]}"
    fi
    printf '%s' "$value"
  }

  if [[ "$orig_user" == "__KYBER_UNSET__" ]]; then
    local loaded_user
    loaded_user="$(_read_env_key "TWINE_USERNAME")"
    if [[ -n "$loaded_user" ]]; then
      export TWINE_USERNAME="$loaded_user"
    fi
  fi

  if [[ "$orig_pass" == "__KYBER_UNSET__" ]]; then
    local loaded_pass
    loaded_pass="$(_read_env_key "TWINE_PASSWORD")"
    if [[ -n "$loaded_pass" ]]; then
      export TWINE_PASSWORD="$loaded_pass"
    fi
  fi

  if [[ "$orig_user" != "__KYBER_UNSET__" ]]; then
    export TWINE_USERNAME="$orig_user"
  fi
  if [[ "$orig_pass" != "__KYBER_UNSET__" ]]; then
    export TWINE_PASSWORD="$orig_pass"
  fi
}

load_twine_env_file "${KYBER_ENV_FILE:-$HOME/.kyber/.env}"

if [[ "${KYBER_SKIP_PUBLISH:-0}" == "1" ]]; then
  echo "Skipping publish (KYBER_SKIP_PUBLISH=1)"
  exit 0
fi

if [[ -z "${TWINE_USERNAME:-}" || -z "${TWINE_PASSWORD:-}" ]]; then
  echo "Skipping publish: TWINE_USERNAME/TWINE_PASSWORD not set"
  exit 0
fi

echo "Building distribution..."
rm -rf dist build
uvx --from build pyproject-build

echo "Uploading to PyPI..."
uvx twine upload dist/*

echo "Publish complete"
