#!/usr/bin/env bash
# Bind the cached upgraded source chroot to its target and preparation policy.
set -euo pipefail

suite=${1:-resolute}
arch=${2:-amd64}
case "$suite" in noble|resolute|stonking|noble-uca-epoxy) ;; *) echo "Unsupported suite: $suite" >&2; exit 2;; esac
case "$arch" in amd64) ;; *) echo "Unsupported architecture: $arch" >&2; exit 2;; esac

script_digest=$(sha256sum scripts/prepare-builder.sh | cut -d' ' -f1)
cache_date=${SBUILD_CACHE_DATE:-$(date -u +%Y-%m-%d)}
key="sbuild-rootfs-host-tools-v2-ubuntu-24.04-$suite-$arch-$cache_date-$script_digest"
if [[ -n ${GITHUB_OUTPUT:-} ]]; then
    printf 'key=%s\n' "$key" >> "$GITHUB_OUTPUT"
fi
printf '%s\n' "$key"
