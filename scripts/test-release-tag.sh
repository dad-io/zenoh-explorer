#!/usr/bin/env bash
# Table-driven tests for scripts/release-tag.sh. Run: bash scripts/test-release-tag.sh
set -uo pipefail

script="$(cd "$(dirname "$0")" && pwd)/release-tag.sh"
pass=0
fail=0

check() { # name expected_exit args...
  local name=$1 expected=$2
  shift 2
  "$script" "$@" >/dev/null 2>&1
  local got=$?
  if [ "$got" -eq "$expected" ]; then
    pass=$((pass + 1))
  else
    echo "FAIL $name: expected exit $expected, got $got" >&2
    fail=$((fail + 1))
  fi
}

check plain            0 v0.9.1
check prerelease       0 v1.2.3-rc.1
check matches-version  0 v0.9.1 0.9.1
check prerelease-match 0 v1.2.3-rc.1 1.2.3-rc.1
check no-v             1 0.9.1
check two-parts        1 v0.9
# shellcheck disable=SC2016 # the literal $(id) is the point of this case
check injection        1 'v1.0.0$(id)'
check newline          1 $'v1.0.0\nfoo'
check space            1 'v1.0.0 x'
check mismatch         1 v0.9.2 0.9.1
check no-args          2

if [ "$fail" -eq 0 ]; then
  echo "all $pass cases passed"
else
  echo "$fail of $((pass + fail)) cases failed" >&2
  exit 1
fi
