#!/usr/bin/env bash
# Validate a release tag before any build uses it.
# Usage: scripts/release-tag.sh <tag> [cargo-version]
#   1 arg : tag must match ^v<major>.<minor>.<patch>(-<prerelease>)?$
#   2 args: tag must also equal "v<cargo-version>"
# Prints the tag on success. Never echoes a tag that failed validation.
set -euo pipefail

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "usage: $0 <tag> [cargo-version]" >&2
  exit 2
fi

tag=$1
re='^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$'

if ! [[ $tag =~ $re ]]; then
  echo "::error title=Invalid release tag::the tag does not match $re" >&2
  exit 1
fi

if [ "$#" -eq 2 ]; then
  version=$2
  if [ "$tag" != "v$version" ]; then
    echo "::error title=Tag/version mismatch::tag $tag but Cargo.toml version is $version" >&2
    exit 1
  fi
fi

echo "$tag"
