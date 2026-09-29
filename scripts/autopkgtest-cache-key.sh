#!/usr/bin/env bash
# Resolve the published Ubuntu image identity without downloading the image.
set -euo pipefail

suite=${1:-resolute}
arch=${2:-amd64}
case "$suite" in resolute|noble|stonking) ;; *) echo "Unsupported suite: $suite" >&2; exit 2;; esac
case "$arch" in amd64) ;; *) echo "Unsupported architecture: $arch" >&2; exit 2;; esac

image="$suite-server-cloudimg-$arch.img"
sums_url="https://cloud-images.ubuntu.com/$suite/current/SHA256SUMS"
sums=$(curl --fail --location --retry 3 --silent --show-error "$sums_url")
digest=$(awk -v image="*$image" '$2 == image {print $1}' <<<"$sums")
if [[ ! "$digest" =~ ^[0-9a-f]{64}$ ]]; then
  echo "No unique SHA256 for $image in $sums_url" >&2
  exit 1
fi
script_digest=$(sha256sum scripts/prepare-autopkgtest.sh | cut -d' ' -f1)
key="autopkgtest-v1-$suite-$arch-$digest-$script_digest"

if [[ -n ${GITHUB_OUTPUT:-} ]]; then
  printf 'key=%s\n' "$key" >> "$GITHUB_OUTPUT"
  printf 'upstream_sha256=%s\n' "$digest" >> "$GITHUB_OUTPUT"
fi
printf '%s\n' "$key"
