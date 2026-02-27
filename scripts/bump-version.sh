#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if [[ ! -f pyproject.toml ]]; then
  echo "pyproject.toml not found"
  exit 1
fi

# Date-based versioning:
#   YEAR.MONTH.DAY.BUILD_NUMBER_FOR_DAY
# Example:
#   2026.2.27.1, 2026.2.27.2, ...
today_prefix="$(date +%Y.%-m.%-d)"

current_version="$(awk -F '"' '/^version = "/ { print $2; exit }' pyproject.toml)"
if [[ -z "$current_version" ]]; then
  echo "Could not read current version from pyproject.toml"
  exit 1
fi

new_build=1
if [[ "$current_version" =~ ^([0-9]{4})\.([0-9]{1,2})\.([0-9]{1,2})\.([0-9]+)$ ]]; then
  current_prefix="${BASH_REMATCH[1]}.${BASH_REMATCH[2]}.${BASH_REMATCH[3]}"
  current_build="${BASH_REMATCH[4]}"
  if [[ "$current_prefix" == "$today_prefix" ]]; then
    new_build=$((current_build + 1))
  fi
fi

new_version="${today_prefix}.${new_build}"

tmp_file="$(mktemp)"
awk -v ver="$new_version" '
  BEGIN { changed = 0 }
  /^version = "/ && changed == 0 {
    print "version = \"" ver "\""
    changed = 1
    next
  }
  { print }
  END {
    if (changed == 0) {
      exit 2
    }
  }
' pyproject.toml > "$tmp_file"
status=$?
if [[ $status -eq 2 ]]; then
  rm -f "$tmp_file"
  echo "Could not find version field in pyproject.toml"
  exit 1
fi

mv "$tmp_file" pyproject.toml
echo "Bumped version -> ${new_version}"
